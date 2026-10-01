"""`make describe` — a concise what-it-does line for every company.

The derived blurbs cover companies whose postings went through a scout with a
description attached. The gaps are systematic: shortlist imports (no scout),
Techstars rows (LinkedIn links, no descriptions), and anything found only by
name. This closes them in order of reliability:

  1. Work at a Startup company pages — the Inertia payload carries the
     company's own one-liner. Applies to every WaaS posting URL regardless of
     how it entered the feed, including the imported spreadsheet rows.
  2. The company's own site — <meta name="description"> / og:description.
     A company's self-description is exactly the concise line wanted.
  3. Whatever's left goes to data/describe_queue.json for web search
     (in-session now, API later) and comes back via `make apply-descriptions`.

Results persist in the company overlay's `description` field, which the
Companies view already prefers over derived blurbs — so a description found
once survives every future rebuild. Existing descriptions are never
overwritten; a human edit in the overlay always wins.
"""

from __future__ import annotations

import html as html_mod
import json
import re

from . import entities
from .boards.base import client
from .models import Entry

QUEUE = entities.ROOT / "data" / "describe_queue.json"

_META = re.compile(
    r'<meta[^>]+(?:name="description"|property="og:description")[^>]+content="([^"]{20,400})"',
    re.I,
)
_META_REV = re.compile(
    r'<meta[^>]+content="([^"]{20,400})"[^>]+(?:name="description"|property="og:description")',
    re.I,
)
_DATA_PAGE = re.compile(r'data-page="([^"]+)"')

MAX_LEN = 220


# Careers-page meta descriptions that describe the page, not the company.
# Harvested live: "Job board and career opportunities" (Klarna), "This is the
# homepage of the website" (Uniplaces), "Page recrutement Lydia".
_JUNK_META = re.compile(
    r"job board|career opportunit|join our team|open positions|current openings"
    r"|homepage of the website|page recrutement|we'?re hiring|browse jobs",
    re.I,
)


def _clean(s: str, max_len: int = MAX_LEN) -> str:
    s = html_mod.unescape(re.sub(r"\s+", " ", s or "").strip())
    if _JUNK_META.search(s):
        return ""
    return (s[: max_len - 1] + "…") if len(s) > max_len else s


FULL_LEN = 1400   # raw material kept for the summarizer, not for cards


def _strip_html(s: str) -> str:
    s = html_mod.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _from_waas(c, url: str) -> str:
    """Company one-liner from a Work at a Startup job or company page."""
    try:
        r = c.get(url, headers={"Accept": "text/html"})
        if r.status_code != 200:
            return ""
        m = _DATA_PAGE.search(r.text)
        if not m:
            return ""
        props = json.loads(html_mod.unescape(m.group(1))).get("props", {})
        comp = props.get("company") or {}
        for j in props.get("jobs") or []:
            if j.get("companyOneLiner"):
                return _clean(j["companyOneLiner"], FULL_LEN)
        return _clean(comp.get("description") or comp.get("oneLiner") or "", FULL_LEN)
    except Exception:  # noqa: BLE001
        return ""


def _from_site(c, site: str) -> str:
    try:
        r = c.get(site, headers={"Accept": "text/html"})
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
        m = _META.search(r.text) or _META_REV.search(r.text)
        return _clean(m.group(1), FULL_LEN) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def run(entries: list[Entry], limit: int | None = None, min_score: int = 0) -> dict:
    views = entities.companies(entries)
    missing = [
        v for v in views
        if not v.blurb and (v.best_score or 0) >= min_score and v.postings
    ]
    if limit:
        missing = missing[:limit]

    overlay = entities.load_company_overlay()
    found, queued = 0, []

    with client() as c:
        for v in missing:
            desc = ""
            waas_urls = [p["url"] for p in v.postings if "workatastartup.com" in p["url"]]
            if waas_urls:
                desc = _from_waas(c, waas_urls[0])
            if not desc and v.site:
                desc = _from_site(c, v.site)

            if desc:
                rec = overlay.get(v.key, {"name": v.name, "added": entities.today()})
                # Raw material is kept in full for the summarizer; the card
                # line only fills directly when the raw is already concise.
                rec["description_full"] = desc
                if not rec.get("description") and len(desc) <= MAX_LEN:
                    rec["description"] = desc
                    rec["description_src"] = "source"
                overlay[v.key] = rec
                found += 1
            else:
                queued.append(
                    {
                        "key": v.key,
                        "name": v.name,
                        "score": v.best_score,
                        "context": (v.postings[0]["title"] if v.postings else ""),
                        "site": v.site,
                    }
                )

    entities.save_company_overlay(overlay)

    if queued:
        QUEUE.parent.mkdir(parents=True, exist_ok=True)
        QUEUE.write_text(
            json.dumps(
                {
                    "instructions": (
                        "Write ONE concise sentence (max ~200 chars) per company: what it "
                        "does, plainly. No hype adjectives, no 'leading provider'. If it "
                        "can't be established, leave the value empty — never guess. "
                        "Return {\"<key>\": \"<sentence>\"} via "
                        "make apply-descriptions FILE=<results.json>."
                    ),
                    "count": len(queued),
                    "companies": queued,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    return {"missing": len(missing), "found": found, "queued": len(queued)}


SUMMARY_QUEUE = entities.ROOT / "data" / "summarize_queue.json"


def queue_summaries(entries: list[Entry], min_score: int = 65) -> dict:
    """Companies that deserve a written card line get a summarize job.

    Material is strictly the company's own words — the full description a
    board or the company site supplied, else the longest cached posting
    description. The summarizer condenses; it must never add facts. A
    hand-written overlay description is never queued over.
    """
    from . import store
    from .models import normalize_url

    views = entities.companies(entries)
    overlay = entities.load_company_overlay()
    descs = store.load()
    jobs = []
    for v in views:
        rec = overlay.get(v.key, {})
        if rec.get("description") and rec.get("description_src") not in ("llm", "source"):
            continue                     # a human wrote this line — keep it
        if not (v.tracked or (v.best_score or 0) >= min_score):
            continue
        material = rec.get("description_full", "")
        if not material:
            best = ""
            for pp in v.postings:
                d = descs.get(normalize_url(pp["url"]), "")
                if len(d) > len(best):
                    best = d
            material = _strip_html(best)[:FULL_LEN]
        if len(material) < 60:
            continue
        jobs.append({"key": v.key, "name": v.name, "score": v.best_score,
                     "material": material})
    jobs.sort(key=lambda j: -(j["score"] or 0))
    SUMMARY_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_QUEUE.write_text(json.dumps({
        "instructions": (
            "For each company, write 1-3 plain sentences saying what it does, "
            "condensed ONLY from `material` (the company's own words). No hype "
            "adjectives, no claims that aren't in the material. Return "
            "a JSON object mapping key to summary via "
            "make apply-descriptions FILE=<results.json>"
        ),
        "count": len(jobs),
        "companies": jobs,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"queued": len(jobs)}


def apply(results: dict[str, str]) -> int:
    """LLM/agent summaries land here. They fill blanks and refresh earlier
    auto lines, but a hand-written description is never overwritten."""
    overlay = entities.load_company_overlay()
    n = 0
    for key, desc in results.items():
        desc = _clean(desc, 400)   # 1-3 sentences get a little more room
        if not desc:
            continue
        rec = overlay.get(key, {"name": key, "added": entities.today()})
        if rec.get("description") and rec.get("description_src") not in ("llm", "source"):
            continue
        rec["description"] = desc
        rec["description_src"] = "llm"
        overlay[key] = rec
        n += 1
    entities.save_company_overlay(overlay)
    return n
