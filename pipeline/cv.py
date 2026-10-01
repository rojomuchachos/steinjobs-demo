"""Parse data/master_cv.md into something selectable.

The master CV is deliberately structured for this: every entry carries a
metadata line like

    `Florham Park, NJ · Jul 2025 – Feb 2026 · priority: core · tags: sports, analytics`

and the file opens with an explicit proof-point map (master_cv.md "Tailoring
rules"). That map is the authority on what to lead with for a given role — it's
richer than the abridged copy in CLAUDE.md, so it's read from the CV, not
duplicated here.

Used by brief.py now, and by `make tailor` later, which needs exactly the
same selection: pick entries whose tags match the role, honour priority, keep
the always-include set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CV_PATH = ROOT / "data" / "master_cv.md"

_META = re.compile(r"`([^`]+)`")
_PRIORITY = re.compile(r"priority:\s*(\w+)", re.I)
_TAGS = re.compile(r"tags:\s*([^·`]+)", re.I)
_STACK = re.compile(r"stack:\s*([^·`]+)", re.I)

PRIORITY_RANK = {"core": 0, "situational": 1, "low": 2, "": 3}


@dataclass
class CVEntry:
    heading: str          # "Football Data Scientist · New York Jets"
    section: str          # "Work experience" | "Technical projects" | ...
    meta: str             # the raw backticked metadata line
    priority: str = ""
    tags: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)

    @property
    def role(self) -> str:
        return self.heading.split("·")[0].strip()

    @property
    def org(self) -> str:
        parts = self.heading.split("·")
        return parts[1].strip() if len(parts) > 1 else ""

    def score_against(self, wanted: list[str]) -> int:
        """How well this entry matches a set of role tags.

        Case-insensitive: the CV writes `tags: bd, ml` lowercase while role
        hints read naturally as "BD"/"ML", and a case mismatch would silently
        drop real matches.
        """
        if not wanted:
            return 0
        mine = {t.lower() for t in self.tags}
        hits = sum(1 for t in wanted if t.lower() in mine)
        # A core entry outranks a situational one at equal tag overlap.
        return hits * 10 - PRIORITY_RANK.get(self.priority, 3)


def _parse_meta(line: str) -> tuple[str, list[str]]:
    m = _META.search(line)
    if not m:
        return "", []
    raw = m.group(1)
    pr = _PRIORITY.search(raw)
    tg = _TAGS.search(raw)
    tags = [t.strip().lower() for t in tg.group(1).split(",")] if tg else []
    return (pr.group(1).lower() if pr else ""), [t for t in tags if t]


def parse(path: Path = CV_PATH) -> list[CVEntry]:
    entries: list[CVEntry] = []
    section = ""
    current: CVEntry | None = None

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            current = None
            continue
        if line.startswith("### "):
            current = CVEntry(heading=line[4:].strip(), section=section, meta="")
            entries.append(current)
            continue
        if current is None:
            continue
        if line.strip().startswith("`") and not current.meta:
            current.meta = line.strip()
            current.priority, current.tags = _parse_meta(line)
            continue
        if line.strip().startswith("- "):
            # Strip authoring comments — they're notes to Eric, not content.
            bullet = re.sub(r"<!--.*?-->", "", line.strip()[2:]).strip()
            if bullet:
                current.bullets.append(bullet)

    return entries


def proof_point_map(path: Path = CV_PATH) -> dict[str, str]:
    """The 'lead with this for that' map from the CV's tailoring rules.

    Returns {trigger phrase -> entry name}, e.g. {"sports": "Jets + TEndencIQ"}.
    """
    text = path.read_text(encoding="utf-8")
    block = text.split("Proof-point mapping")[-1].split("3.")[0]
    out: dict[str, str] = {}
    for line in block.splitlines():
        m = re.match(r"\s*-\s*(.+?)\s*→\s*(.+?)\s*$", line)
        if m:
            for trigger in re.split(r"\s*/\s*", m.group(1)):
                out[trigger.strip().lower()] = m.group(2).strip()
    return out


def best_entries(tags: list[str], limit: int = 3, path: Path = CV_PATH) -> list[CVEntry]:
    """Rank CV entries against a role's tags. Ties fall back to priority order."""
    entries = [e for e in parse(path) if e.bullets]
    ranked = sorted(
        entries,
        key=lambda e: (-e.score_against(tags), PRIORITY_RANK.get(e.priority, 3)),
    )
    return ranked[:limit]


# Maps the vocabulary that shows up in job postings onto the CV's own tags, so a
# posting about "digital health" reaches the clinical entries.
ROLE_TAG_HINTS: dict[str, list[str]] = {
    "sports": ["sports"],
    "athlet": ["sports"],
    "fitness": ["sports", "health"],
    "health": ["health", "clinical"],
    "clinical": ["clinical", "health"],
    "patient": ["clinical", "health"],
    "care": ["clinical", "health"],
    "medic": ["clinical", "health"],
    "biotech": ["clinical", "research"],
    "mental health": ["mental-health", "psychedelics"],
    "psychedelic": ["psychedelics", "mental-health"],
    "neuro": ["neuroscience", "research"],
    "music": ["music", "community"],
    "creator": ["music", "community", "marketing"],
    "community": ["community", "growth"],
    "education": ["ed-tech", "teaching"],
    "learning": ["ed-tech", "teaching"],
    "ed-tech": ["ed-tech", "GTM"],
    "agricultur": ["agriculture", "food-systems"],
    "food": ["agriculture", "food-systems"],
    "farm": ["agriculture", "food-systems"],
    "civic": ["civic-tech", "ML"],
    "law enforcement": ["civic-tech", "trust-and-safety"],
    "public safety": ["civic-tech", "trust-and-safety"],
    "fraud": ["trust-and-safety", "ML"],
    "misinformation": ["civic-tech", "trust-and-safety"],
    "moderation": ["trust-and-safety"],
    "compliance": ["trust-and-safety"],
    "election": ["civic-tech"],
    "trust and safety": ["trust-and-safety", "civic-tech"],
    "policy": ["civic-tech"],
    "growth": ["growth", "0-to-1"],
    "marketing": ["marketing", "growth"],
    "brand": ["marketing", "growth"],
    "partnership": ["BD", "growth"],
    "business development": ["BD", "growth"],
    "sales": ["BD", "revenue-ownership"],
    "founding": ["0-to-1", "revenue-ownership"],
    "chief of staff": ["0-to-1", "stakeholder-comms"],
    "operations": ["0-to-1", "high-pressure"],
    "product": ["product", "user-research"],
}


def tags_for_role(title: str, description: str = "", industry: str = "") -> list[str]:
    """Derive CV tags to match on, from a posting's own words."""
    hay = f"{title} {industry} {description[:1500]}".lower()
    tags: list[str] = []
    for phrase, mapped in ROLE_TAG_HINTS.items():
        if phrase in hay:
            tags.extend(mapped)
    # Preserve first-seen order, drop repeats.
    seen: set[str] = set()
    return [t for t in tags if not (t in seen or seen.add(t))]
