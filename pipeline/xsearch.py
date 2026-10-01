"""X hiring-tweet sweep — Grok's server-side x_search, one capped call a day.

The only affordable path to tweets is xAI's API: Grok searches X itself
(server-side x_search tool, ~$5/1k searches + tokens) and returns the posts
with their URLs. The raw X API starts at $200/mo and headless scraping of X
breaks the walled-network rule, so this module is the whole strategy.

The tweet IS the lead (Eric, 2026-08-07): a VC vouching "a founder I back is
hiring" has no link to extract and loses its meaning as a feed row. So the
sweep collects tweets VERBATIM into a ledger (data/x_tweets.json) for a
Tweets view where Eric reads them in context and promotes by hand. The one
automatic conversion left: a tweet carrying a direct application link for a
specific named role becomes an unscored posting — that case is unambiguous.

This is a ~$2/month EXPERIMENT. `make insights` + the ledger's own
saved/dismissed ratio decide whether it earns its keep.

Guardrails, in order of the lessons that created them:
- One request per calendar day, period. `last_run` is the throttle.
- An ERRORED call never stamps `last_run` (a failed research run once
  stamped 303 companies on an empty credit balance) — only an answered
  request, even answered-empty, counts as today's run.
- Seen tweets (data/x_seen.json) never re-enter the ledger; stamped after
  the ledger and feed are safely written.
- Tweet text is stored verbatim, never paraphrased; hints (company, people,
  link) come only from what the tweet says. No People records, ever — the
  ledger is a review surface, promotion is manual.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from .prefilter import MAX_YEARS

ROOT = Path(__file__).resolve().parent.parent
SEEN = ROOT / "data" / "x_seen.json"
TWEETS = ROOT / "data" / "x_tweets.json"

API_URL = "https://api.x.ai/v1/responses"
# grok-4.3 after the credit burn (2026-08-07): the day-one burst on grok-4.5
# emptied the balance — reasoning tokens bill at $6/MTok there vs $2.50 on
# 4.3, and an uncapped agentic loop re-feeds every search result each turn.
MODEL = "grok-4.3"  # XSEARCH_MODEL overrides
MAX_TURNS = 4  # caps the agentic loop — the other half of the credit burn

WINDOW_DAYS = 21  # Eric 2026-08-07: 3 weeks for now — daily runs skim the fresh edge anyway
MAX_ITEMS = 25  # per call; seen-dedupe means only fresh ones land

_PROMPT = """Search X for HIGH-SIGNAL early-stage hiring tweets from the last
{window} days for Eric Steinberg: high-agency generalist roles (founding team,
growth, GTM, BD/partnerships, chief of staff, PM, GTM/growth/forward-deployed
engineer) at pre-seed / seed / Series A startups. Sweet-spot industries:
health & human performance, fitness, longevity, wearables; also music and the
creative industry, psychedelics & mental health, neuroscience tools, ag/food
tech, ed-tech, civic tech. NYC preferred, SF fine, remote fine. Europe is IN
SCOPE when visa sponsorship is plausible — Dublin, Amsterdam, Prague, London,
and Italy especially.

Hunt several distinct shapes of tweet — all count:
- a founder personally hiring at their own startup ("we're hiring our first…",
  "looking for a founding…", "DM me")
