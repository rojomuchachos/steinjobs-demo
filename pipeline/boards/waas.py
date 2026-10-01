"""Work at a Startup (Y Combinator).

CLAUDE.md listed this as login-walled, and the first probe agreed — but that was
a header problem, not an auth wall. With ordinary browser headers the public
pages return 200 and ship their whole payload inline: the site is an Inertia.js
app, so `<div data-page="...">` holds URL-encoded JSON. No browser, no login,
no API key.

This is the highest-yield source on the list. Most of the roles on Eric's
hand-built shortlist came from Work at a Startup, and the pipeline was blind to
it — before this adapter the two sets had zero overlap.

Two passes, because the detail pages are dramatically richer than the list:

  1. `/jobs/l/<category>` — discovery. ~30 postings per category, no pagination
     (`?page=N` is a no-op and location/query params are ignored, both verified).
  2. `/jobs/<id>` — the real payload: full description, exact salary and equity
     ranges, stated minimum experience, real team size, and **the founders with
     their LinkedIn URLs**. That last one is the enrichment step CLAUDE.md asks
     for on every 75+ posting, free and exact, with no web search to hallucinate
     from.

The detail URL is also what Eric's own tracker used, so imported shortlist rows
and scouted rows dedupe on URL rather than falling back to title matching.
"""

from __future__ import annotations

import html as html_mod
import json
import re

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

BASE = "https://www.workatastartup.com"

# The spec's target functions, mapped to the site's own category paths.
# Engineering / Design / Recruiting / Legal / Science are deliberately omitted —
# they're hard excludes, so fetching them only to drop them wastes requests.
CATEGORIES = ["operations", "sales-manager", "marketing", "product-manager", "finance"]

_DATA_PAGE = re.compile(r'data-page="([^"]+)"')
_BATCH = re.compile(r"^([WSF])(\d{2})$")
_YEARS = re.compile(r"(\d+)")
_TAG = re.compile(r"<[^>]+>")


def _payload(page_html: str) -> dict:
    m = _DATA_PAGE.search(page_html)
    if not m:
        return {}
    try:
        return json.loads(html_mod.unescape(m.group(1))).get("props", {}) or {}
    except json.JSONDecodeError:
        return {}


def _strip_html(s: str, limit: int = 6000) -> str:
    if not s:
        return ""
    text = _TAG.sub(" ", s)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _stage_from_batch(batch: str, current_yy: int = 26) -> str:
    """A YC batch tag beats anything in the posting as a stage signal — a W26
    company is weeks old. Beyond ~3 years it says nothing useful, so say nothing."""
    m = _BATCH.match((batch or "").strip())
    if not m:
        return ""
    age = current_yy - int(m.group(2))
    if age <= 0:
        return "pre_seed"
    if age == 1:
        return "seed"
    if age <= 3:
        return "series_a"
    return ""


def _parse_salary(s: str | None) -> tuple[int | None, int | None]:
    """'$90K - $110K' / '$120,000 - $160,000'. Equity lives in its own field."""
    if not s:
        return None, None
    nums: list[int] = []
    for raw, k in re.findall(r"\$\s?([\d,]+(?:\.\d+)?)\s*([KkMm]?)", s):
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if k.lower() == "k":
            v *= 1_000
        elif k.lower() == "m":
            v *= 1_000_000
        if 10_000 <= v <= 1_000_000:
            nums.append(int(v))
    if not nums:
        return None, None
    return min(nums), (max(nums) if len(nums) > 1 else None)


def _min_years(s: str | None) -> int | None:
    """'1+ years' / 'Any (new grads ok)' -> 1 / None."""
    if not s:
        return None
    m = _YEARS.search(s)
    return int(m.group(1)) if m else None


_ROLE = re.compile(
    r"\b((?:co[\s-]?founder|founder|ceo|cto|coo|cpo|cfo|cmo|president|"
    r"head of [a-z ]{3,24}|chief [a-z ]{3,24})"
    r"(?:\s*(?:&|and|/|,)\s*(?:co[\s-]?founder|founder|ceo|cto|coo|cpo|cfo|cmo))*)",
    re.I,
)


