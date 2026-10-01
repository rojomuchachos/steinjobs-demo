"""Tier-2 newsletter extraction — judge queued prose leads, automatically.

The substack adapter queues paragraphs that carry hiring/founder language plus
something actionable (a link or contact hint) but no direct job link. This
module turns those into routed results, the same two-backend way scoring
works: with ANTHROPIC_API_KEY the pass runs itself after every scout (haiku,
bounded per run); without a key the queue simply waits for an in-session pass
written back via `make apply`.

Routing (shared by the auto pass and `make apply`):
  company -> entities.track_company (auto — dedupe protects)
  person  -> data/substack_people_queue.json (review queue; the honesty rule
             means newsletter blurbs never write People records directly)
  posting -> unscored feed entry (make rescore picks it up)
  skip    -> judged noise; removed from the queue either way
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import feed
from .boards import substack
from .models import RawPosting, normalize_company, normalize_url

MODEL = "claude-haiku-4-5"
MAX_CONCURRENCY = 8
AUTO_LIMIT = 200  # per scout run — bounded like complete_companies

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["posting", "company", "person", "skip"]},
        "name": {"type": "string"},
        "company": {"type": "string"},
        "role": {"type": "string"},
        "url": {"type": "string"},
        "contact_hint": {"type": "string"},
        "why": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["kind"],
    "additionalProperties": False,
}

_SYSTEM = """You extract job-search leads from startup-newsletter paragraphs
for Eric Steinberg, who hunts high-agency generalist roles (growth, GTM,
chief of staff, BD, founding-team) at pre-seed/seed/Series A startups.

