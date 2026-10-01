"""Consider platform (a16z, Greylock) — the API was never actually closed.

The 2026-07-18 probe concluded "no public API found" from the empty app-shell
HTML. The shell was the wrong place to look: the page POSTs to
`/api-boards/search-jobs` with a board id that lives in an inline
`window.serverInitialData` blob (`"fixedBoard":"andreessen-horowitz"`), and
that endpoint answers unauthenticated — 16k jobs on a16z, 2k on Greylock,
with salary, staff count, seniorities, posting timestamps and the member
company's own ATS URL (which makes cross-board dedupe free).

Caveats found probing:
  - An EMPTY query times out server-side; always send searchText.
  - `description` is null in list results. The metadata line built here is
    enough for the prefilter (staff count, seniority, min years are explicit
    fields); the real text arrives via Repopulate Job, whose fetch of the
    ATS `url` usually succeeds because it's Greenhouse/Lever/Ashby.
"""

from __future__ import annotations

from datetime import datetime

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

# companyStaffCount arrives as a number; map onto the getro-style buckets the
# prefilter already reasons about (4+ = playbook written).
_BUCKET_EDGES = [(10, 1), (50, 2), (200, 3), (500, 4), (1000, 5)]


def _bucket(staff) -> int | None:
    try:
        n = int(staff)
    except (TypeError, ValueError):
        return None
    for edge, bucket in _BUCKET_EDGES:
        if n <= edge:
            return bucket
    return 6


def _posted(ts: str):
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _salary(sal: dict) -> tuple[int | None, int | None]:
    if not isinstance(sal, dict):
        return None, None
    if (sal.get("period") or {}).get("value") != "year":
        return None, None
    if (sal.get("currency") or {}).get("value") not in ("USD", "EUR"):
        return None, None
    lo, hi = sal.get("minValue"), sal.get("maxValue")
    return (int(lo) if lo else None), (int(hi) if hi else None)


def fetch(name: str, cfg: dict) -> BoardResult:
    host = cfg["host"]
    board_id = cfg["board_id"]
    size = int(cfg.get("size", 100))
    out: list[RawPosting] = []
    seen: set[str] = set()
    errors = []
    with client() as c:
        for term in cfg.get("queries") or [""]:
            try:
                r = c.post(
                    f"https://{host}/api-boards/search-jobs",
                    json={
                        "meta": {"size": size, "from": 0},
                        "board": {"id": board_id, "isParent": True},
                        "query": {"promoteFeatured": True, "searchText": term},
                    },
                )
                r.raise_for_status()
                jobs = r.json().get("jobs") or []
            except Exception as exc:  # noqa: BLE001 — one term must not kill the board
                msg, blocked = classify_http_error(exc)
                errors.append(f"{term}: {msg}")
                if blocked:
                    return BoardResult(board=name, postings=out, error=msg, blocked=True)
                continue
            for j in jobs:
                url = j.get("url") or j.get("applyUrl") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                comp_min, comp_max = _salary(j.get("salary"))
                def _labels(v):
                    # facets arrive as strings OR {label,value} dicts
                    return ", ".join(
                        (x.get("label") or x.get("value") or "") if isinstance(x, dict) else str(x)
                        for x in (v or []))
                meta = " · ".join(x for x in (
                    _labels(j.get("jobFunctions")),
                    _labels(j.get("jobSeniorities")),
                    _labels((j.get("markets") or [])[:3]),
                    f"team ~{j.get('companyStaffCount')}" if j.get("companyStaffCount") else "",
                    f"min {j.get('minYearsExp')}y exp" if j.get("minYearsExp") else "",
                ) if x)
                out.append(RawPosting(
                    title=j.get("title") or "",
                    company=j.get("companyName") or "",
                    url=url,
                    source=name,
                    location=_labels((j.get("normalizedLocations") or j.get("locations") or [])[:2]),
                    description=f"[board metadata — Repopulate Job pulls the real text] {meta}" if meta else "",
                    head_count_bucket=_bucket(j.get("companyStaffCount")),
                    posted_at=_posted(j.get("timeStamp")),
                    comp_min=comp_min,
                    comp_max=comp_max,
                ))
    err = "; ".join(errors[:3]) if errors else None
    return BoardResult(board=name, postings=out, error=err)
