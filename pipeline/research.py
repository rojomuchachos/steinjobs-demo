"""Company research: fill missing descriptions/industry/site by looking, not guessing.

Companies arrive from newsletters, prose extraction, and boards with no
self-description anywhere on file. The honesty rule forbids generating one
from vibes — but researching one is fine: an API call with the web_search
tool, seeded with the disambiguating context we already hold (ATS board slug,
role titles, the newsletter's own sentence), returning empty when it cannot
identify the company confidently. Wrong-company facts are worse than blanks —
the prompt says so and the caller writes blanks-only.

Runs automatically after every scout (bounded, like complete_companies) and
on backfill; `auto_fill` is also callable for one-time drains.

Results land in the company overlay (description / industry / site), which
the Companies view prefers over derived blurbs — found once, kept forever.
Overlay industry must be one of entities._INDUSTRIES labels or empty.
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from . import entities

MODEL = "claude-haiku-4-5"
MAX_CONCURRENCY = 6
AUTO_CAP = 25  # per scout run, same spirit as complete_companies

_LABELS = [label for label, _ in entities._INDUSTRIES]

_SYSTEM = f"""You research early-stage startups for a job-search pipeline.
Given a company name plus context (job titles, an ATS board URL, a newsletter
sentence), use web search to find what THE SAME company does.

Respond with ONLY a JSON object:
{{"description": "<one factual sentence, <=200 chars, from their site or coverage>",
  "industry": "<one of: {", ".join(_LABELS)} — or empty>",
  "website": "<official homepage URL or empty>"}}

Identity discipline: the context must match (ATS slug, role, sector). Many
startups share names — if you are not confident you found the SAME company,
return all fields empty. Never describe a different company with the same
name. Never invent: empty beats plausible."""


def _context_for(view, entries_by_key: dict) -> str:
    bits = [f"Company: {view.name}"]
    if view.site:
        bits.append(f"Known website: {view.site}")
    rows = entries_by_key.get(view.key, [])
    titles = ", ".join(sorted({e.title for e in rows})[:4])
    if titles:
        bits.append(f"Open roles: {titles}")
    for e in rows:
        if "ashbyhq.com" in e.url or "greenhouse.io" in e.url or "lever.co" in e.url:
            bits.append(f"ATS board: {e.url}")
            break
    for e in rows:
        if e.why or "via" in (view.blurb or ""):
            break
    snippets = [x for x in (view.blurb, view.why) if x]
    if snippets:
        bits.append(f"What we have on file: {snippets[0][:220]}")
    if view.stage:
        bits.append(f"Stage: {view.stage}")
    if view.locations:
        bits.append(f"Location: {', '.join(view.locations[:2])}")
    return "\n".join(bits)


def _parse(text: str) -> dict:
    # Web-search answers arrive with <cite index="…"> markers inline —
    # citation plumbing, not content. Strip tags before anything else.
    text = re.sub(r"<[^>]+>", "", text or "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except (ValueError, TypeError):
        return {}
    out = {}
    desc = str(d.get("description") or "").strip()
    if 20 <= len(desc) <= 300:
        out["description"] = desc
    ind = str(d.get("industry") or "").strip().lower()
    if ind in _LABELS:
        out["industry"] = ind
    site = str(d.get("website") or "").strip()
    if site.startswith("http") and "linkedin.com" not in site:
        out["site"] = site.rstrip("/")
    return out


_SITE_SYSTEM = f"""You summarize startups from their own homepage text for a
job-search pipeline. Given the text of a company's website plus context,
respond with ONLY a JSON object:
{{"description": "<one factual sentence, <=200 chars, drawn from the page>",
  "industry": "<one of: {", ".join(_LABELS)} — or empty>",
  "website": ""}}
If the page text does not plausibly belong to the named company (parked
domain, unrelated product), return all fields empty. Never invent."""


def _site_text(c, url: str, limit: int = 5000) -> str:
    """Homepage text, tags stripped — free, and the company's own words."""
    import html as _h

    try:
        r = c.get(url)
        r.raise_for_status()
    except Exception:  # noqa: BLE001
        return ""
    t = re.sub(r"(?is)<(script|style|nav|footer|svg)[^>]*>.*?</\1>", " ", r.text)
    t = _h.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()[:limit]


