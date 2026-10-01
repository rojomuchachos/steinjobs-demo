"""Core data shapes.

`RawPosting` is what a board adapter returns — deliberately loose, since every
board knows different things. `Entry` is what lands in data/feed.json, and its
field set is the contract documented in CLAUDE.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from typing import Any

# Status values. `new` is where scout puts things; the rest are set by `make
# mark` — except `expired`, which only the link sweep writes: the posting's
# page is gone (404/410). Their page dying is not Eric's no and not their no,
# so it's its own state (Eric, 2026-08-17).
# `incomplete` (Eric, 2026-08-18): a posting with no description yet. It is
# IN the ledger — the feed is also the dedupe memory, so a parallel holding
# file would drift — but out of the pile: nothing unjudged should cost Eric
# a triage decision. It promotes itself to `review` the moment text arrives
# and a score lands.
STATUSES = ("review", "saved", "applied", "interviewing", "offer",
            "rejected", "dormant", "uninterested", "expired", "incomplete")

# The vocabulary was renamed once (2026-07: new/shortlisted/drafted/responded/
# passed -> review/saved/saved/interviewing/uninterested). Old spellings still
# exist in history detail fields and any state written before the rename, so
# loads normalize through this map forever.
LEGACY_STATUS = {
    "new": "review",
    "shortlisted": "saved",
    "drafted": "saved",
    "responded": "interviewing",
    "passed": "uninterested",
}

# Getro/ATS stage strings normalized to a common vocabulary.
EARLY_STAGES = {"pre_seed", "seed", "series_a"}

# One spelling per stage. Free-text write paths (the app's edit form, hand
# tracking) produced "series a" / "pre-seed" / "pre-series a" / "early"
# alongside the canonical keys, and each variant became its own dead filter
# row in the UI (found 2026-08-18). Normalize on the way IN, always.
_STAGE_ALIASES = {
    "preseed": "pre_seed", "pre seed": "pre_seed", "pre-seed": "pre_seed",
    "pre series a": "pre_seed", "pre-series a": "pre_seed", "early": "pre_seed",
    "angel": "pre_seed", "friends and family": "pre_seed",
    "seed": "seed", "seed+": "seed", "post seed": "seed",
    "series a": "series_a", "a": "series_a", "series a1": "series_a",
    "series b": "series_b", "b": "series_b",
    "series c": "series_c", "c": "series_c",
    "series d": "growth", "series e": "growth", "late": "growth",
    "growth": "growth", "public": "growth", "ipo": "growth",
    "private equity": "private_equity", "acquisition": "acquisition",
    "acquired": "acquisition", "bootstrapped": "bootstrapped",
    "series unknown": "series_unknown", "unknown": "", "other": "other",
}


def normalize_stage(s: str) -> str:
    """Free-text stage -> the one canonical spelling, or '' when unreadable."""
    key = re.sub(r"[^a-z0-9+ ]", " ", (s or "").strip().lower())
    key = re.sub(r"\s+", " ", key).strip()
    if not key:
        return ""
    if key.replace(" ", "_") in {
            "pre_seed", "seed", "series_a", "series_b", "series_c", "growth",
            "private_equity", "acquisition", "bootstrapped", "series_unknown",
            "other"}:
        return key.replace(" ", "_")
    return _STAGE_ALIASES.get(key, "")

# Getro's company-size buckets. Boundaries inferred from known companies, so
# treat them as approximate — they're used for a coarse cut, not a precise one.
HEADCOUNT_BUCKETS = {
    1: "1-10",
    2: "11-50",
    3: "51-200",
    4: "201-500",
    5: "501-1000",
    6: "1000+",
}
# CLAUDE.md excludes companies of 100+ where "the playbook is written". Bucket 3
# straddles that line, so only 4+ is a confident exclude; 3 goes to the scorer.
HEADCOUNT_CLEARLY_TOO_BIG = 4


def headcount_label(bucket: int | None) -> str:
    return HEADCOUNT_BUCKETS.get(bucket, "") if bucket is not None else ""


@dataclass
class RawPosting:
    """One posting as a board reported it, before scoring."""

    title: str
    company: str
    url: str
    source: str
    location: str = ""

    # Structured signal, when the board provides it. These feed the prefilter
    # and let the scorer judge role shape rather than re-deriving facts.
    description: str = ""
    stage: str = ""
    # Getro reports company size as a bucket index, NOT a raw count — verified
    # empirically (Preply=6, Zipline=5, one-person shops=1). See HEADCOUNT_BUCKETS.
    head_count_bucket: int | None = None
    industry_tags: list[str] = field(default_factory=list)
    comp_min: int | None = None  # annual USD
    comp_max: int | None = None
    offers_equity: bool | None = None
    equity_range: str = ""
    remote: bool | None = None
    posted_at: date | None = None

    # Minimum years of experience, when the board states it outright. More
    # reliable than parsing prose out of the description.
    min_experience: int | None = None

    # Whether the employer states it will sponsor. Decisive for Europe: Eric is
    # a US citizen, so an EU role that won't sponsor is not a real option.
    sponsors_visa: bool | None = None

    # Some boards hand us the founders directly (Work at a Startup does), which
    # is the enrichment step for free — and the founder is the real target.
    founders: list[dict[str, Any]] = field(default_factory=list)
    company_url: str = ""

    def identity(self) -> str:
        """Cross-board dedupe key: same role on three boards collapses to one."""
        return f"{normalize_company(self.company)}::{normalize_title(self.title)}"


@dataclass
class Entry:
    """One row of data/feed.json. Field names are the CLAUDE.md contract."""

    title: str
    company: str
    url: str
    source: str
    location: str
    score: int | None
    why: str
    founders: list[dict[str, Any]] = field(default_factory=list)
    funding: str = ""
    headcount: str = ""
    date_added: str = ""

    # Extensions (see plan). Safe to add: nothing outside this repo parses the feed.
    status: str = "review"
    status_note: str = ""
    last_touched: str = ""
    first_seen: str = ""
    identity: str = ""
    also_seen_on: list[str] = field(default_factory=list)
    warm_path: str = ""
    escape_hatch: bool = False
    stage: str = ""
    posted_at: str = ""
    sponsors_visa: bool | None = None
    company_url: str = ""
    comp_min: int | None = None   # annual USD, from the board when stated
    comp_max: int | None = None
    min_experience: int | None = None  # years, board-stated only (WaaS); the
                                       # dashboard derives the rest from the
                                       # cached description at render time
    level: str = ""               # intern|entry|mid|senior|exec — from the
                                  # DESCRIPTION at scoring time, "" when the
                                  # text doesn't say. Never title-inferred.
    interview_stage: str = ""     # screen | take-home | technical | onsite | final
    role_type: str = ""           # hand override of the title-derived job-type bucket

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Entry":
        known = {f for f in cls.__dataclass_fields__}
        d = {k: v for k, v in d.items() if k in known}
        if d.get("status"):
            d["status"] = LEGACY_STATUS.get(d["status"], d["status"])
        return cls(**d)


# --- normalization helpers, shared by dedupe and enrichment ---

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|co|company|labs|lab|technologies|technology"
    r"|tech|health|holdings|group|studio|studios|ai|io|hq)\b\.?",
    re.I,
)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Stripped from titles before comparison — they vary by board for the same role.
_TITLE_NOISE = re.compile(
    r"\b(senior|sr|junior|jr|lead|staff|principal|head\s+of|i{1,3}|1|2|3"
    r"|full[\s-]?time|part[\s-]?time|contract|remote|hybrid|on[\s-]?site"
    r"|us|usa|uk|nyc|new\s+york|sf|san\s+francisco)\b",
    re.I,
)


def normalize_company(s: str) -> str:
    s = _PUNCT.sub(" ", (s or "").lower())
    s = _COMPANY_SUFFIXES.sub(" ", s)
    return _WS.sub(" ", s).strip()


def normalize_title(s: str) -> str:
    s = _PUNCT.sub(" ", (s or "").lower())
    # Drop anything after a separator — "Growth Lead - NYC, Full Time".
    s = re.split(r"\s+[-–—|/]\s+", s)[0]
    s = _TITLE_NOISE.sub(" ", s)
    return _WS.sub(" ", s).strip()


def normalize_url(u: str) -> str:
    """Strip tracking params and trailing slashes so the same job matches itself."""
    u = (u or "").strip()
    u = re.sub(r"[?&](utm_[^&]*|gh_src|ref|source|lever-source[^&]*)(?=&|$)", "", u)
    u = re.sub(r"[?&]$", "", u)
    return u.rstrip("/").lower()


def today() -> str:
    # LOCAL date, not UTC — same bug history.py already fixed: an 8pm mark in
    # PDT stamped tomorrow's date, so day counters read -1d and daily-goal
    # tiles credited the wrong day. Eric's day is the unit everywhere.
    return datetime.now().astimezone().date().isoformat()
