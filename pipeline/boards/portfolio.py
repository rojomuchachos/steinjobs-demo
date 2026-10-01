"""VC portfolio job boards that link out to member companies' own ATS boards.

South Park Commons and Rock Health render their listings as links to
jobs.ashbyhq.com / boards.greenhouse.io / jobs.lever.co. Rather than parse
their bespoke HTML — which changes whenever they redesign — this harvests the
ATS slugs and hands them to ats.py, which gets full descriptions from a stable
public API.

Side benefit: the same company found via two portfolios dedupes naturally,
since both resolve to the same ATS slug.
"""

from __future__ import annotations

import re

from .ats import fetch_one
from .base import BoardResult, classify_http_error, client

# Capture the company slug from an ATS URL wherever it appears in the page.
_PATTERNS = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9._-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9._-]+)", re.I)),
]

# Slugs that are paths, not companies.
_NOT_COMPANIES = {"embed", "api", "static", "assets", "www", "job", "jobs", "search"}


def harvest_slugs(html: str) -> list[tuple[str, str]]:
    """Return unique (platform, slug) pairs found in a page."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for platform, pat in _PATTERNS:
        for slug in pat.findall(html):
            key = (platform, slug)
            if slug.lower() in _NOT_COMPANIES or key in seen:
                continue
            seen.add(key)
            found.append(key)
    return found


def fetch(name: str, cfg: dict, max_companies: int = 120) -> BoardResult:
    urls = cfg.get("urls") or ([cfg["url"]] if cfg.get("url") else [])
    postings = []
    failures = 0
    discovered: list[tuple[str, str]] = []

    try:
        with client() as c:
            seen: set[tuple[str, str]] = set()
            for url in urls:
                r = c.get(url)
                r.raise_for_status()
                for pair in harvest_slugs(r.text):
                    if pair not in seen:
                        seen.add(pair)
                        discovered.append(pair)

            if not discovered:
                return BoardResult(
                    board=name,
                    error="no ATS links found — page structure may have changed",
                )

            for platform, slug in discovered[:max_companies]:
                try:
                    found = fetch_one(c, platform, slug)
                except Exception:  # noqa: BLE001 — dead/renamed boards are routine
                    failures += 1
                    continue
                for p in found:
                    p.source = name
                postings.extend(found)
    except Exception as exc:  # noqa: BLE001
        msg, blocked = classify_http_error(exc)
        return BoardResult(board=name, postings=postings, error=msg, blocked=blocked)

    n = min(len(discovered), max_companies)
    err = f"{failures}/{n} company boards unreachable" if failures else ""
    return BoardResult(board=name, postings=postings, error=err)
