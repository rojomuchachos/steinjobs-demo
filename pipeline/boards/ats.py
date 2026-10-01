"""Direct ATS APIs: Ashby, Greenhouse, Lever.

All three are public, unauthenticated, and return the *full job description* —
which is what CLAUDE.md:82 requires before scoring an ambiguous title.

This module serves two callers:
  1. The watchlist (Gravity, Journey Clinical, Mindbloom, ...) — one company each.
  2. portfolio.py, which harvests company slugs off VC portfolio pages
     (South Park Commons, Rock Health) and hands them here in bulk.

Verified live: Ashby (Doppel, 18 jobs), Greenhouse (195 jobs), Lever (13 jobs).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER = "https://api.lever.co/v0/postings/{slug}?mode=json"
# Rippling needs two calls: the board lists title/location/url only, and the
# description (plus the real posted date) lives on the per-job record. Worth
# the N+1 — without the description the scorer refuses to judge, and these
# would sit unscored forever. Found via LOVB, whose careers page embeds it.
RIPPLING = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
RIPPLING_JOB = RIPPLING + "/{uuid}"

_TAG = re.compile(r"<[^>]+>")
_ENTITY = re.compile(r"&(nbsp|amp|lt|gt|quot|#39|#x27);")
_ENTITY_MAP = {
    "nbsp": " ", "amp": "&", "lt": "<", "gt": ">",
    "quot": '"', "#39": "'", "#x27": "'",
}


def _strip_html(s: str, limit: int = 6000) -> str:
    if not s:
        return ""
    # Unescape FIRST: Greenhouse ships content entity-escaped (&lt;div&gt;), so
    # stripping tags before unescaping finds no tags and then turns the
    # entities into literal markup — which leaked raw <div> into blurbs.
    s = _ENTITY.sub(lambda m: _ENTITY_MAP.get(m.group(1), " "), s)
    s = _TAG.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _iso_to_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _ms_to_date(ms):
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date()
    except (ValueError, OSError, OverflowError):
        return None


def _fetch_ashby(c, slug, company_hint):
    jobs = (c.get(ASHBY.format(slug=slug)).raise_for_status().json() or {}).get("jobs", [])
    out = []
    for j in jobs:
        if j.get("isListed") is False:
            continue
        comp = j.get("compensation") or {}
        lo = hi = None
        for tier in comp.get("compensationTiers") or []:
            lo = tier.get("minValue") or lo
            hi = tier.get("maxValue") or hi
        out.append(
            RawPosting(
                title=j.get("title") or "",
                company=company_hint or slug,
                url=j.get("jobUrl") or j.get("applyUrl") or "",
                source="",
                location=j.get("location") or "",
                description=_strip_html(j.get("descriptionPlain") or j.get("descriptionHtml") or ""),
                comp_min=int(lo) if lo else None,
                comp_max=int(hi) if hi else None,
                remote=bool(j.get("isRemote")),
                posted_at=_iso_to_date(j.get("publishedAt")),
            )
        )
    return out


def _fetch_greenhouse(c, slug, company_hint):
    data = c.get(GREENHOUSE.format(slug=slug)).raise_for_status().json() or {}
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(
            RawPosting(
                title=j.get("title") or "",
                company=j.get("company_name") or company_hint or slug,
                url=j.get("absolute_url") or "",
                source="",
                location=loc,
                description=_strip_html(j.get("content") or ""),
                remote="remote" in loc.lower(),
                posted_at=_iso_to_date(j.get("first_published") or j.get("updated_at")),
            )
        )
    return out


def _fetch_lever(c, slug, company_hint):
    data = c.get(LEVER.format(slug=slug)).raise_for_status().json() or []
    out = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ""
        salary = j.get("salaryRange") or {}
        out.append(
            RawPosting(
                title=j.get("text") or "",
                company=company_hint or slug,
                url=j.get("hostedUrl") or j.get("applyUrl") or "",
                source="",
                location=loc,
                description=_strip_html(
                    j.get("descriptionPlain") or j.get("descriptionBodyPlain") or ""
                ),
                comp_min=salary.get("min"),
                comp_max=salary.get("max"),
                remote=(j.get("workplaceType") == "remote") or "remote" in loc.lower(),
                posted_at=_ms_to_date(j.get("createdAt")),
            )
        )
    return out


def _fetch_rippling(c, slug, company_hint):
    data = c.get(RIPPLING.format(slug=slug)).raise_for_status().json() or []
    jobs = data if isinstance(data, list) else (data.get("items") or [])
    out = []
    for j in jobs:
        uuid = j.get("uuid")
        loc = (j.get("workLocation") or {}).get("label") or ""
        desc, posted = "", None
        if uuid:
            try:
                d = c.get(RIPPLING_JOB.format(slug=slug, uuid=uuid)).raise_for_status().json()
            except Exception:  # noqa: BLE001 — one job must not sink the board
                d = {}
            body = d.get("description") or {}
            # {"company": ..., "role": ...} — role is the actual job text; company
            # is boilerplate repeated on every posting. Keep both, role first, so
            # the scorer reads the job before the mission statement.
            desc = _strip_html(" ".join(
                str(body.get(k) or "") for k in ("role", "company")))
            posted = _iso_to_date(d.get("createdOn"))
        out.append(
            RawPosting(
                title=j.get("name") or "",
                company=company_hint or slug,
                url=j.get("url") or "",
                source="",
                location=loc,
                description=desc,
                remote="remote" in loc.lower(),
                posted_at=posted,
            )
        )
    return out


_FETCHERS = {"ashby": _fetch_ashby, "greenhouse": _fetch_greenhouse,
             "lever": _fetch_lever, "rippling": _fetch_rippling}


def fetch_one(c, platform: str, slug: str, company_hint: str = "") -> list[RawPosting]:
    """Fetch a single company's board. Raises — callers decide how to handle."""
    fetcher = _FETCHERS.get(platform)
    if not fetcher:
        raise ValueError(f"unknown ATS platform: {platform}")
    return fetcher(c, slug, company_hint)


def fetch(name: str, cfg: dict) -> BoardResult:
    """Fetch many companies. cfg['companies'] = [{platform, slug, name?}, ...]

    One company 404ing (board renamed, company dead) is normal and must not
    sink the other forty, so failures are counted rather than raised.
    """
    companies = cfg.get("companies") or []
    out: list[RawPosting] = []
    failures: list[str] = []

    try:
        with client() as c:
            for entry in companies:
                platform, slug = entry.get("platform", ""), entry.get("slug", "")
                if not platform or not slug:
                    continue
                try:
                    postings = fetch_one(c, platform, slug, entry.get("name", ""))
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{slug}({type(exc).__name__})")
                    continue
                for p in postings:
                    p.source = name
                out.extend(postings)
    except Exception as exc:  # noqa: BLE001 — client-level failure
        msg, blocked = classify_http_error(exc)
        return BoardResult(board=name, postings=out, error=msg, blocked=blocked)

    err = ""
    if failures and len(failures) == len(companies):
        err = f"all {len(companies)} company boards failed"
    elif failures:
        err = f"{len(failures)}/{len(companies)} company boards unreachable"
    return BoardResult(board=name, postings=out, error=err)
