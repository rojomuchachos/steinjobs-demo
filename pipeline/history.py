"""Append-only activity log per entry.

`status_note` holds one line and is overwritten on every transition, so the
moment you move a thread from `drafted` to `applied` the reason you drafted it
disappears. For a pipeline whose whole point is remembering what you did and
what was said back, that's the wrong shape.

This is the record instead: every status change, note, and generated artifact,
timestamped, never rewritten. `status_note` stays as the one-line summary the
status view shows; this is the full story behind it.

Kept in its own file rather than inside feed.json for the same reason
descriptions are: the feed is the ledger and is read and rewritten constantly,
and inlining a growing event list per row would bloat every read.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import Entry, normalize_url

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.json"

# What kinds of thing happen to an entry.
# Anything not listed here is silently relabelled "note" by add(), which means a
# typo'd or newly-invented kind loses its identity without erroring. That already
# happened twice: "funding" and "enrich" were both recorded as notes, so the
# events existed but no filter could find them. Add the kind here when you add
# the caller — test_history_kinds_cover_every_caller pins the pair together.
KINDS = ("status", "note", "artifact", "score", "contact", "funding", "enrich", "feedback", "nudge")


@dataclass
class Event:
    at: str
    kind: str
    text: str
    detail: str = ""

    @property
    def date(self) -> str:
        return self.at[:10]


@dataclass
class History:
    events: dict[str, list[dict]] = field(default_factory=dict)

    def for_entry(self, url: str) -> list[Event]:
        return [Event(**e) for e in self.events.get(normalize_url(url), [])]

    def add(self, url: str, kind: str, text: str, detail: str = "",
            at: str = "") -> Event:
        # `at` lets a stage change be recorded on the day it actually happened
        # ("I applied last Tuesday"), not the day it was typed in.
        #
        # LOCAL time, not UTC: an application at 8pm Monday stamped in UTC lands
        # on Tuesday, so the daily-goal tiles read 0/3 on the very day the work
        # was done, and cards show a date from the future. Eric's day is the
        # unit of the streak, so Eric's clock is the one that counts.
        stamp = (f"{at}T12:00:00" if at
                 else datetime.now().astimezone().isoformat(timespec="seconds"))
        ev = Event(
            at=stamp,
            kind=kind if kind in KINDS else "note",
            text=text,
            detail=detail,
        )
        self.events.setdefault(normalize_url(url), []).append(asdict(ev))
        return ev

    def last(self, url: str, kind: str = "") -> Event | None:
        evs = [e for e in self.for_entry(url) if not kind or e.kind == kind]
        return evs[-1] if evs else None

    def count(self, url: str, kind: str = "") -> int:
        return len([e for e in self.for_entry(url) if not kind or e.kind == kind])


def load() -> History:
    if not HISTORY.exists():
        return History()
    try:
        return History(events=json.loads(HISTORY.read_text(encoding="utf-8") or "{}"))
    except json.JSONDecodeError:
        return History()


def save(h: History) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(h.events, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(HISTORY)


def record(url: str, kind: str, text: str, detail: str = "", at: str = "") -> Event:
    """Convenience: load, append, save. Fine at this scale."""
    h = load()
    ev = h.add(url, kind, text, detail, at=at)
    save(h)
    return ev


def render(entry: Entry, h: History | None = None) -> str:
    """The thread's story, oldest first."""
    h = h or load()
    evs = h.for_entry(entry.url)
    if not evs:
        return "  (nothing recorded yet)"
    icon = {"status": "→", "note": "·", "artifact": "◆", "score": "#", "contact": "✉"}
    lines = []
    for e in evs:
        lines.append(f"  {e.date}  {icon.get(e.kind, '·')} {e.text}")
        if e.detail:
            lines.append(f"              {e.detail}")
    return "\n".join(lines)
