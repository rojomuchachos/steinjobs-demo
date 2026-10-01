"""The run summary. CLAUDE.md requires every scout run to end with one:
count found per board, top 3 roles with reasoning, boards that blocked access.
"""

from __future__ import annotations

from .boards.base import BoardResult
from .models import Entry

BAR = "─" * 72


def _fmt_board(r: BoardResult, kept: int) -> str:
    if r.blocked:
        return f"  {r.board:<20} blocked — {r.error}"
    if r.error and not r.postings and not r.companies:
        return f"  {r.board:<20} FAILED — {r.error}"
    note = f"  ({r.error})" if r.error else ""
    if r.companies and not r.postings:
        # Company-first boards (EDGAR since the play retirement, 2026-08-12)
        return f"  {r.board:<20} {len(r.companies):>5} fresh raises → company review{note}"
    return f"  {r.board:<20} {len(r.postings):>5} found → {kept:>4} after prefilter{note}"


def render(
    results: list[BoardResult],
    kept_by_board: dict[str, int],
    dropped: dict[str, int],
    dupes: int,
    new_entries: list[Entry],
    backend: str,
) -> str:
    out: list[str] = ["", BAR, "SCOUT RUN", BAR, "", "Boards:"]

    for r in results:
        out.append(_fmt_board(r, kept_by_board.get(r.board, 0)))

    total = sum(len(r.postings) for r in results)
    out += [
        "",
        f"Total fetched: {total}   ·   duplicates skipped: {dupes}   ·   new: {len(new_entries)}",
    ]

    if dropped:
        out.append("")
        out.append("Prefilter dropped:")
        for reason, n in sorted(dropped.items(), key=lambda kv: -kv[1]):
            out.append(f"  {n:>5}  {reason}")

    scored = [e for e in new_entries if e.score is not None]
    if scored:
        out += ["", BAR, "TOP ROLES", BAR, ""]
        for e in sorted(scored, key=lambda x: -(x.score or 0))[:3]:
            flag = "  [ESCAPE HATCH]" if e.escape_hatch else ""
            out += [
                f"  {e.score}  {e.title} · {e.company}{flag}",
                f"       {e.why}",
                f"       {e.location or 'location n/a'} · {e.stage or 'stage n/a'} · {e.source}",
                f"       {e.url}",
                "",
            ]
        buckets = {
            "75+ (enriched)": sum(1 for e in scored if (e.score or 0) >= 75),
            "60-74": sum(1 for e in scored if 60 <= (e.score or 0) < 75),
            "under 60": sum(1 for e in scored if (e.score or 0) < 60),
        }
        out.append("Score distribution: " + " · ".join(f"{k}: {v}" for k, v in buckets.items()))
        if buckets["75+ (enriched)"] == len(scored) and len(scored) > 5:
            out.append("  ⚠ everything scored 75+ — the scorer is not discriminating; check the prompt")
    elif new_entries:
        out += [
            "",
            f"⚠ {len(new_entries)} postings were NOT scored (backend: {backend}).",
            "  They are in the feed with score: null. See data/candidates.json.",
        ]

    blocked = [r.board for r in results if r.blocked]
    if blocked:
        out += ["", f"Blocked / not yet implemented: {', '.join(blocked)}"]

    out.append("")
    return "\n".join(out)
