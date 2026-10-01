"""Getro-powered VC job boards.

Verified live against Techstars (network 89). The search endpoint returns rich
structured data — crucially `organization.stage` and `organization.head_count`,
which cover the stage-fit (25%) and the 100+-headcount exclusion without a
model call.

Network IDs live in data/boards.yaml. To find one for a new board: fetch the
board's /jobs page and read props.pageProps.network.id out of __NEXT_DATA__.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

API = "https://api.getro.com/api/v2/collections/{network_id}/search/jobs"

# Hosts that carry postings for many employers — the domain implies no company.
_AGGREGATOR_HOSTS = (
    "linkedin.", "ashbyhq.", "greenhouse.", "lever.co", "breezy.hr", "getro.",
    "workable.", "bamboohr.", "jobvite.", "smartrecruiters.", "recruitee.",
    "notion.site", "airtable.", "teamtailor.", "rippling.", "paylocity.",
)

# The API silently caps results at 20 per page no matter what hitsPerPage says
# (verified: asking for 100 returns 20). So pagination is the only way to depth.
PAGE_SIZE = 20


def _cents_to_dollars(v: int | None) -> int | None:
    return None if v is None else int(v) // 100


def _to_date(ts: int | None):
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
    except (ValueError, OSError, OverflowError):
        return None


def _org_is_suspect(job_url: str, company: str) -> bool:
    """Getro's `organization` block is occasionally attached to the wrong job.

    Observed live: a job at jobs.scjohnson.com carrying organization
    "BOxES 4.0 Devices" (pre_seed, 2 people). That's a source data-entry error,
    not a parsing bug — but trusting it would feed a garbage stage and headcount
    straight into a rubric where stage is 25%. When the URL host clearly belongs
    to a *different* named company, the org metadata is dropped rather than
    believed. Aggregator and ATS hosts are exempt: a LinkedIn or Ashby URL says
    nothing about who the employer is.
    """
    host = re.sub(r"^https?://(www\.)?", "", job_url or "").split("/")[0].lower()
    if not host or any(a in host for a in _AGGREGATOR_HOSTS):
        return False
    stem = host.split(".")[0]
    tokens = {t for t in re.split(r"[^a-z0-9]+", (company or "").lower()) if len(t) > 3}
    if not tokens or len(stem) < 4:
        return False
    # Company and domain share nothing — e.g. "boxes 4.0 devices" vs "scjohnson".
    return not any(t in stem or stem in t for t in tokens)


def fetch(name: str, cfg: dict, max_pages: int = 12) -> BoardResult:
    network_id = cfg["network_id"]
    origin = cfg.get("origin", "https://jobs.techstars.com")
    queries = cfg.get("queries") or [""]
    # Server-side location filter — verified against the live API:
    # filters.searchable_locations ORs a city list and cut Accel's corpus from
    # 10,365 to 584 for London alone. Filtering at the source beats fetching
    # the world and discarding it here. (stage/seniority keys are accepted but
    # silently ignored — don't trust them.)
    locations = cfg.get("locations")

    out: list[RawPosting] = []
    seen: set[str] = set()

    try:
        with client() as c:
            for query in queries:
                for page in range(max_pages):
                    body = {"hitsPerPage": PAGE_SIZE, "page": page}
                    if query:
                        body["query"] = query
                    if locations:
                        body["filters"] = {"searchable_locations": locations}
                    r = c.post(
                        API.format(network_id=network_id),
                        json=body,
                        headers={"Origin": origin, "Referer": origin + "/"},
                    )
                    r.raise_for_status()
                    jobs = (r.json().get("results") or {}).get("jobs") or []
                    if not jobs:
                        break
                    for j in jobs:
                        url = (j.get("url") or "").strip()
                        if not url or url in seen:
                            continue
                        seen.add(url)
                        org = j.get("organization") or {}
                        company = org.get("name") or ""
                        suspect = _org_is_suspect(url, company)
                        out.append(
                            RawPosting(
                                title=j.get("title") or "",
                                company=company,
                                url=url,
                                source=name,
                                location=", ".join((j.get("locations") or [])[:2]),
                                stage="" if suspect else (org.get("stage") or ""),
                                head_count_bucket=None if suspect else org.get("head_count"),
                                industry_tags=[] if suspect else (org.get("industry_tags") or []),
                                comp_min=_cents_to_dollars(
                                    j.get("compensation_amount_min_cents")
                                ),
                                comp_max=_cents_to_dollars(
                                    j.get("compensation_amount_max_cents")
                                ),
                                offers_equity=j.get("compensation_offers_equity"),
                                remote=(j.get("work_mode") == "remote"),
                                posted_at=_to_date(j.get("created_at")),
                            )
                        )
                    if len(jobs) < PAGE_SIZE:
                        break
    except Exception as exc:  # noqa: BLE001 — one board must never kill the run
        msg, blocked = classify_http_error(exc)
        return BoardResult(board=name, postings=out, error=msg, blocked=blocked)

    return BoardResult(board=name, postings=out)
