"""LinkedIn resolution — turning founder names into profiles you can write to.

Form D gives names ("Jordan Pike, Executive Officer"), company sites often
give names without links, and outreach needs a person you can actually reach.
This is the last mile.

There is no legitimate free deterministic route. DuckDuckGo's HTML endpoint
403s bots (verified), Google and Bing are ToS-hostile to scraping, and the paid
APIs (Proxycurl etc.) are a licensing decision Eric hasn't made. So resolution
follows the same two-backend split as scoring and enrichment:

  api    — Anthropic web search finds the profile unattended
  agent  — a queue file for in-session resolution by Claude Code

Either way, results come back through `apply()` which VALIDATES before writing:
the URL must actually be a linkedin.com/in/ profile, and empty stays empty. A
wrong profile is worse than none — Eric would be writing to a stranger with the
right name, which reads as carelessness to exactly the person he's trying to
impress. Confidence travels with every resolution so a `probable` match gets a
human glance before anything is sent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Entry

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "data" / "resolution_queue.json"

_PROFILE = re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9%_\-\.]+/?$")

CONFIDENCES = ("confirmed", "probable", "none")


def needs_resolution(e: Entry, min_score: int = 70) -> list[dict]:
    """Founders on a relevant entry who have a name but no profile."""
    if (e.score or 0) < min_score or e.status == "uninterested":
        return []
    return [f for f in (e.founders or []) if f.get("name") and not f.get("linkedin")]


def targets(entries: list[Entry], min_score: int = 70) -> list[tuple[Entry, dict]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[Entry, dict]] = []
    for e in entries:
        for f in needs_resolution(e, min_score):
            key = (f["name"].lower(), e.company.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append((e, f))
    return sorted(out, key=lambda t: -(t[0].score or 0))


def write_queue(pairs: list[tuple[Entry, dict]]) -> Path:
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE.write_text(
        json.dumps(
            {
                "instructions": (
                    "Resolve each person to their LinkedIn profile. Search by name + "
                    "company + title. Rules: (1) the URL must be a linkedin.com/in/ "
                    "profile; (2) mark confidence 'confirmed' only when the profile "
                    "unambiguously matches name AND company — same-name strangers are "
                    "the failure mode; (3) if not found or ambiguous, leave the url "
                    "empty with confidence 'none' — a wrong profile is worse than "
                    "none. Write back with: make apply-resolution FILE=<results.json> "
                    "using {\"Company|Name\": {\"linkedin\": ..., \"confidence\": ...}}."
                ),
                "count": len(pairs),
                "people": [
                    {
                        "key": f"{e.company}|{f['name']}",
                        "name": f["name"],
                        "title": f.get("title", ""),
                        "company": e.company,
                        "company_context": (e.why or "")[:140],
                        "score": e.score,
                    }
                    for e, f in pairs
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return QUEUE


def apply(entries: list[Entry], results: dict[str, dict]) -> tuple[int, list[str]]:
    """Write resolutions back. Validates every URL; refuses garbage loudly."""
    rejected: list[str] = []
    clean: dict[tuple[str, str], dict] = {}

    for key, data in results.items():
        company, _, name = key.partition("|")
        url = (data.get("linkedin") or "").strip().rstrip("/")
        conf = (data.get("confidence") or "none").lower()
        if conf not in CONFIDENCES:
            conf = "probable"
        if url and not _PROFILE.match(url):
            rejected.append(f"{key}: not a linkedin.com/in/ URL → dropped ({url[:60]})")
            continue
        if not url:
            continue  # explicit not-found: nothing to write, and that's correct
        clean[(company.lower().strip(), name.lower().strip())] = {
            "linkedin": url,
            "confidence": conf,
        }

    updated = 0
    for e in entries:
        for f in e.founders or []:
            hit = clean.get((e.company.lower().strip(), (f.get("name") or "").lower().strip()))
            if hit and not f.get("linkedin"):
                f["linkedin"] = hit["linkedin"]
                # Ride along on the existing email_confidence-style pattern:
                # record how sure the resolution is, so outreach can warn.
                f["linkedin_confidence"] = hit["confidence"]
                updated += 1
    return updated, rejected
