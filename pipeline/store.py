"""Sidecar cache for job descriptions.

feed.json deliberately doesn't carry descriptions — it's the permanent ledger and
is never trimmed, so embedding 5KB of prose per row would push it into the
megabytes fast. But rescoring needs them: scoring a posting from its title alone
is exactly the weak, title-matching behaviour the rubric warns against.

So descriptions live here, keyed by normalized URL, written during scout and read
during rescore. It's regenerable from a fresh scout, so it lives under data/.cache/
and is gitignored.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import normalize_url

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / ".cache"
DESCRIPTIONS = CACHE_DIR / "descriptions.json"

# Descriptions past this length add tokens without adding judgment.
MAX_CHARS = 6000


def load() -> dict[str, str]:
    if not DESCRIPTIONS.exists():
        return {}
    try:
        return json.loads(DESCRIPTIONS.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def save(cache: dict[str, str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DESCRIPTIONS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DESCRIPTIONS)


def remember(postings) -> int:
    """Record descriptions for anything that has one. Returns how many were new."""
    cache = load()
    added = 0
    for p in postings:
        desc = (getattr(p, "description", "") or "").strip()
        if not desc:
            continue
        key = normalize_url(p.url)
        if key and key not in cache:
            cache[key] = desc[:MAX_CHARS]
            added += 1
    if added:
        save(cache)
    return added


def get(url: str) -> str:
    return load().get(normalize_url(url), "")


def stats() -> tuple[int, int]:
    cache = load()
    total = sum(len(v) for v in cache.values())
    return len(cache), total