- a VC or angel vouching for a portfolio founder who is hiring ("a founder I
  work with / we just backed is hiring…") — name who is vouching for whom
- a just-raised announcement that mentions hiring, even with no role named
- a just-raised or launch announcement in the sweet-spot industries above with
  NO hiring mention — outreach targets: money landed, roles unposted
- an operator/talent person sharing a specific early role at a specific startup

Roles often live in an ATTACHED IMAGE ("roles below 👇" + screenshot) — read
the images. Also check reply threads: founders answer "are you hiring?" in
replies, and VC "what are you building" threads hide startups in the replies.

EXCLUDE: job-board and aggregator accounts, engagement-bait threads
("100 startups hiring rn 🧵"), big companies (100+ people), senior/staff
roles, internships, and pure IC data science / analytics seats.

Return ONLY a JSON array (no prose), at most {max_items} items, best first:
{{"tweet_url": "https://x.com/... (REQUIRED - the exact post)",
  "handle": "author handle without @",
  "author_name": "author display name",
  "tweet_date": "YYYY-MM-DD",
  "text": "the tweet's text VERBATIM - do not summarize or clean it up",
  "why": "one line on why this fits Eric",
  "company": "the hiring startup, if the tweet names it",
  "people": ["names of specific people the tweet says to contact or vouches for"],
  "role": "role title, only if the tweet states one",
  "kind": "ONE of: hiring | raise | intro | advice — hiring = a specific role
    is open; raise = a funding announcement; intro = someone vouching for a
    person or offering to connect; advice = commentary with no live opening",
  "location": "city/country the ROLE is in, only if the tweet states one",
  "min_years": "minimum years of experience as a NUMBER, only if the tweet
    states one (e.g. '5+ years' -> 5); omit otherwise",
  "application_link": "direct application/careers URL, only if the tweet contains one"}}

Never invent handles, names, companies, links, dates, or text - only what the
tweets actually say. An empty array beats padding."""


def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# The ledger's tweet types, so the UI can facet them. Anything unrecognized
# falls back to "hiring" — the ledger's original meaning, and the safe read
# for a tweet that reached here at all.
_KINDS = {k: k for k in ("hiring", "raise", "intro", "advice")}
_KINDS.update({"funding": "raise", "raised": "raise", "job": "hiring",
               "role": "hiring", "referral": "intro", "commentary": "advice"})

# Where Eric will actually work: the US, the European cities he named, and
# remote. Everything else is out of scope (CLAUDE.md Constraints). Matched
# only against a STATED location — silence is not a rejection.
_IN_SCOPE = re.compile(
    r"\b(remote|anywhere|us|usa|united states|new york|nyc|brooklyn|san francisco"
    r"|sf|bay area|los angeles|la|seattle|austin|boston|chicago|denver|miami"
    r"|atlanta|washington|dc|philadelphia|portland|san diego|toronto"
    r"|london|dublin|amsterdam|prague|berlin|paris|barcelona|madrid|lisbon"
    r"|milan|rome|italy|ireland|netherlands|czech|uk|united kingdom|europe|eu)\b",
    re.I)


def _out_of_scope_location(loc: str) -> bool:
    """True only when a location is STATED and it is somewhere Eric won't work."""
    loc = (loc or "").strip()
    return bool(loc) and not _IN_SCOPE.search(loc)


def extract_items(text: str) -> list[dict]:
    """Grok's reply -> validated items. Tags stripped first (the <cite>
    lesson); anything without a tweet_url we can link and dedupe on, or
    without verbatim text to display, is noise - drop it."""
    text = re.sub(r"<[^>]+>", "", text or "")
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    items = []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        turl = str(it.get("tweet_url") or "").strip()
        if not (turl.startswith("http") and str(it.get("text") or "").strip()):
            continue
        it["tweet_url"] = turl
        it["kind"] = _KINDS.get(str(it.get("kind") or "").strip().lower(), "hiring")
        # The boards' constraints apply here too (Eric, 2026-08-18: a
        # Bengaluru role wanting heavy experience reached the ledger). Gate
        # ONLY on what the tweet states — a tweet with no stated location or
        # years is kept, exactly like a posting that states neither.
        if _out_of_scope_location(str(it.get("location") or "")):
            continue
        try:
            yrs = int(str(it.get("min_years") or "").strip() or -1)
        except ValueError:
            yrs = -1
        if yrs > MAX_YEARS:
            continue
        items.append(it)
    return items[:MAX_ITEMS]


def _request(api_key: str, model: str, lens: str = "",
             window: int = WINDOW_DAYS) -> str:
    """One Responses-API call with the x_search tool; returns the reply text."""
    import httpx

    prompt = _PROMPT.format(window=window, max_items=MAX_ITEMS)
    if lens:
        # A lens biases one sweep toward a slice the generic query underserves
        # (NYC, an industry, just-raised) — used for burst backfills and any
        # future weekday rotation. Same call shape, same cost.
        prompt += f"\n\nFOCUS THIS SWEEP ON: {lens}. Prefer tweets matching it."
    body = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "tools": [{"type": "x_search",
                   "from_date": (date.today() - timedelta(days=window)).isoformat(),
                   "to_date": date.today().isoformat(),
                   # Role lists live in attached screenshots more often than
                   # in tweet text — without this flag those read as empty.
                   "enable_image_understanding": True}],
        "max_output_tokens": 12000,
        "max_turns": MAX_TURNS,
    }
    r = httpx.post(API_URL, json=body, timeout=300.0,
                   headers={"Authorization": f"Bearer {api_key}"})
    r.raise_for_status()
    data = r.json()
    parts = []
    for out in data.get("output") or []:
        if out.get("type") == "message":
            for c in out.get("content") or []:
                if c.get("type") == "output_text":
                    parts.append(c.get("text") or "")
    # Cost accountability: the day-one burst emptied the credit balance with
    # nobody watching per-call usage. Surface what THIS call consumed.
    # usd_ticks are 1e-10 USD — verified against token math 2026-08-07
    # (ticks 287223500 = $0.0287 = uncached input + output + 3 searches).
    u = data.get("usage") or {}
    det = u.get("server_side_tool_usage_details") or {}
    stats = {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
             "x_searches": det.get("x_search_calls"),
             "cost_usd": round((u.get("cost_in_usd_ticks") or 0) / 1e10, 4)}
    return "\n".join(parts) or str(data.get("output_text") or ""), stats


