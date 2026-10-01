"""data/feed.json — the permanent ledger and the dedupe memory.

Append-only. Never trimmed (CLAUDE.md). Two dedupe keys:
  1. normalized URL — primary, exact.
  2. identity (company + title) — catches the same role posted to three boards
     under three URLs.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import (Entry, RawPosting, headcount_label, normalize_company,
                     normalize_title, normalize_url, today)

FEED_PATH = Path(__file__).resolve().parent.parent / "data" / "feed.json"


def load(path: Path = FEED_PATH) -> list[Entry]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8") or "[]")
    return [Entry.from_dict(d) for d in raw]


def save(entries: list[Entry], path: Path = FEED_PATH) -> None:
    """Write atomically — a crash mid-write must not destroy the ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps([e.to_dict() for e in entries], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


class Index:
    """Lookup over the existing feed, for deciding what is actually new."""

    def __init__(self, entries: list[Entry]):
        self.by_url = {normalize_url(e.url): e for e in entries if e.url}
        self.by_identity: dict[str, Entry] = {}
        for e in entries:
            if e.identity and e.identity not in self.by_identity:
                self.by_identity[e.identity] = e
        # Company-level memory: which companies have you already engaged?
        self.engaged_companies = {
            e.company.lower() for e in entries
            if e.status not in ("review", "uninterested", "expired", "incomplete")
        }

    def find(self, p: RawPosting) -> Entry | None:
        hit = self.by_url.get(normalize_url(p.url))
        if hit:
            return hit
        return self.by_identity.get(p.identity())

    def is_engaged(self, company: str) -> bool:
        return (company or "").lower() in self.engaged_companies


def merge_new(
    existing: list[Entry], postings: list[RawPosting]
) -> tuple[list[RawPosting], int]:
    """Split incoming postings into genuinely-new vs. already-known.

    Known postings aren't discarded silently: if a role turns up on a board we
    hadn't seen it on, that board is recorded in also_seen_on, which is signal
    (a role pushed to five boards is being marketed hard).
    """
    index = Index(existing)
    fresh: list[RawPosting] = []
    seen_this_run: dict[str, RawPosting] = {}
    desc_backfill: dict[str, tuple[str, str]] = {}
    dupes = 0

    for p in postings:
        if not p.url or not p.title:
            continue
        hit = index.find(p)
        if hit:
            dupes += 1
            if p.source and p.source != hit.source and p.source not in hit.also_seen_on:
                hit.also_seen_on.append(p.source)
            # Ashby wins the URL (Eric, 2026-08-17): when the same role is on
            # several boards, the Ashby page is the clean apply path — prefer
            # it as the canonical link. Only while the entry is untouched:
            # once saved/applied, history and artifacts reference the URL.
            if ("jobs.ashbyhq.com" in p.url and "jobs.ashbyhq.com" not in hit.url
                    and (hit.status or "review") in ("review", "new")):
                from . import store
                from .models import normalize_url as _nu
                cache = store.load()
                old = _nu(hit.url)
                if old in cache:
                    cache.setdefault(_nu(p.url), cache[old])
                    store.save(cache)
                hit.url = p.url
            # A re-seen posting can carry facts the first sighting lacked —
            # boards disagree about what they publish (WaaS ships founders,
            # Consider ships salary, Wellfound ships neither reliably). Merge
            # every field the duplicate knows and the survivor doesn't.
            # Blanks only; never overwrite something already recorded.
            if hit.sponsors_visa is None and p.sponsors_visa is not None:
                hit.sponsors_visa = p.sponsors_visa
            if not hit.founders and p.founders:
                hit.founders = p.founders
            if not hit.company_url and p.company_url:
                hit.company_url = p.company_url
            if not hit.location and p.location:
                hit.location = p.location
            if not hit.posted_at and p.posted_at:
                hit.posted_at = p.posted_at.isoformat()
            if hit.comp_min is None and p.comp_min is not None:
                hit.comp_min = p.comp_min
            if hit.comp_max is None and p.comp_max is not None:
                hit.comp_max = p.comp_max
            if hit.min_experience is None and p.min_experience is not None:
                hit.min_experience = p.min_experience
            if not hit.stage and p.stage:
                hit.stage = p.stage
            # The description cache is keyed by the SURVIVOR's url — a dupe
            # carrying real text fills that slot so the entry becomes
            # scorable, even though the dupe itself is discarded.
            if (p.description or "").strip():
                desc_backfill.setdefault(p.identity(), (hit.url, p.description))
            continue

        # Collapse duplicates within this same run, too.
        key = p.identity()
        prior = seen_this_run.get(key)
        if prior:
            dupes += 1
            if p.source and p.source != prior.source:
                prior.industry_tags = list({*prior.industry_tags, *p.industry_tags})
            continue

        seen_this_run[key] = p
        fresh.append(p)

    if desc_backfill:
        from . import store
        from .models import normalize_url as _nu
        cache = store.load()
        changed = False
        for _ident, (hit_url, desc) in desc_backfill.items():
            key = _nu(hit_url)
            if key and key not in cache:
                cache[key] = desc.strip()[: store.MAX_CHARS]
                changed = True
        if changed:
            store.save(cache)

    return fresh, dupes


def to_entry(p: RawPosting, score: int | None, why: str, escape_hatch: bool = False,
             level: str = "") -> Entry:
    # An acquired brand resolves to the acquirer on the way in, so a rename
    # done once in the feed can't be undone by the next scout.
    from .entities import resolve_company

    company = resolve_company(p.company)
    return Entry(
        title=p.title,
        company=company,
        url=p.url,
        source=p.source,
        location=p.location,
        score=score,
        why=why,
        date_added=today(),
        first_seen=today(),
        last_touched=today(),
        identity=f"{normalize_company(company)}::{normalize_title(p.title)}",
        escape_hatch=escape_hatch,
        stage=p.stage,
        posted_at=p.posted_at.isoformat() if p.posted_at else "",
        headcount=headcount_label(p.head_count_bucket),
        founders=list(p.founders),
        sponsors_visa=p.sponsors_visa,
        company_url=p.company_url,
        comp_min=p.comp_min,
        comp_max=p.comp_max,
        min_experience=p.min_experience,
        level=level,
    )