def _founder_title(bio: str) -> str:
    """Pull an actual role out of the bio, not just its first clause.

    Bios vary: "Cofounder & COO @ Structured AI, previously IB at Greenhill"
    starts with the title, but "Gobhanu left Wharton's Huntsman Program to go all
    in on Vela" never states one. Taking the leading clause blindly turns the
    second kind into a fake title, which would then be quoted at the founder in
    an outreach email. Better to return nothing than something wrong.
    """
    m = _ROLE.search(bio or "")
    return m.group(1).strip().rstrip(",&/ ")[:80] if m else ""


def _sponsors(value: str | None) -> bool | None:
    """WaaS states this outright: 'US citizen/visa only' vs 'Will sponsor'."""
    if not value:
        return None
    v = value.lower()
    if "sponsor" in v and "not" not in v and "no " not in v:
        return True
    if "citizen" in v or "only" in v:
        return False
    return None


def _founders(company: dict) -> list[dict]:
    out = []
    for f in company.get("founders") or []:
        title = _founder_title(f.get("bio") or "")
        out.append(
            {
                "name": f.get("name", ""),
                "title": title,
                "linkedin": f.get("linkedin") or "",
                "email": "",
                "email_confidence": "none",
            }
        )
    return out


def _discover(c, categories: list[str]) -> tuple[list[int], list[str]]:
    ids: list[int] = []
    seen: set[int] = set()
    failures: list[str] = []
    for cat in categories:
        try:
            r = c.get(f"{BASE}/jobs/l/{cat}", headers={"Accept": "text/html"})
            r.raise_for_status()
            jobs = _payload(r.text).get("jobs") or []
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{cat}({type(exc).__name__})")
            continue
        if not jobs:
            failures.append(f"{cat}(empty payload)")
            continue
        for j in jobs:
            jid = j.get("id")
            if isinstance(jid, int) and jid not in seen:
                seen.add(jid)
                ids.append(jid)
    return ids, failures


def _detail(c, jid: int, name: str) -> RawPosting | None:
    r = c.get(f"{BASE}/jobs/{jid}", headers={"Accept": "text/html"})
    r.raise_for_status()
    props = _payload(r.text)
    job, company = props.get("job") or {}, props.get("company") or {}
    if not job.get("title"):
        return None

    lo, hi = _parse_salary(job.get("salaryRange"))
    equity = job.get("equityRange") or ""
    loc = job.get("location") or ""
    industry = company.get("industry") or ""

    return RawPosting(
        title=job["title"],
        company=company.get("name") or "",
        url=f"{BASE}/jobs/{jid}",
        source=name,
        location=loc,
        description=_strip_html(job.get("descriptionHtml") or ""),
        stage=_stage_from_batch(company.get("batch") or ""),
        head_count_bucket=None,  # teamSize is exact; don't fake a bucket
        industry_tags=[t.strip() for t in re.split(r"->|,", industry) if t.strip()],
        comp_min=lo,
        comp_max=hi,
        offers_equity=bool(equity),
        equity_range=equity,
        remote="remote" in loc.lower(),
        min_experience=_min_years(job.get("minExperience")),
        sponsors_visa=_sponsors(job.get("sponsorsVisa")),
        founders=_founders(company),
        company_url=company.get("url") or "",
    )


def fetch(name: str, cfg: dict, max_details: int = 400) -> BoardResult:
    categories = cfg.get("categories") or CATEGORIES
    out: list[RawPosting] = []
    detail_failures = 0

    try:
        with client() as c:
            ids, failures = _discover(c, categories)
            if not ids:
                return BoardResult(
                    board=name,
                    error=f"no jobs discovered ({', '.join(failures) or 'unknown'})",
                )
            for jid in ids[:max_details]:
                try:
                    p = _detail(c, jid, name)
                except Exception:  # noqa: BLE001 — a pulled posting is routine
                    detail_failures += 1
                    continue
                if p:
                    out.append(p)
    except Exception as exc:  # noqa: BLE001
        msg, blocked = classify_http_error(exc)
        return BoardResult(board=name, postings=out, error=msg, blocked=blocked)

    notes = list(failures)
    if detail_failures:
        notes.append(f"{detail_failures} detail fetches failed")
    return BoardResult(board=name, postings=out, error="; ".join(notes))
