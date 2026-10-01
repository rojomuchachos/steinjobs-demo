"""Built In (NYC) — the JSON API is broken server-side, but the HTML isn't.

`/jobs` renders complete job cards server-side (probe 2026-08-05: 40 cards,
200 OK with a plain browser UA): job id + title from the `/job/...` link,
company from the `/company/<slug>` link's logo alt text, and the card body
carries location/salary text. Detail pages are also server-rendered inside a
`job-post-item` div, so descriptions come from a capped second pass — spent
only on cards that survive an obvious-seniority screen, because 40 detail
fetches per query would be most of a scout's runtime.
"""

from __future__ import annotations

import html as _html
import re

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

# the closing quote matters: data-id="job-card-title" would split cards in half
_CARD_SPLIT = re.compile(r'data-id="job-card"')
_JOB_LINK = re.compile(r'href="(/job/[^"]+/(\d+))"[^>]*>(?:<[^>]+>)*([^<]{3,120})<')
_CO_LINK = re.compile(r'href="/company/([a-z0-9-]+)"')
_CO_ALT = re.compile(r'alt="([^"]+?)(?:\s+Logo)?"', re.I)
_LOC = re.compile(r'<i class="fa-regular fa-location-dot[^>]*></i>\s*<span[^>]*>([^<]+)<')
_DESC_BLOCK = re.compile(r'<div class="job-post-item[^"]*"[^>]*>(.*?)</div>\s*<div class="(?:d-none|bg-)', re.S)
_TAG = re.compile(r"<[^>]+>")

# Cheap title screen so detail fetches aren't wasted on obvious prefilter
# kills — the real prefilter still runs on everything returned.
_SKIP_TITLE = re.compile(
    r"senior|staff|principal|director|vp |vice president|intern|attorney|"
    r"counsel|nurse|physician|accountant|controller", re.I)

MAX_DETAILS = 30


def _card(chunk: str, origin: str) -> RawPosting | None:
    m = _JOB_LINK.search(chunk)
    if not m:
        return None
    path, _jid, title = m.groups()
    co = ""
    cm = _CO_LINK.search(chunk)
    if cm:
        am = _CO_ALT.search(chunk)
        co = _html.unescape(am.group(1)) if am else cm.group(1).replace("-", " ").title()
    lm = _LOC.search(chunk)
    return RawPosting(
        title=_html.unescape(title).strip(),
        company=co.strip(),
        url=origin + path,
        source="builtinnyc",
        location=_html.unescape(lm.group(1)).strip() if lm else "New York, NY",
    )


def fetch(name: str, cfg: dict) -> BoardResult:
    origin = cfg.get("origin", "https://builtinnyc.com")
    days = cfg.get("days_since_updated", 7)
    out: list[RawPosting] = []
    seen: set[str] = set()
    errors = []
    try:
        with client() as c:
            for term in cfg.get("queries") or [""]:
                q = f"&search={term.replace(' ', '+')}" if term else ""
                r = c.get(f"{origin}/jobs?daysSinceUpdated={days}{q}")
                r.raise_for_status()
                for chunk in _CARD_SPLIT.split(r.text)[1:]:
                    p = _card(chunk[:4000], origin)
                    if p and p.url not in seen:
                        seen.add(p.url)
                        out.append(p)
            # capped description pass, screened titles only
            fetched = 0
            for p in out:
                if fetched >= MAX_DETAILS or _SKIP_TITLE.search(p.title):
                    continue
                fetched += 1
                try:
                    r = c.get(p.url)
                    r.raise_for_status()
                    m = _DESC_BLOCK.search(r.text)
                    if m:
                        txt = _html.unescape(_TAG.sub(" ", m.group(1)))
                        p.description = re.sub(r"\s+", " ", txt).strip()[:8000]
                except Exception:  # noqa: BLE001 — a dead detail page just skips
                    continue
    except Exception as exc:  # noqa: BLE001
        msg, blocked = classify_http_error(exc)
        return BoardResult(board=name, postings=out, error=msg, blocked=blocked)
    return BoardResult(board=name, postings=out,
                       error="; ".join(errors[:2]) if errors else None)
