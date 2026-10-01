"""Substack newsletters as a discovery source (jobs + companies + people).

Four writers Eric follows publish exactly what the pipeline hunts: specific
early-stage companies, job listings with ATS links, and founders worth
contacting. Verified 2026-08-06: every pub's `/feed` RSS carries FULL post
bodies for free posts, and listing lines arrive structured —
`<strong>Datalab</strong> (Seed, AI for document intelligence, NY),
<a href="https://jobs.ashbyhq.com/datalab/...">Founding Business Operations</a>`
— so the bulk of extraction is deterministic link-walking, no LLM needed.

Two tiers:
  1. ATS-shaped links -> RawPosting, straight into the normal scout path
     (prefilter, dedupe-by-identity, scoring). The url is the REAL job URL, so
     the same role found on another board collapses into one entry.
  2. Hiring/founder prose without an ATS link -> data/substack_queue.json for
     an in-session/API extraction pass (enrichment-queue pattern). People
     NEVER land in the overlay from here — newsletter blurbs are third-party
     text, so person leads go to data/substack_people_queue.json for review
     (the honesty rule: a person's record only ever quotes their own text).
     For the same reason this adapter never sets RawPosting.founders.

Seen-memory: data/substack_seen.json, keyed by post URL. Committed only AFTER
feed.save (funding.py's ordering lesson) via commit_seen(), which cli.scout
calls — a crash mid-scout must not mark posts seen whose finds were never
recorded.

Paywalled posts (`audience: only_paid` in the archive API) are skipped and
counted. Custom-domain pubs must be fetched at the custom domain — the
substack.com subdomain 301s (fysk -> newsletter.foundersysk.com).
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

from selectolax.parser import HTMLParser

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

ROOT = Path(__file__).resolve().parent.parent.parent
SEEN = ROOT / "data" / "substack_seen.json"
QUEUE = ROOT / "data" / "substack_queue.json"
PEOPLE_QUEUE = ROOT / "data" / "substack_people_queue.json"

# Job-link shapes that identify a specific posting. Bare careers-page links
# ("see all open roles") are Tier 2's problem, not a posting.
_NON_ROLE_ANCHORS = {
    "careers", "careers page", "jobs", "jobs page", "job board", "open roles",
    "all roles", "apply", "apply here", "here", "link", "more roles", "hiring page",
}
# "(Seed, AI for document intelligence, NY)" — stage token first, city last.
_STAGES = {
    "pre-seed": "pre_seed", "preseed": "pre_seed", "seed": "seed",
    "series a": "series_a", "series b": "series_b", "series c": "series_c",
    "series d": "series_d", "series e": "series_e", "late stage": "late",
    "growth": "growth", "public": "public",
}
_LEAD_LANGUAGE = re.compile(
    r"is hiring|are hiring|hiring for|hiring a |reach out|email (?:him|her|them|the founder)"
    r"|dm (?:him|her|them)|worth knowing|founders? you should know|open to meeting"
    r"|just raised|recently raised|should you join|looking for (?:a |an |their first)",
    re.I,
)

# Seen-state pending commit: fetch() stages, cli.scout commits after feed.save.
_PENDING: dict[str, dict] = {}


# ---------------------------------------------------------------- seen-memory

def load_seen() -> dict:
    try:
        return json.loads(SEEN.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def commit_seen() -> int:
    """Write staged seen-state. Called by cli.scout AFTER feed.save."""
    global _PENDING
    if not _PENDING:
        return 0
    seen = load_seen()
    seen.update(_PENDING)
    n = len(_PENDING)
    _PENDING = {}
    tmp = SEEN.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=1, sort_keys=True))
    tmp.replace(SEEN)
    return n


# ---------------------------------------------------------------- extraction

def _clean_url(href: str) -> str:
    """Drop tracking queries — the newsletter's ?utm_source would defeat the
    url dedupe against the same role seen on the ATS directly."""
    return href.split("?")[0].rstrip("/")


def _is_job_link(href: str, anchor_text: str) -> bool:
    if not href.startswith("http"):
        return False
    rest = href.split("//", 1)[1]
    host, _, tail = rest.partition("/")
    path = "/" + tail.split("?")[0]
    depth = len([s for s in path.split("/") if s])
    text = anchor_text.strip().lower()
    if not text or text in _NON_ROLE_ANCHORS:
        return False
    # A company's ATS root ("boards.greenhouse.io/datalab") is a careers page,
    # not a posting — each ATS needs its specific-job depth.
    if host.endswith("ashbyhq.com") or host.endswith("lever.co"):
        return depth >= 2
    if host.endswith("greenhouse.io"):
        return "/jobs/" in path
    if host.endswith(("wellfound.com", "workatastartup.com", "jobs.a16z.com")):
        return "/jobs/" in path
    # Company-site career links count only when the path names a specific role.
    return re.search(r"/(careers|jobs|positions|openings)/.+", path) is not None


def _parse_parenthetical(text: str) -> tuple[str, str, list[str]]:
    """'(Seed, AI for document intelligence, NY)' -> (stage, location, tags)."""
    m = re.search(r"\(([^)]{3,120})\)", text)
    if not m:
        return "", "", []
    parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
    if not parts:
        return "", "", []
    stage = ""
    first = parts[0].lower()
    for token, canon in _STAGES.items():
        if token in first:
            stage = canon
            parts = parts[1:]
            break
    location = parts[-1] if parts else ""
    tags = parts[:-1] if len(parts) > 1 else []
    # A "location" like 'AI for housing' is a tag that ended up last.
    if location and len(location) > 24:
        tags.append(location)
        location = ""
    return stage, location, tags


# Newsletter section labels that sit in headings where a company name would —
# "Open roles", "Build -1 → 0", "Top 10 Open Roles" all imported as companies
# on the second live backfill before this blacklist existed.
_SECTION_HEADING = re.compile(
    r"open role|top \d|now hiring|who.?s hiring|^roles?\b|^jobs?\b|talent|"
    r"moves|^build\b|hiring$|newsletter|this week", re.I)

_ROLE_WORDS = re.compile(
    r"\b(engineer|engineering|developer|designer|manager|lead|head|director|vp|"
    r"president|chief|officer|analyst|scientist|researcher|marketing|marketer|"
    r"sales|account|success|operations|ops|recruiter|founding|intern|associate|"
    r"specialist|strategist|generalist|growth|product|staff|principal|counsel|"
    r"architect|partnerships|bizops|gtm)\b", re.I)


def _looks_like_role(text: str) -> bool:
    return bool(text) and _ROLE_WORDS.search(text) is not None


# Companies are short noun phrases. Sentences ("SpaceX is buying Cursor in a
# $60B stock deal"), CTA anchors ("Apply here") and section labels are not —
# all three imported as companies before this gate existed.
_SENTENCEY = re.compile(r"\b(is|are|was|were|just|raised|buying|hiring|deal|announc)\b", re.I)


def _plausible_company(text: str) -> bool:
    t = (text or "").strip()
    return (2 <= len(t) <= 35
            and len(t.split()) <= 4
            and t.lower() not in _NON_ROLE_ANCHORS
            and not _looks_like_role(t)
            and not _SECTION_HEADING.search(t)
            and not _SENTENCEY.search(t))


def _slug_company(url: str) -> str:
    """Ashby/Lever/Greenhouse URLs embed the company slug as a path segment —
    the most reliable company signal a listing line carries."""
    rest = url.split("//", 1)[-1]
    host, _, tail = rest.partition("/")
    parts = [p for p in tail.split("/") if p]
    slug = ""
    if host.endswith(("ashbyhq.com", "lever.co")) and parts:
        slug = parts[0]
    elif host.endswith("greenhouse.io") and parts:
        slug = parts[0] if parts[0] != "embed" else ""
    if not slug or slug in ("jobs", "job"):
        return ""
    pretty = re.sub(r"[-_]+", " ", slug).strip()
    return pretty if pretty.upper() == pretty else pretty.title() if pretty.islower() else pretty


def _container_of(node):
    """The listing line an anchor belongs to: nearest li or p ancestor."""
    cur = node.parent
    while cur is not None and cur.tag not in ("li", "p", "body"):
        cur = cur.parent
    return cur if cur is not None and cur.tag in ("li", "p") else None


def _company_for(anchor, container) -> str:
    """Last <strong>/<b> text before the anchor inside the listing line."""
    if container is None:
        return ""
    best = ""
    for node in container.traverse(include_text=False):
        if node is anchor:
            break
        if node.tag in ("strong", "b"):
            t = node.text(strip=True)
            if t:
                best = t
    return best.strip().rstrip(",:").strip()


def extract_postings(body_html: str, source: str, pub: str,
                     post_title: str, post_url: str,
                     posted: date | None) -> list[RawPosting]:
    tree = HTMLParser(body_html)
    out: list[RawPosting] = []
    seen_urls: set[str] = set()

    # One document-order pass so each anchor knows the heading above it —
    # a16z Build puts the company in an <h4> over a list of role links.
    last_heading = ""
    for node in tree.root.traverse(include_text=False):
        if node.tag in ("h1", "h2", "h3", "h4", "h5"):
            last_heading = node.text(strip=True)
            continue
        if node.tag != "a":
            continue
        href = node.attributes.get("href") or ""
        anchor = node.text(strip=True)
        if not _is_job_link(href, anchor):
            continue
        url = _clean_url(href)
        if url in seen_urls:
            continue
        container = _container_of(node)
        line = container.text(strip=True) if container is not None else anchor
        strong = _company_for(node, container)

        # The two observed layouts are inverses of each other:
        #   AI Operators:  <strong>Company</strong> (Seed, …, NY), <a>Role</a>
        #   a16z Build:    <h4>Company</h4> … <strong>Role</strong> — <a>Team</a>
        # Classify by which side reads like a role title; the ATS URL's own
        # company slug arbitrates when neither side is convincing.
        if _looks_like_role(anchor):
            title, company = anchor, (strong if _plausible_company(strong) else "")
        elif _looks_like_role(strong):
            title, company = strong, ""
        else:
            title, company = anchor, (strong if _plausible_company(strong) else "")
        # A company-site link in the same line ("…seed for
        # <a href=westmag.com>Westmag</a> … <a href=ats>Role</a>") is both a
        # company-name candidate AND the company_url — without it the card
        # shows 'Website?' forever, because the posting url is an ATS host
        # that complete_companies rightly refuses to scrape.
        site_text = site_href = ""
        for other in (container.css("a[href]") if container is not None else []):
            if other is node:
                break
            o_href = other.attributes.get("href") or ""
            o_text = other.text(strip=True)
            if (o_href.startswith("http") and _plausible_company(o_text)
                    and not re.search(r"linkedin\.com|substack\.com|twitter\.com|x\.com",
                                      o_href)
                    and not _is_job_link(o_href, o_text)):
                site_text, site_href = o_text, o_href
        if not company and site_text:
            company = site_text
        if not company:
            heading = re.sub(r"^(open roles?|now hiring|jobs?|roles?) (at|@)\s+", "",
                             last_heading.strip(), flags=re.I)
            if _plausible_company(heading):
                company = heading
        if not company:
            company = _slug_company(url)
        if not company and title:
            # Last resort: "Datalab — Founding BizOps" packed into the anchor.
            m = re.match(r"(.{2,40}?)\s+[—–-]\s+(.{3,80})", title)
            if m:
                company, title = m.group(1).strip(), m.group(2).strip()
        if (not company or not title or company.lower() == title.lower()
                # A one-word non-role title with only a slug-derived company is
                # link debris ("Morpho" → apply-here lines), not a listing.
                or (not _looks_like_role(title) and len(title.split()) == 1)):
            continue
        stage, location, tags = _parse_parenthetical(line)
        seen_urls.add(url)
        # Only trust the site link when its text IS the chosen company —
        # a stray press link must not become the company's website.
        company_url = ""
        if site_href and site_text.lower() == company.lower():
            company_url = _clean_url(site_href)
        out.append(RawPosting(
            title=title[:120],
            company=company[:80],
            url=url,
            source=source,
            location=location,
            description=f"{line[:600]} — via {pub} ({post_title}, {posted or 'undated'})",
            stage=stage,
            industry_tags=tags[:4],
            posted_at=posted,
            company_url=company_url,
        ))
    return out


_CONTACT_HINT = re.compile(r"@[A-Za-z0-9_]{2,}|mailto:|\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b")
_LEADS_PER_POST = 20  # a16z "35+ talent moves" posts are dense but real; past
                      # this the post is a directory, not a lead list.


def extract_leads(body_html: str, pub: str, post_title: str, post_url: str,
                  posted: date | None) -> list[dict]:
    """Tier 2: hiring/founder prose with no specific job link -> queue items.

    First live backfill queued 1,111 items — hiring language alone is far too
    loose. A lead must also carry something actionable: an external link
    (LinkedIn, company site) or a contact hint (email, @handle). Boilerplate
    prose with neither is commentary, not a lead.
    """
    tree = HTMLParser(body_html)
    items: list[dict] = []
    for node in tree.css("p, li"):
        if len(items) >= _LEADS_PER_POST:
            break
        text = node.text(strip=True)
        if not (60 <= len(text) <= 1200) or not _LEAD_LANGUAGE.search(text):
            continue
        links = []
        has_job_link = False
        for a in node.css("a[href]"):
            href = a.attributes.get("href") or ""
            if _is_job_link(href, a.text(strip=True)):
                has_job_link = True
            elif href.startswith("http") and "substack.com" not in href:
                links.append(_clean_url(href))
        if has_job_link:
            continue  # Tier 1 already captured the concrete part
        if not links and not _CONTACT_HINT.search(text):
            continue  # nothing actionable — commentary, not a lead
        items.append({
            "pub": pub,
            "post_title": post_title,
            "post_url": post_url,
            "post_date": str(posted or ""),
            "text": text[:900],
            "links": links[:5],
        })
    return items


def queue_leads(items: list[dict]) -> int:
    """Append Tier-2 items to data/substack_queue.json, deduped by (post, text)."""
    if not items:
        return 0
    try:
        payload = json.loads(QUEUE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {
            "instructions": (
                "Newsletter paragraphs that mention hiring, companies worth joining, "
                "or founders worth contacting — but carry no direct job link. For each, "
                "return {kind: posting|company|person|skip, name, company, role, url, "
                "contact_hint, why, quote}. kind=person for a named individual worth "
                "contacting (goes to a review queue, never straight to People). "
                "kind=company for a company endorsement with no specific role. "
                "kind=skip for advice/noise. Write results to a JSON file mapping "
                "item index -> result and run `make apply FILE=<file>`."
            ),
            "items": [],
        }
    known = {(i.get("post_url"), i.get("text")) for i in payload["items"]}
    added = 0
    for it in items:
        if (it["post_url"], it["text"]) in known:
            continue
        payload["items"].append(it)
        added += 1
    payload["count"] = len(payload["items"])
    if added:
        tmp = QUEUE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        tmp.replace(QUEUE)
    return added


# ---------------------------------------------------------------- fetch (RSS)

_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"


def _parse_rss(xml_text: str) -> list[dict]:
    root = ElementTree.fromstring(xml_text)
    posts = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        body = item.findtext(_CONTENT_NS) or ""
        pub_date = None
        raw = item.findtext("pubDate") or ""
        if raw:
            try:
                pub_date = parsedate_to_datetime(raw).date()
            except (TypeError, ValueError):
                pub_date = None
        if link and body:
            posts.append({"url": link, "title": title, "body": body, "date": pub_date})
    return posts


def fetch(name: str, cfg: dict) -> BoardResult:
    pubs = cfg.get("pubs") or []
    seen = load_seen()
    postings: list[RawPosting] = []
    lead_count = 0
    errors: list[str] = []
    blocked = False
    with client() as c:
        for pub in pubs:
            pub_name, pub_url = pub.get("name", "?"), (pub.get("url") or "").rstrip("/")
            if not pub_url:
                continue
            try:
                r = c.get(pub_url + "/feed")
                r.raise_for_status()
                posts = _parse_rss(r.text)
            except Exception as exc:  # noqa: BLE001 — a dead pub never sinks the board
                msg, blk = classify_http_error(exc)
                errors.append(f"{pub_name}: {msg}")
                blocked = blocked or blk
                continue
            for post in posts:
                key = _clean_url(post["url"])
                if key in seen or key in _PENDING:
                    continue
                found = extract_postings(post["body"], name, pub_name,
                                         post["title"], post["url"], post["date"])
                leads = extract_leads(post["body"], pub_name, post["title"],
                                      post["url"], post["date"])
                lead_count += queue_leads(leads)
                postings.extend(found)
                _PENDING[key] = {
                    "pub": pub_name,
                    "date": str(post["date"] or ""),
                    "postings": len(found),
                    "leads": len(leads),
                }
            time.sleep(0.4)
    if lead_count:
        print(f"  substack: queued {lead_count} prose leads for extraction",
              flush=True)
    err = "; ".join(errors)
    if errors and not postings and not _PENDING:
        return BoardResult(board=name, error=err, blocked=blocked)
    return BoardResult(board=name, postings=postings, error="" if postings or _PENDING else err)


# ---------------------------------------------------------------- backfill

def _get_json(c, url: str, params: dict | None = None):
    """One throttled GET with a single slow retry — the archive walk is a
    burst of requests and Substack rate-limits bursts (verified: the same
    endpoints answer 200 solo after 429ing mid-backfill)."""
    import httpx

    time.sleep(0.4)
    for attempt in (1, 2):
        try:
            r = c.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            if attempt == 1 and exc.response.status_code in (429, 500, 502, 503):
                time.sleep(6.0)
                continue
            raise
    return None  # unreachable


def backfill(name: str, cfg: dict, months: int = 3,
             log=print) -> tuple[list[RawPosting], int, int]:
    """Walk each pub's archive API back `months`. Returns (postings, posts_done,
    paid_skipped). Seen-state is staged; caller commits after the feed is saved."""
    cutoff = date.today() - timedelta(days=months * 30)
    seen = load_seen()
    postings: list[RawPosting] = []
    done = paid = 0
    with client() as c:
        for pub in cfg.get("pubs") or []:
            pub_name, pub_url = pub.get("name", "?"), (pub.get("url") or "").rstrip("/")
            offset = 0
            while True:
                try:
                    metas = _get_json(c, f"{pub_url}/api/v1/archive",
                                      params={"sort": "new", "limit": 20, "offset": offset})
                except Exception as exc:  # noqa: BLE001
                    log(f"  {pub_name}: archive fetch failed ({type(exc).__name__}) — moving on")
                    break
                if not metas:
                    break
                # Stop only when a WHOLE page is older than the cutoff.
                # Stopping at the first old post truncated entire pubs on the
                # first live runs — Substack archives interleave pinned/older
                # posts into the sort=new stream (nextplay lost ~40 posts).
                page_has_fresh = False
                for meta in metas:
                    d = (meta.get("post_date") or "")[:10]
                    try:
                        post_day = datetime.strptime(d, "%Y-%m-%d").date()
                    except ValueError:
                        post_day = None
                    if post_day and post_day < cutoff:
                        continue
                    page_has_fresh = True
                    url = _clean_url(meta.get("canonical_url") or "")
                    if not url or url in seen or url in _PENDING:
                        continue
                    if meta.get("audience") == "only_paid":
                        paid += 1
                        _PENDING[url] = {"pub": pub_name, "date": d, "paid": True}
                        continue
                    slug = meta.get("slug") or ""
                    try:
                        body = (_get_json(c, f"{pub_url}/api/v1/posts/{slug}") or {}).get("body_html") or ""
                    except Exception as exc:  # noqa: BLE001
                        log(f"  {pub_name}/{slug}: body fetch failed ({type(exc).__name__})")
                        continue
                    found = extract_postings(body, name, pub_name,
                                             meta.get("title") or "", url, post_day)
                    leads = extract_leads(body, pub_name, meta.get("title") or "",
                                          url, post_day)
                    queue_leads(leads)
                    postings.extend(found)
                    _PENDING[url] = {"pub": pub_name, "date": d,
                                     "postings": len(found), "leads": len(leads)}
                    done += 1
                    time.sleep(0.4)
                if not page_has_fresh:
                    break
                offset += 20
            log(f"  {pub_name}: backfill through {cutoff} done")
    return postings, done, paid
