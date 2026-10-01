"""Climatebase — the ag/food/climate vertical the other boards don't carry.

Eric's ask (2026-08-04): tech companies whose PRODUCT serves agriculture,
food, or farming — not farms that happen to employ technologists. No active
board covers that: generalist is GTM-curated, WaaS is YC, the Getro boards
are generalist EU VC portfolios. Climatebase is the de facto climate jobs
board and tags every job with sectors, including "Food, Agriculture, & Land
Use" — which is precisely the product-serves-agriculture cut.

Platform reality (probed 2026-08-04):
  - /jobs ships the newest ~100 jobs inline in __NEXT_DATA__ (same pattern
    as Work at a Startup): title, employer, sectors, locations, salary range,
    activation_date (a real posting date), remote preference, a one-line
    employer description.
  - The ?q=, ?sectors= and ?page= params are all IGNORED server-side — every
    request returns the same newest-100. So this adapter is a rolling sweep:
    one request per run, sector-filtered client-side, and the feed's dedupe
    turns daily pulls into cumulative coverage.
  - /job/<id> detail pages 403 behind Cloudflare, so no full descriptions.
    The stored URL still opens fine in a real browser.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

URL = "https://climatebase.org/jobs"

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

# The sector tag that means "product serves agriculture / food / land".
DEFAULT_SECTORS = ["Food, Agriculture, & Land Use"]


def _posted(job: dict):
    raw = (job.get("activation_date") or "")[:10]
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _salary(job: dict) -> tuple[int | None, int | None]:
    if (job.get("salary_period") or "yearly") != "yearly":
        return None, None
    try:
        lo, hi = int(job.get("salary_from") or 0), int(job.get("salary_to") or 0)
        return (lo or None), (hi or None)
    except (TypeError, ValueError):
        return None, None


def fetch(name: str, cfg: dict) -> BoardResult:
    sectors = set(cfg.get("sectors") or DEFAULT_SECTORS)
    out: list[RawPosting] = []
    try:
        with client() as c:
            r = c.get(URL)
            r.raise_for_status()
            m = _NEXT_DATA.search(r.text)
            if not m:
                return BoardResult(board=name, postings=[],
                                   error="__NEXT_DATA__ missing — page layout changed")
            jobs = json.loads(m.group(1))["props"]["pageProps"].get("jobs") or []
    except Exception as exc:  # noqa: BLE001
        msg, blocked = classify_http_error(exc)
        return BoardResult(board=name, postings=[], error=msg, blocked=blocked)

    for j in jobs:
        if not sectors & set(j.get("sectors") or []):
            continue
        comp_min, comp_max = _salary(j)
        blurb = (j.get("employer_short_description") or "").strip()
        desc = " ".join(x for x in (
            blurb,
            "Sectors: " + ", ".join(j.get("sectors") or []) + ".",
            "Remote: " + ", ".join(j.get("remote_preferences") or []) + "."
            if j.get("remote_preferences") else "",
        ) if x)
        out.append(RawPosting(
            title=j.get("title") or "",
            company=j.get("name_of_employer") or "",
            url=f"https://climatebase.org/job/{j.get('id')}",
            source=name,
            location=", ".join((j.get("locations") or [])[:2]),
            description=desc,
            industry_tags=["agriculture", "food-systems"],
            posted_at=_posted(j),
            comp_min=comp_min,
            comp_max=comp_max,
        ))
    return BoardResult(board=name, postings=out)
