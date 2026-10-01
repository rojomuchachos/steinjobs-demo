"""TeamWork Online — "JS-rendered" was wrong; the cards ship in the HTML.

The July probe searched for `/jobs/` hrefs and found none, concluding the
search was client-rendered. The 2026-08-05 browser sniff proved the page
makes NO data request at all — every `browse-jobs-card` is server-rendered;
the job links just live under property paths like
`/multiple-properties/<org>/<team>-jobs/<slug>-<id>` and the query needs a
`commit=Search` param before the backend runs it (without it: "No matches").

Cards carry title, organization, city, employment type, and posted-age
("2 hours ago" scoreboard). No description on the list page; detail pages
are server-rendered too, so a capped detail pass fills the top of the run.
"""

from __future__ import annotations

import html as _html
import re
from datetime import date, timedelta

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

ORIGIN = "https://www.teamworkonline.com"

_CARD = re.compile(
    r'<a class="browse-jobs-card__content--title" href="(?P<path>[^"]+)">\s*'
    r'<div[^>]*>(?P<title>[^<]+)</div></a>'
    r'<div class="browse-jobs-card__content--organization">(?P<org>[^<]*)</div>'
    r'(?P<tail>.{0,600}?)</div></div></div>', re.S)
_CITY = re.compile(r'trending__content--small">([^<]{2,60})<')
_AGO = re.compile(r'scoreboard">(\d+)</div><div class="browse-jobs-card__scoreboard">(\d+)</div>'
                  r'<div[^>]*>\s*(hour|day|week|month)s? ago')
_DESC = re.compile(r'<div class="opportunity-preview__body[^"]*"[^>]*>(.*?)</div>\s*<(?:div|section) ', re.S)
_TAG = re.compile(r"<[^>]+>")

_SKIP_TITLE = re.compile(r"part.time|seasonal|intern|game.?night|usher|retail|"
                         r"security|janitor|senior|director|vp ", re.I)
MAX_DETAILS = 20


def _age_to_date(tail: str):
    m = _AGO.search(tail)
    if not m:
        return None
    tens, ones, unit = m.groups()
    n = int(tens) * 10 + int(ones)
    days = {"hour": 0, "day": n, "week": n * 7, "month": n * 30}[unit] if unit != "hour" else 0
    return date.today() - timedelta(days=days)


def fetch(name: str, cfg: dict) -> BoardResult:
    out: list[RawPosting] = []
    seen: set[str] = set()
    try:
        with client() as c:
            for term in cfg.get("queries") or ["marketing"]:
                q = term.replace(" ", "+")
                r = c.get(f"{ORIGIN}/jobs-in-sports?employment_opportunity_search"
                          f"%5Bquery%5D={q}&commit=Search")
                r.raise_for_status()
                for m in _CARD.finditer(r.text):
                    path = m.group("path")
                    if path in seen:
                        continue
                    seen.add(path)
                    tail = m.group("tail")
                    city = _CITY.search(tail)
                    out.append(RawPosting(
                        title=_html.unescape(m.group("title")).strip(),
                        company=_html.unescape(m.group("org")).strip(),
                        url=ORIGIN + path,
                        source=name,
                        location=_html.unescape(city.group(1)).strip().replace(" · ", ", ") if city else "",
                        posted_at=_age_to_date(tail),
                    ))
            fetched = 0
            for p in out:
                if fetched >= MAX_DETAILS or _SKIP_TITLE.search(p.title):
                    continue
                fetched += 1
                try:
                    r = c.get(p.url)
                    r.raise_for_status()
                    m = _DESC.search(r.text)
                    if m:
                        txt = _html.unescape(_TAG.sub(" ", m.group(1)))
                        p.description = re.sub(r"\s+", " ", txt).strip()[:8000]
                except Exception:  # noqa: BLE001 — a dead detail page just skips
                    continue
    except Exception as exc:  # noqa: BLE001
        msg, blocked = classify_http_error(exc)
        return BoardResult(board=name, postings=out, error=msg, blocked=blocked)
    return BoardResult(board=name, postings=out)
