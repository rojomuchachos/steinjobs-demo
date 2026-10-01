"""Pipeline state: the `make status` view and the `make mark` transition.

This is what turns a feed into a pipeline. Two jobs:

  1. Show what's actually live, grouped by state and oldest-first inside each
     group, so threads that have gone quiet are visible rather than buried
     under whatever the last scout run found.

  2. Record transitions — and, when a note is given, record the verdict into
     data/calibration.json so the scorer learns from it. An `uninterested` with a
     reason is the single most useful thing Eric can produce for this system.

Company-level memory falls out of the status field: if any role at a company is
past `review`, later roles there are surfaced as "already in touch" rather than as
fresh finds, so a founder never gets cold-emailed twice.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from .models import LEGACY_STATUS, STATUSES, Entry, normalize_company, normalize_url, today

# Displayed in pipeline order, not alphabetically — this is a funnel.
ORDER = ["offer", "interviewing", "applied", "saved", "review",
         "dormant", "rejected", "uninterested", "expired", "incomplete"]

LABELS = {
    "review": "Review",
    "saved": "Saved",
    "applied": "Applied",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "dormant": "Dormant",
    "uninterested": "Uninterested",
    "expired": "Expired",
    "incomplete": "Incomplete",
}

# A thread in one of these states with no touch for this many days is stalled.
STALE_AFTER = {"saved": 7, "applied": 10, "interviewing": 4}

# Days of silence before each nudge. The length of this list IS the budget:
# two follow-ups total, then stop. CLAUDE.md — past that it stops reading as
# persistence and starts reading as pestering.
NUDGE_SCHEDULE = (4, 7)

BAR = "─" * 72


def _days_since(iso: str) -> int | None:
    try:
        return (date.today() - date.fromisoformat(iso)).days
    except (ValueError, TypeError):
        return None


def find(entries: list[Entry], needle: str) -> list[Entry]:
    """Locate entries by URL fragment, or by 'company' / 'company:title'."""
    needle = needle.strip()
    if not needle:
        return []

    norm = normalize_url(needle)
    exact = [e for e in entries if normalize_url(e.url) == norm]
    if exact:
        return exact

    partial = [e for e in entries if needle.lower() in e.url.lower()]
    if partial:
        return partial

    if ":" in needle:
        comp, _, title = needle.partition(":")
        return [
            e
            for e in entries
            if normalize_company(comp) == normalize_company(e.company)
            and title.strip().lower() in e.title.lower()
        ]

    return [e for e in entries if normalize_company(needle) == normalize_company(e.company)]


def engaged_companies(entries: list[Entry]) -> dict[str, list[Entry]]:
    """Companies where a conversation already exists, keyed by normalized name."""
    out: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        if e.status not in ("review", "uninterested", "expired", "incomplete"):
            out[normalize_company(e.company)].append(e)
    return out


def render(entries: list[Entry], limit_new: int = 12) -> str:
    by_status: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        by_status[e.status].append(e)

    live = sum(len(by_status[s]) for s in ORDER if s not in ("review", "uninterested", "expired", "incomplete"))
    out = [
        "",
        BAR,
        f"PIPELINE — {live} live · {len(by_status['review'])} unworked · {len(entries)} tracked",
        BAR,
    ]

    for status in ORDER:
        rows = by_status.get(status, [])
        if not rows:
            continue

        # Oldest touch first: whatever has been sitting longest needs attention.
        rows.sort(key=lambda e: (e.last_touched or "9999", -(e.score or 0)))
        shown = rows[:limit_new] if status == "review" else rows

        out += ["", f"{LABELS[status].upper()}  ({len(rows)})"]
        for e in shown:
            age = _days_since(e.last_touched)
            stale = (
                age is not None
                and status in STALE_AFTER
                and age >= STALE_AFTER[status]
            )
            flag = "  ⚠ stalled" if stale else ""
            score = f"{e.score:>3}" if e.score is not None else "  ?"
            aged = f"{age}d" if age is not None else "  "
            out.append(f"  {score}  {e.title[:38]:38} {e.company[:18]:18} {aged:>4}{flag}")
            if e.status_note:
                out.append(f"       ↳ {e.status_note[:64]}")
        if status == "review" and len(rows) > limit_new:
            out.append(f"       … and {len(rows) - limit_new} more (highest-scoring shown)")

    # Company-level memory — the guard against emailing a founder twice.
    engaged = engaged_companies(entries)
    if engaged:
        notes = []
        for comp, rows in engaged.items():
            others = [
                e
                for e in entries
                if normalize_company(e.company) == comp and e.status == "review"
            ]
            if others:
                name = rows[0].company
                notes.append(f"  {name}: already in touch — {len(others)} other role(s) open")
        if notes:
            out += ["", BAR, "ALREADY IN TOUCH (don't cold-email twice)", BAR] + notes

    out += [
        "",
        f"{BAR}",
        "make log COMPANY=x to see a thread's full history; make followups for nudges due",
    ]
    out.append("")
    return "\n".join(out)


def mark(
    entries: list[Entry],
    needle: str,
    status: str,
    note: str = "",
) -> tuple[list[Entry], str]:
    """Move matching entries to `status`. Returns (changed, message)."""
    status = LEGACY_STATUS.get(status, status)
    if status not in STATUSES:
        return [], f"unknown status '{status}' — expected one of: {', '.join(STATUSES)}"

    hits = find(entries, needle)
    if not hits:
        return [], f"nothing matched '{needle}'"
    if len(hits) > 1 and status != "uninterested":
        listing = "\n".join(f"    {e.title} — {e.url}" for e in hits[:6])
        return [], (
            f"'{needle}' matched {len(hits)} entries; narrow it "
            f"(use a URL, or 'Company:Title'):\n{listing}"
        )

    for e in hits:
        e.status = status
        e.last_touched = today()
        if note:
            e.status_note = note
    return hits, f"moved {len(hits)} entry(s) to '{status}'"


def calibration_record(entry: Entry, note: str) -> dict:
    """A transition with a reason is a labeled example. Feed it to the scorer."""
    verdict = {
        "uninterested": "Screened out",
        "saved": "Top match",
        "applied": "Top match",
        "interviewing": "Top match",
        "offer": "Top match",
    }.get(entry.status, entry.status)
    return {
        "title": entry.title,
        "company": entry.company,
        "verdict": verdict,
        "why": note,
        "stage_note": entry.stage or entry.funding,
        "pay": "",
        "yoe": "",
        "location": entry.location,
        "recorded": today(),
        "source": "make mark",
        "score_given": entry.score,
    }


def followups(entries: list[Entry], today_: date | None = None) -> list[tuple[Entry, int, int]]:
    """Who has gone quiet. Returns (entry, days_since, nudge_number).

    Nudge on day 4 and day 11, then stop. Two follow-ups is the whole budget —
    past that it stops reading as persistence. Anything that reached
    `interviewing` or a terminal state drops out on its own, since only
    `applied` qualifies.
    """
    ref = today_ or date.today()
    out = []
    for e in entries:
        if e.status != "applied":
            continue
        age = _days_since(e.last_touched)
        if age is None:
            continue
        sent = e.status_note.lower().count("nudge")
        # The budget is the length of this schedule: one nudge at day 4, a
        # second at day 7 after that, then nothing. Expressed as data rather
        # than as a separate `sent >= 2` guard, which was unreachable — the
        # lookup already fails for any sent beyond the schedule — and so could
        # be deleted without any test noticing.
        if sent >= len(NUDGE_SCHEDULE):
            continue
        if age >= NUDGE_SCHEDULE[sent]:
            out.append((e, age, sent + 1))
    return sorted(out, key=lambda t: -t[1])
