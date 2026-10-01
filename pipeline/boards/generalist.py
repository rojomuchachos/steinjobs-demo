"""generalist.world/jobs.

Listed as blocked because the first probe hit a 301 — the URL needs its trailing
slash. With that, the whole board is one request: the listing page renders every
posting as a `gw-job-card` carrying company, title, a real description, tags,
salary band and location inline. No pagination, no detail fetch, ~315 postings.

Curated for exactly the audience CLAUDE.md describes — the cards read "First
sales hire at...", "You'd be the first growth hire..." — so the hit rate here is
far higher than the volume-driven boards.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

ORIGIN = "https://generalist.world"
URL = f"{ORIGIN}/jobs/"

_MONEY = re.compile(r"\$\s?([\d,]+)")


def _text(node, sel: str) -> str:
    n = node.css_first(sel)
    return n.text(strip=True) if n else ""


def _parse_salary(s: str) -> tuple[int | None, int | None]:
    nums = []
    for raw in _MONEY.findall(s or ""):
        try:
            v = int(raw.replace(",", ""))
        except ValueError:
            continue
        if 10_000 <= v <= 1_000_000:
            nums.append(v)
    if not nums:
        return None, None
    return min(nums), (max(nums) if len(nums) > 1 else None)


def _parse_card(card, origin: str, source: str) -> RawPosting | None:
    title = _text(card, ".gw-job-title")
    company = _text(card, ".gw-job-company")
    if not title or not company:
        return None

    href = card.attributes.get("href", "")
    # href is site-absolute ("/jobs/<slug>/"), so join against the ORIGIN —
    # joining against the listing URL doubles the /jobs/ segment.
    url = href if href.startswith("http") else ORIGIN + href

    salary = location = ""
    tags: list[str] = []
    for tag in card.css(".gw-job-meta-tag"):
        classes = tag.attributes.get("class", "")
        text = tag.text(strip=True)
        if "gw-salary" in classes:
            salary = text
        elif "gw-location" in classes:
            location = text
        elif text:
            tags.append(text)

    lo, hi = _parse_salary(salary)
    desc = _text(card, ".gw-job-description")
    # The "weird tag" is the board's own editorial one-liner about the company —
    # genuinely useful context for industry fit, so keep it with the description.
    weird = _text(card, ".gw-job-weird-tag")
    if weird:
        desc = f"[{weird}] {desc}"

    return RawPosting(
        title=title,
        company=company,
        url=url,
        source=source,
        location=location,
        description=desc,
        industry_tags=tags,
        comp_min=lo,
        comp_max=hi,
        remote="remote" in (location + " " + desc).lower(),
    )


def fetch(name: str, cfg: dict) -> BoardResult:
    url = cfg.get("url") or URL
    try:
        with client() as c:
            r = c.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
            r.raise_for_status()
            tree = HTMLParser(r.text)
    except Exception as exc:  # noqa: BLE001
        msg, blocked = classify_http_error(exc)
        return BoardResult(board=name, error=msg, blocked=blocked)

    cards = tree.css("a.gw-job-card")
    if not cards:
        return BoardResult(
            board=name,
            error="no .gw-job-card elements — the page markup has changed",
        )

    out, seen = [], set()
    for card in cards:
        p = _parse_card(card, ORIGIN, name)
        if p and p.url not in seen:
            seen.add(p.url)
            out.append(p)

    return BoardResult(board=name, postings=out)