def route(items: list[dict], entries: list) -> dict:
    """Fresh tweets land in the ledger verbatim, status 'new'. The only
    automatic conversion: a direct application link for a named role becomes
    an unscored posting. Everything else waits for Eric in the Tweets view."""
    from . import feed, prefilter, store
    from .models import RawPosting, normalize_url

    ledger = _load(TWEETS)
    rows = ledger.setdefault("tweets", [])
    idx = feed.Index(entries)
    counts = {"tweets": 0, "postings": 0}
    for it in items:
        handle = str(it.get("handle") or "?").lstrip("@")
        rows.append({
            "url": it["tweet_url"], "handle": handle,
            "author_name": it.get("author_name", ""),
            "date": it.get("tweet_date", ""), "text": it.get("text", ""),
            "why": it.get("why", ""), "company": it.get("company", ""),
            "people": [p for p in (it.get("people") or []) if isinstance(p, str)],
            "role": it.get("role", ""),
            "application_link": it.get("application_link", ""),
            "status": "new", "found": date.today().isoformat(),
        })
        counts["tweets"] += 1
        link, role = it.get("application_link", ""), it.get("role", "")
        if link and role and it.get("company") and not idx.by_url.get(normalize_url(link)):
            p = RawPosting(
                title=role, company=it["company"], url=link, source="x",
                description=f"{it.get('text', '')} — via x (@{handle}, {it.get('tweet_date') or ''})",
            )
            # Same gate every board posting passes — the first live sweep
            # auto-imported two full-stack-engineer roles the prefilter
            # screens everywhere else. The tweet stays in the ledger either way.
            kept, _ = prefilter.run([p])
            if not kept:
                continue
            store.remember([p])
            entries.append(feed.to_entry(p, None, "", False))
            counts["postings"] += 1
    if counts["tweets"]:
        TWEETS.write_text(json.dumps(ledger, indent=1, ensure_ascii=False))
    return counts


def sweep(force: bool = False, lens: str = "") -> dict | None:
    """The self-driving pass: key present + not yet run today -> one Grok
    call, ledger, save. Returns counts, or None (no key / capped / errored)."""
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return None
    state = _load(SEEN)
    today_iso = date.today().isoformat()
    if state.get("last_run") == today_iso and not force:
        return None
    # Dynamic window (2026-08-18): the fixed 21-day window went 7 straight
    # days finding 0 fresh — Grok's top-25 for three weeks is the same
    # tweets every morning, all already seen, and new tweets never crack
    # the ranking. A daily run only needs the fresh edge; the window
    # stretches to cover however long the sweep actually missed (capped at
    # 21) so an offline laptop still catches up.
    try:
        gap = (date.today() - date.fromisoformat(state.get("last_run", ""))).days
    except (ValueError, TypeError):
        gap = WINDOW_DAYS
    window = min(WINDOW_DAYS, max(3, gap + 2))
    print(f"  X sweep: asking Grok for hiring tweets ({window}d window)"
          f"{' — lens: ' + lens if lens else ''}…",
          file=sys.stderr)
    try:
        text, stats = _request(api_key, os.environ.get("XSEARCH_MODEL", MODEL), lens,
                               window=window)
        print(f"  X sweep usage: {stats['input_tokens']} in / "
              f"{stats['output_tokens']} out tokens, "
              f"{stats['x_searches']} searches ≈ ${stats['cost_usd']}",
              file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — an errored call must NOT stamp last_run
        print(f"  X sweep failed (not counted as today's run): "
              f"{type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)
        return None

    from . import feed

    seen_tweets = state.setdefault("tweets", {})
    items = [it for it in extract_items(text) if it["tweet_url"] not in seen_tweets]
    entries = feed.load()
    counts = route(items, entries)
    if counts["postings"]:
        feed.save(entries)
    # Seen-state commits only after the ledger and feed are safely written
    # (funding.py ordering rule); an answered-empty run still stamps
    # last_run - that WAS today's request.
    for it in items:
        seen_tweets[it["tweet_url"]] = it.get("tweet_date") or today_iso
    state["last_run"] = today_iso
    state["last_stats"] = stats
    SEEN.write_text(json.dumps(state, indent=1))
    return counts