Given one paragraph, return ONE result:
- kind=person: a named individual worth contacting (founder, hiring manager),
  name + company + role if stated, contact_hint (email/@handle/'linkedin in
  links'), quote = the sentence that makes them worth contacting.
- kind=company: a specific startup endorsed or described as hiring/just
  raised, with no specific role link. name = the company. why = one line on
  why it's interesting NOW. Prefer person over company when both appear and
  the person is contactable.
- kind=posting: a specific open role stated in prose with a usable url in the
  links. company + role + url required.
- kind=skip: career advice, commentary, events, big-company news, or anything
  with no specific startup/person to act on.

Never invent names, companies, or contact details — only what the text
states. quote must be verbatim from the paragraph."""


def _prompt(item: dict) -> str:
    links = "\n".join(item.get("links") or []) or "(none)"
    return (f"Newsletter: {item.get('pub', '?')} — post: {item.get('post_title', '')} "
            f"({item.get('post_date', '')})\n\nParagraph:\n{item.get('text', '')}\n\n"
            f"Links in this paragraph:\n{links}")


def extract_api(items: list[tuple[int, dict]], model: str = MODEL) -> dict[str, dict]:
    """index -> result via live API calls. One bad item never kills the run."""
    import anthropic

    client = anthropic.Anthropic()
    system = [{"type": "text", "text": _SYSTEM,
               "cache_control": {"type": "ephemeral"}}]

    def one(pair: tuple[int, dict]) -> tuple[int, dict | None]:
        idx, item = pair
        try:
            resp = client.messages.create(
                model=model, max_tokens=350, system=system,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": _prompt(item)}],
            )
            if resp.stop_reason == "refusal":
                return idx, {"kind": "skip"}
            text = next(b.text for b in resp.content if b.type == "text")
            return idx, json.loads(text)
        except Exception:  # noqa: BLE001
            return idx, None  # stays queued for a later pass
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        out = dict(pool.map(one, items))
    results = {str(i): r for i, r in out.items() if r is not None}
    if not results and items:
        # Every call failing is a config problem, not 560 coincidences —
        # surface ONE real error instead of silently re-queueing everything.
        idx, item = items[0]
        try:
            client.messages.create(
                model=model, max_tokens=350, system=system,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": _prompt(item)}],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  newsletter extraction: every call failed — first error: "
                  f"{type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
    return results


def route_results(payload: dict, queue_items: list[dict],
                  entries: list) -> dict:
    """Apply extraction results. Mutates entries (postings) and the overlay
    files (companies, people queue). Returns counts + handled queue indexes."""
    from . import entities, store

    idx = feed.Index(entries)
    counts = {"companies": 0, "people": 0, "postings": 0}
    people_rows: list[dict] = []
    handled: set[int] = set()

    for key, res in payload.items():
        if not isinstance(res, dict):
            continue
        try:
            qi = int(key)
        except (TypeError, ValueError):
            continue
        item = queue_items[qi] if 0 <= qi < len(queue_items) else {}
        kind = (res.get("kind") or "skip").lower()
        handled.add(qi)  # a judged 'skip' is handled too — it must leave the queue
        src = f"substack ({item.get('pub', '?')})" if item else "substack"
        if kind == "company" and res.get("name"):
            entities.track_company(
                res["name"],
                why=f"{res.get('why') or res.get('quote') or ''} — per {src}".strip(" —"),
                follow=False)
            counts["companies"] += 1
        elif kind == "person" and res.get("name"):
            people_rows.append({
                "name": res["name"], "company": res.get("company", ""),
                "role": res.get("role", ""), "contact_hint": res.get("contact_hint", ""),
                "quote": (res.get("quote") or res.get("why") or "")[:280],
                "pub": item.get("pub", ""), "post_url": item.get("post_url", ""),
                "date": item.get("post_date", ""),
            })
            counts["people"] += 1
        elif kind == "posting" and res.get("url") and res.get("company"):
            if idx.by_url.get(normalize_url(res["url"])):
                continue
            p = RawPosting(
                title=res.get("role") or res.get("name") or "(untitled)",
                company=res["company"], url=res["url"], source="substack",
                description=(res.get("quote") or res.get("why") or ""),
            )
            store.remember([p])
            entries.append(feed.to_entry(p, None, "", False))
            counts["postings"] += 1

    if people_rows:
        try:
            existing = json.loads(substack.PEOPLE_QUEUE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            existing = []
        known = {(r.get("name", "").lower(), normalize_company(r.get("company", "")))
                 for r in existing}
        fresh = [r for r in people_rows
                 if (r["name"].lower(), normalize_company(r["company"])) not in known]
        substack.PEOPLE_QUEUE.write_text(json.dumps(existing + fresh, indent=1))
        counts["people"] = len(fresh)

    counts["handled"] = handled
    return counts


def rewrite_queue(queue_items: list[dict], handled: set[int]) -> int:
    """Drop handled items from data/substack_queue.json; returns remaining."""
    remaining = [it for i, it in enumerate(queue_items) if i not in handled]
    try:
        instructions = json.loads(substack.QUEUE.read_text()).get("instructions", "")
    except (FileNotFoundError, json.JSONDecodeError):
        instructions = ""
    substack.QUEUE.write_text(json.dumps(
        {"instructions": instructions, "count": len(remaining),
         "items": remaining}, indent=1))
    return len(remaining)


def auto_run(limit: int | None = AUTO_LIMIT) -> dict | None:
    """The self-driving pass: API key present -> extract, route, save.
    Returns counts, or None when there is no key or nothing queued."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # API parked (Eric, 2026-08-18): don't extract, but don't go silent —
        # the daily log must show the backlog so "drain it in-session" is a
        # visible ask, not a surprise.
        try:
            n = len(json.loads(substack.QUEUE.read_text()).get("items", []))
        except (OSError, json.JSONDecodeError):
            n = 0
        if n:
            print(f"  newsletter tier-2: {n} leads queued (API parked — "
                  f"say 'run the substack extraction' in a session to drain)",
                  file=sys.stderr)
        return None
    try:
        queue_items = json.loads(substack.QUEUE.read_text()).get("items", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not queue_items:
        return None
    batch = list(enumerate(queue_items))[: limit or len(queue_items)]
    print(f"  extracting {len(batch)} newsletter leads via API…", file=sys.stderr)
    payload = extract_api(batch)
    entries = feed.load()
    counts = route_results(payload, queue_items, entries)
    if counts["postings"]:
        feed.save(entries)
    counts["remaining"] = rewrite_queue(queue_items, counts.pop("handled"))
    return counts
