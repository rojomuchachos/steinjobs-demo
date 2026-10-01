"""Warm-intro pathfinding against a LinkedIn connections export.

A warm intro converts many times better than the best cold email that will ever
be written, so this runs before outreach, not after. Three paths, strongest first:

  1. First-degree — you already know someone there.
  2. Shared institution — Emory, the Jets, UCSF/Neuroscape, Carter Center,
     Northfield Mount Hermon, CIEE Prague. A connection who works somewhere else
     but shares one of those is still a real opening.
  3. Investor path — a connection at a fund named in the company's funding line.

`data/connections.csv` is Eric's whole professional network. It is gitignored,
never leaves the machine, and matching is plain string comparison — no model
call, no upload, nothing sent anywhere.

Export it once: LinkedIn → Settings & Privacy → Data Privacy → Get a copy of
your data → Connections. The CSV has a few preamble lines before the header,
which this skips.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Entry

ROOT = Path(__file__).resolve().parent.parent
CONNECTIONS_PATH = ROOT / "data" / "connections.csv"

# Institutions from master_cv.md that make a stranger not-quite-a-stranger.
SHARED_INSTITUTIONS = {
    "emory": "Emory",
    "new york jets": "the Jets",
    "ny jets": "the Jets",
    "ucsf": "UCSF",
    "neuroscape": "UCSF Neuroscape",
    "carter center": "the Carter Center",
    "northfield mount hermon": "Northfield Mount Hermon",
    "foxino": "Foxino",
    "ciee": "CIEE Prague",
    "trash compactor": "Trash Compactor",
}

_COMPANY_NOISE = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|co|company|the|group|holdings)\b\.?", re.I
)


@dataclass
class Connection:
    first: str
    last: str
    company: str
    position: str
    url: str = ""

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}".strip()


@dataclass
class Path_:
    kind: str          # first-degree | institution | investor
    connection: Connection
    detail: str = ""


@dataclass
class Match:
    entry: Entry
    paths: list[Path_] = field(default_factory=list)

    @property
    def strength(self) -> int:
        order = {"first-degree": 0, "institution": 1, "investor": 2}
        return min((order.get(p.kind, 9) for p in self.paths), default=9)


def load_connections(path: Path = CONNECTIONS_PATH) -> list[Connection]:
    """Read the LinkedIn export, skipping its notes preamble."""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8", errors="replace")

    # LinkedIn prepends a "Notes:" block; the real CSV starts at the header row.
    lines = raw.splitlines()
    start = 0
    for i, line in enumerate(lines[:12]):
        if "First Name" in line and "Last Name" in line:
            start = i
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))

    out = []
    for row in reader:
        norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        first = norm.get("first name", "")
        last = norm.get("last name", "")
        if not first and not last:
            continue
        out.append(
            Connection(
                first=first,
                last=last,
                company=norm.get("company", ""),
                position=norm.get("position", ""),
                url=norm.get("url", ""),
            )
        )
    return out


def _company_key(name: str) -> str:
    s = _COMPANY_NOISE.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _investors(entry: Entry) -> list[str]:
    """Fund names out of the funding line — 'Seed $7M (YC/Initialized)'."""
    text = entry.funding or ""
    inside = re.findall(r"\(([^)]+)\)", text)
    names: list[str] = []
    for chunk in inside:
        names.extend(re.split(r"[/,]| and ", chunk))
    return [n.strip() for n in names if len(n.strip()) > 2]


def find_paths(entries: list[Entry], connections: list[Connection]) -> list[Match]:
    by_company: dict[str, list[Connection]] = {}
    for c in connections:
        by_company.setdefault(_company_key(c.company), []).append(c)

    matches: list[Match] = []
    for e in entries:
        paths: list[Path_] = []
        key = _company_key(e.company)

        if key:
            for c in by_company.get(key, []):
                paths.append(Path_("first-degree", c, f"works at {c.company}"))

        # Shared institution — checked across the whole network, not per company.
        for c in connections:
            hay = f"{c.company} {c.position}".lower()
            for needle, label in SHARED_INSTITUTIONS.items():
                if needle in hay and _company_key(c.company) == key:
                    paths.append(Path_("institution", c, f"shared: {label}"))
                    break

        for fund in _investors(e):
            for c in by_company.get(_company_key(fund), []):
                paths.append(Path_("investor", c, f"at {c.company}, an investor"))

        if paths:
            # Dedupe by person, keeping the strongest path each.
            seen: dict[str, Path_] = {}
            for p in paths:
                k = p.connection.name
                order = {"first-degree": 0, "institution": 1, "investor": 2}
                if k not in seen or order.get(p.kind, 9) < order.get(seen[k].kind, 9):
                    seen[k] = p
            matches.append(Match(entry=e, paths=list(seen.values())))

    return sorted(matches, key=lambda m: (m.strength, -(m.entry.score or 0)))


def alumni_pool(connections: list[Connection]) -> dict[str, list[Connection]]:
    """Everyone in the network attached to a shared institution, for cold companies."""
    pool: dict[str, list[Connection]] = {}
    for c in connections:
        hay = f"{c.company} {c.position}".lower()
        for needle, label in SHARED_INSTITUTIONS.items():
            if needle in hay:
                pool.setdefault(label, []).append(c)
                break
    return pool


def render(matches: list[Match], total_connections: int, min_score: int) -> str:
    bar = "─" * 72
    if not total_connections:
        return "\n".join(
            [
                "",
                bar,
                "NO CONNECTIONS FILE",
                bar,
                "",
                "Export your LinkedIn connections once and drop them at",
                f"  {CONNECTIONS_PATH.relative_to(ROOT)}",
                "",
                "LinkedIn → Settings & Privacy → Data Privacy → Get a copy of your data",
                "  → pick 'Connections' → download the CSV.",
                "",
                "It is gitignored and never leaves this machine — matching is plain",
                "string comparison, with no model call and nothing uploaded.",
                "",
            ]
        )

    out = [
        "",
        bar,
        f"WARM PATHS — {len(matches)} companies, {total_connections} connections searched",
        bar,
    ]
    if not matches:
        out += [
            "",
            f"No paths into any company scoring {min_score}+.",
            "That is a real answer, not a bug — cold outreach is the play for these.",
            "",
        ]
        return "\n".join(out)

    for m in matches:
        e = m.entry
        out += [
            "",
            f"  {e.score if e.score is not None else '?':>3}  {e.title} — {e.company}",
        ]
        for p in m.paths[:4]:
            c = p.connection
            out.append(f"       [{p.kind}] {c.name} — {c.position or 'role n/a'} ({p.detail})")
            if c.url:
                out.append(f"                {c.url}")
        out.append(f"       ask for the intro before writing anything cold: {e.url}")

    out += [
        "",
        bar,
        "A warm intro beats the best cold email you will ever write. Work this list",
        "top-down before writing cold to any of them.",
        "",
    ]
    return "\n".join(out)
