"""Freshness handling.

Eric's rule: nothing older than a month, ideally under two weeks, sooner is
better. Implementing that turned out to be less straightforward than it sounds,
because **the two highest-yield boards don't publish posting dates at all.**

Measured coverage:

    southparkcommons   100%   real dates (Ashby/Greenhouse/Lever all supply them)
    techstars          100%   real dates
    generalist           0%   ld+json says datePosted 2026-03-01 on *every*
                              posting — a hardcoded placeholder, not a date
    workatastartup       0%   neither the list nor the detail payload has one

So a naive "drop anything over 30 days" would delete generalist and Work at a
Startup entirely — which between them hold every one of the current top finds,
including Hera at 88. The filter would remove exactly the postings it exists to
surface.

Two honest mechanisms instead of one dishonest one:

1. Where a board states a real date, enforce the cutoff and score recency.
2. Where it doesn't, fall back to `first_seen` — the date this pipeline first
   observed the posting. That's weak on the first run (everything looks new) but
   becomes genuinely accurate as runs accumulate: a posting still present sixty
   days after we first saw it is, in fact, sixty days stale.

Nothing is dropped on an inferred date. Guessing a posting is old and deleting
it is a silent, unrecoverable error; letting it through with a scoring penalty
is a visible, recoverable one.
"""

from __future__ import annotations

from datetime import date

# Score adjustments. Deliberately modest — recency is a tiebreaker between
# comparable roles, not a reason to bury a genuinely excellent one.
BONUS_FRESH = 5      # <= 14 days — Eric's "ideally"
PENALTY_AGING = -3   # 31-60 days
PENALTY_STALE = -6   # 61-120 days; past 120 the prefilter drops it outright

IDEAL_DAYS = 14
CUTOFF_DAYS = 30


# Dates a board emits for every posting regardless of when it was actually
# posted. generalist.world's ld+json returns 2026-03-01 on all ~300 rows. The
# current adapter reads the listing cards and never touches that field, but if
# anyone later adds a detail fetch, trusting it would both drop the whole board
# past the 30-day cutoff and stamp it stale. Distrusted by name so that can't
# happen quietly.
PLACEHOLDER_DATES = {"2026-03-01"}


def age_days(posted_at: str, first_seen: str = "", today: date | None = None) -> tuple[int | None, str]:
    """Return (age_in_days, basis). Basis is 'posted', 'first_seen', or 'unknown'."""
    ref = today or date.today()
    if posted_at in PLACEHOLDER_DATES:
        posted_at = ""
    for value, basis in ((posted_at, "posted"), (first_seen, "first_seen")):
        if not value:
            continue
        try:
            d = date.fromisoformat(value)
        except (ValueError, TypeError):
            continue
        return max(0, (ref - d).days), basis
    return None, "unknown"


def adjustment(age: int | None, basis: str) -> tuple[int, str]:
    """Score modifier for a posting's age, plus a one-line explanation."""
    if age is None:
        return 0, ""
    if age <= IDEAL_DAYS:
        label = "posted" if basis == "posted" else "first seen"
        return BONUS_FRESH, f"fresh — {label} {age}d ago"
    if age <= CUTOFF_DAYS:
        return 0, f"{age}d old"
    if age <= 60:
        return PENALTY_AGING, f"aging — {age}d old"
    # Only a stated date justifies calling something stale outright.
    if basis == "posted":
        return PENALTY_STALE, f"stale — posted {age}d ago"
    return PENALTY_AGING, f"seen in the feed for {age}d without moving"


def apply(score: int | None, posted_at: str, first_seen: str = "") -> tuple[int | None, str]:
    """Adjust a score for freshness. Returns (new_score, note)."""
    if score is None:
        return None, ""
    age, basis = age_days(posted_at, first_seen)
    delta, note = adjustment(age, basis)
    if not delta:
        return score, note
    return max(0, min(100, score + delta)), note