def research(views: list, entries_by_key: dict, model: str = MODEL) -> dict[str, dict]:
    """view.key -> {description, industry, site}, cheapest source first:
    a company with a site on file gets a plain summarize call over its own
    homepage text (~50x cheaper than search); web search (one use) is the
    fallback for companies with no site anywhere. Empty results (couldn't
    identify the company) simply don't appear."""
    import anthropic

    from .boards.base import client as _http_client

    client = anthropic.Anthropic()
    http = _http_client()  # shared httpx client; thread-safe for reads

    def one(view) -> tuple[str, dict | None]:
        try:
            page = _site_text(http, view.site) if view.site else ""
            if len(page) >= 300:
                resp = client.messages.create(
                    model=model, max_tokens=350,
                    system=[{"type": "text", "text": _SITE_SYSTEM,
                             "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content":
                               f"{_context_for(view, entries_by_key)}\n\n"
                               f"Homepage text:\n{page}"}],
                )
                text = " ".join(b.text for b in resp.content
                                if getattr(b, "type", "") == "text")
                out = _parse(text)
                if out:
                    return view.key, out
                # fall through to search only if the page didn't identify it
            resp = client.messages.create(
                model=model, max_tokens=700,
                system=[{"type": "text", "text": _SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[{"type": "web_search_20250305", "name": "web_search",
                        "max_uses": 1}],
                messages=[{"role": "user",
                           "content": _context_for(view, entries_by_key)}],
            )
            text = " ".join(b.text for b in resp.content
                            if getattr(b, "type", "") == "text")
            return view.key, _parse(text)
        except Exception as exc:  # noqa: BLE001 — one company must not kill the run
            # None = the call ERRORED (billing, rate limit): distinct from an
            # answered-empty, so the caller neither stamps nor gives up on it.
            errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
            return view.key, None
    errors: list[str] = []
    try:
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
            results = dict(pool.map(one, views))
    finally:
        http.close()
    if errors and len(errors) == len(views):
        # Every call failing is one systemic problem (a failed run once
        # stamped 303 companies as researched on an empty credit balance).
        print(f"  company research: ALL calls failed — first error: {errors[0]}",
              file=sys.stderr)
    answered = {k: v for k, v in results.items() if v is not None}
    return {"answered": answered, "found": {k: v for k, v in answered.items() if v}}


def auto_fill(entries: list, cap: int | None = AUTO_CAP) -> dict | None:
    """Research companies with no usable description; write blanks-only into
    the overlay. Returns counts, or None when there's no key / nothing to do."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from datetime import date, timedelta

    views = entities.companies(entries)
    overlay0 = entities.load_company_overlay()
    retry_before = (date.today() - timedelta(days=30)).isoformat()
    blank = [c for c in views
             if not (c.blurb and len(c.blurb) > 40)
             and c.status not in ("uninterested", "rejected")
             # Score gate: a 40-scoring SDR posting's company doesn't need a
             # dossier. Research spends only where a posting cleared 60, or
             # where Eric tracks the company by hand. Unscored (None) passes:
             # those companies are unscored BECAUSE no description exists —
             # reading None as 0 locked them out of the very pass that fixes
             # it, and 400+ blank company cards accumulated (2026-08-13).
             and (c.best_score is None or c.best_score >= 60 or c.tracked)
             # A company research couldn't identify stays skipped for 30 days —
             # without this stamp the same unidentifiable names re-billed a web
             # search on every scout, forever.
             and (overlay0.get(c.key, {}).get("research_checked") or "") < retry_before]
    if not blank:
        return None
    batch = blank[:cap] if cap else blank
    by_key: dict[str, list] = {}
    for e in entries:
        from .models import normalize_company

        by_key.setdefault(normalize_company(e.company), []).append(e)
    print(f"  researching {len(batch)} companies via web search…", file=sys.stderr)
    r = research(batch, by_key)
    answered, found = r["answered"], r["found"]

    overlay = entities.load_company_overlay()
    names = {c.key: c.name for c in batch}
    filled = {"description": 0, "industry": 0, "site": 0}
    for key, res in found.items():
        rec = overlay.setdefault(key, {"name": names.get(key, key)})
        for field in ("description", "industry", "site"):
            if res.get(field) and not rec.get(field):
                rec[field] = res[field]
                filled[field] += 1
    # Stamp only companies whose call ANSWERED (even answered-empty). An
    # errored call — billing, rate limit — must stay eligible for retry.
    today_iso = date.today().isoformat()
    for c in batch:
        if c.key in answered:
            overlay.setdefault(c.key, {"name": c.name})["research_checked"] = today_iso
    entities.save_company_overlay(overlay)
    filled["researched"] = len(batch)
    filled["identified"] = len(found)
    filled["errored"] = len(batch) - len(answered)
    return filled


# --- founders ---------------------------------------------------------------
#
# The bottleneck the signals work ran into: 1,208 of 1,344 companies have no
# human to write to, and applying cold is the weakest move in the whole
# pipeline. Measured 2026-08-07, the free sources do not close it — SEC Form D
# resolved 1 of 12 (early-stage raises are SAFEs, which don't file) and the
# /about + /team scraper found 0 founders across 67 companies. Search does,
# because it reads the coverage and the team page rather than guessing a URL.
#
# Same discipline as the description pass above: identity guard, blanks-only
# writes, bounded per run, and a stamp so an unidentifiable company isn't
# re-billed on every scout.

_FOUNDER_SYSTEM = """You find the founders of early-stage startups for a
job-search pipeline. Given a company name plus context (what it does, its
website, open roles, an ATS board URL), use web search to find who founded
THE SAME company.

Respond with ONLY a JSON object:
{"founders": [{"name": "<full name>", "title": "<Co-founder & CEO, CTO, …>",
               "linkedin": "<https://www.linkedin.com/in/… or empty>"}]}

At most 4 people, CEO first. Only people described as a founder, co-founder,
or the chief executive — not early employees, not investors, not advisors.

Identity discipline, and this is the whole job: many startups share a name.
The context must match — sector, website, location. If you are not confident
you found the SAME company, return {"founders": []}. A wrong founder name is
far worse than none: Eric emails these people by name. Never guess a LinkedIn
URL from a person's name; leave it empty unless you actually saw it."""

_LI_RE = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/?#]+", re.I)


def _parse_founders(text: str) -> list[dict]:
    text = re.sub(r"<[^>]+>", "", text or "")          # strip citation markers
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    out = []
    for f in (d.get("founders") or [])[:4]:
        name = str(f.get("name") or "").strip()
        # A name is two words minimum; "Founder" and "The team" are not people.
        if not name or len(name.split()) < 2 or len(name) > 60:
            continue
        title = str(f.get("title") or "").strip()[:60]
        li = str(f.get("linkedin") or "").strip().rstrip("/")
        out.append({"name": name, "title": title,
                    "linkedin": li if _LI_RE.match(li) else "",
                    "email": "", "email_confidence": "none"})
    return out


def research_founders(views: list, entries_by_key: dict,
                      model: str = MODEL) -> dict[str, list[dict] | None]:
    """view.key -> [founder dicts], or None when the call itself errored."""
    import anthropic

    client = anthropic.Anthropic()
    errors: list[str] = []

    def one(view):
        try:
            resp = client.messages.create(
                model=model, max_tokens=900,
                system=[{"type": "text", "text": _FOUNDER_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[{"type": "web_search_20250305", "name": "web_search",
                        "max_uses": 2}],
                messages=[{"role": "user",
                           "content": _context_for(view, entries_by_key)}],
            )
            text = " ".join(b.text for b in resp.content
                            if getattr(b, "type", "") == "text")
            return view.key, _parse_founders(text)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
            return view.key, None

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        results = dict(pool.map(one, views))
    if errors and len(errors) == len(views):
        print(f"  founder research: ALL calls failed — first: {errors[0]}",
              file=sys.stderr)
    return results


def auto_fill_founders(entries: list, cap: int | None = 15,
                       saved_only: bool = True) -> dict | None:
    """Find founders for SAVED companies that have none, write them onto the
    feed (blanks-only), and queue the new founders for a LinkedIn profile fill.

    Saved-only by Eric's call (2026-08-07): a Chrome sitting and a web search
    are both scarcer than an HTTP request, and he'd rather spend them on
    companies he has already chosen.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from datetime import date, timedelta

    from .models import normalize_company

    overlay0 = entities.load_company_overlay()
    tracked = {k for k, v in overlay0.items() if v.get("tracked")}
    live = {normalize_company(e.company) for e in entries
            if e.status in ("saved", "applied", "interviewing", "offer")}
    wanted = tracked | live

    retry_before = (date.today() - timedelta(days=30)).isoformat()
    views = [c for c in entities.companies(entries)
             if not c.founders
             and c.status not in ("uninterested", "rejected")
             and (c.key in wanted if saved_only else True)
             and (overlay0.get(c.key, {}).get("founders_checked") or "") < retry_before]
    if not views:
        return None
    batch = views[:cap] if cap else views

    by_key: dict[str, list] = {}
    for e in entries:
        by_key.setdefault(normalize_company(e.company), []).append(e)

    print(f"  researching founders for {len(batch)} saved companies…", file=sys.stderr)
    results = research_founders(batch, by_key)

    named = queued = 0
    for view in batch:
        found = results.get(view.key)
        if not found:
            continue
        for e in by_key.get(view.key, []):
            if not e.founders:
                e.founders = [dict(f) for f in found]
        named += len(found)
        # The point of finding them: their profiles become readable, which is
        # where every connection signal comes from.
        for f in found:
            if f.get("linkedin") and entities.queue_person_fill(
                    {"linkedin": f["linkedin"], "name": f["name"],
                     "role": f.get("title", ""), "company": view.name}):
                queued += 1

    overlay = entities.load_company_overlay()
    today_iso = date.today().isoformat()
    for view in batch:
        if results.get(view.key) is not None:      # answered, even if empty
            overlay.setdefault(view.key, {"name": view.name})["founders_checked"] = today_iso
    entities.save_company_overlay(overlay)
    return {"companies": len(batch), "founders": named, "queued_fills": queued,
            "identified": sum(1 for v in batch if results.get(v.key))}
