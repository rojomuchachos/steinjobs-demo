"""landing.jobs — Lisbon-centred European tech board.

Public JSON API, no auth. Two honest caveats, both verified:

  - the `page` parameter is ignored (page 2 returns page 1), so ~50 jobs is
    the whole visible surface
  - the board skews hard to engineering, which the prefilter excludes

Kept anyway because it costs a single request and carries two fields most
boards don't: `relocation_paid` — directly relevant since Eric needs
sponsorship/relocation for Europe — and a real `published_at` date, so the
recency rules run on stated dates rather than first_seen fallback.
"""

from __future__ import annotations

import re
from datetime import datetime

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

API = "https://landing.jobs/api/v1/jobs"
_TAG = re.compile(r"<[^>]+>")


def _strip(s: str, limit: int = 4000) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", s or "")).strip()[:limit]


def _company_from_url(url: str) -> str:
    # https://landing.jobs/at/<company-slug>/<job-slug> — the slug is all we get.
    m = re.search(r"/at/([^/]+)/", url or "")
    return m.group(1).replace("-", " ").title() if m else ""


def _date(s: str):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def fetch(name: str, cfg: dict) -> BoardResult:
    try:
        with client() as c:
            r = c.get(API, headers={"Accept": "application/json"})
            r.raise_for_status()
            jobs = r.json() or []
    except Exception as exc:  # noqa: BLE001
        msg, blocked = classify_http_error(exc)
        return BoardResult(board=name, error=msg, blocked=blocked)

    out = []
    for j in jobs:
        url = j.get("url") or ""
        if not url or not j.get("title"):
            continue
        locs = j.get("locations") or []
        location = ", ".join(str(x) for x in locs[:2]) if locs else "Portugal"
        desc = _strip(f"{j.get('role_description', '')} {j.get('main_requirements', '')}")
        if j.get("relocation_paid"):
            desc = "[relocation paid] " + desc
        out.append(
            RawPosting(
                title=j["title"],
                company=_company_from_url(url),
                url=url,
                source=name,
                location=location,
                description=desc,
                comp_min=j.get("gross_salary_low") or None,
                comp_max=j.get("gross_salary_high") or None,
                remote=bool(j.get("remote")),
                posted_at=_date(j.get("published_at")),
                industry_tags=[t for t in (j.get("tags") or [])[:6] if isinstance(t, str)],
                # relocation_paid isn't visa sponsorship, but it's the closest
                # signal this board has; leave sponsors_visa unset rather than
                # claiming more than the field says.
            )
        )
    return BoardResult(board=name, postings=out)
