"""Sidecar cache for captured LinkedIn profile text.

The person record carries `role` and `notes`, and for a long time those were the
only text connection signals were matched against. That is one line and one
hand-written summary — so someone whose headline says "Product Manager at
Stripe" but who spent four years in the Brooklyn Nets front office never fired
the Sports signal. Their past is on the profile; it was simply never read.

Notes is the wrong place to put the fix: it's Eric's own curated line about a
person ("Goizueta BBA 2022-2026, overlapped with Eric on campus") and burying it
under a scraped paragraph would ruin the one field he actually reads. So the raw
capture lives here instead, keyed by profile URL — exactly the split store.py
already makes for job descriptions.

Two things follow from being a cache rather than a record:

  - It's gitignored (data/.cache/), so scraped text about real people never
    lands in the repo.
  - Re-matching is free. Adding a needle re-reads every stored profile on the
    next load; it does not need a re-sweep of LinkedIn.

Written only by the Chrome person-fill sitting, from the person's own profile.
That provenance is what makes it a legitimate signal source under the honesty
rule — it is their text about themselves, never Eric-side context.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / ".cache"
PROFILES = CACHE_DIR / "profiles.json"

# Enough for an About section plus a full experience and education list. Past
# this it's endorsements and "people also viewed" — noise that only adds ways
# to match the wrong thing.
MAX_CHARS = 8000


def key(linkedin: str) -> str:
    """Normalized profile URL. Trailing slashes and query strings vary by how
    the link was copied; the same person must not cache twice."""
    u = (linkedin or "").strip().split("?")[0].rstrip("/")
    return u.replace("http://", "https://").replace("://linkedin", "://www.linkedin").lower()


def load() -> dict[str, str]:
    try:
        return json.loads(PROFILES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(d: dict[str, str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")


# The capture boundary is a list of English headings, so it rots silently: if
# LinkedIn renames "Interests" or reorders the right rail, other people's
# biographies start landing in this person's record and nothing complains. This
# is the canary — text that still smells of the rail or the footer is refused,
# loudly, rather than cached as if it were theirs. Each marker below is one
# that actually leaked on 2026-08-07.
_LEAK = re.compile(
    r"(People also viewed|Others named|Explore Premium|More profiles for you|"
    r"People you may know|Select language|LinkedIn Corporation|"
    r"\u010ce\u0161tina|Recommendation transparency)", re.I)


def looks_leaky(text: str) -> str:
    """The leak marker found in `text`, or "" if the capture is clean."""
    m = _LEAK.search(text or "")
    return m.group(0) if m else ""


def record(linkedin: str, text: str) -> bool:
    """Store one profile's readable text. Returns False when there's nothing
    usable — a profile that wouldn't render is skipped, never half-recorded,
    and a capture that swept in the sidebar or footer is refused outright."""
    k = key(linkedin)
    text = " ".join((text or "").split())
    if not k or len(text) < 40:
        return False
    leak = looks_leaky(text)
    if leak:
        raise ValueError(
            f"capture includes {leak!r} — that is the right rail or the footer, "
            "not this person. LinkedIn probably renamed a section heading; fix "
            "the boundary in the sitting before recording anything.")
    d = load()
    d[k] = text[:MAX_CHARS]
    save(d)
    return True


def get(linkedin: str) -> str:
    return load().get(key(linkedin), "")


# A profile is not a flat blob. Where a signal was found decides how much it
# means: Riley Okafor's Sports came from a 2019 student internship, while the
# thing that actually matters — she works at Elevate, a sports agency, TODAY —
# carried no weight at all. The sitting writes these markers; captures made
# before they existed have none and are treated as one undifferentiated block.
SECTIONS = ("headline", "current", "past", "education", "other")

# Strongest first. "current" beats "past" beats "education" — what someone does
# now is the opener; what they did at 19 is a footnote.
RANK = {"headline": 4, "current": 4, "past": 2, "education": 1, "other": 1, "profile": 1}

_MARK = re.compile(r"\[(" + "|".join(SECTIONS) + r")\]", re.I)


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(section, text)] strongest-first. Unmarked captures stay one block."""
    text = text or ""
    parts = _MARK.split(text)
    if len(parts) < 3:
        return [("profile", text)] if text.strip() else []
    out = []
    for i in range(1, len(parts) - 1, 2):
        sec, body = parts[i].lower(), parts[i + 1].strip()
        if body:
            out.append((sec, body))
    return sorted(out, key=lambda x: -RANK.get(x[0], 1))
