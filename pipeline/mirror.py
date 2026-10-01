"""Recover walled posting text by finding the same role published elsewhere.

The link sweep (refetch.py) reads any page that serves HTML and any ATS that
publishes JSON. What survives that is genuinely walled: LinkedIn and Indeed
job pages, JS-only boards, login gates. Those postings stay unscored forever
under the no-blind-scoring rule, which is correct but wasteful — the SAME role
is usually published somewhere readable: the company's own careers page, a
different aggregator, an ATS mirror.

So: ask a model with web search to find that other copy and hand back its
description. Three guards, because a description drives a score:

  1. IDENTITY. The mirror must be the same company AND the same role. A
     "Growth Lead" at a different Acme is worse than no text at all, so the
     prompt returns empty rather than guess, and the caller checks the
     company name came back matching.
  2. WORTH IT. Only postings whose title already passes the prefilter and
     whose host isn't a mega-corp get spent on — measured 2026-08-18, that's
     ~2/3 of the walled pile, and the rest would have floored anyway.
  3. NO RE-BILLING. Every attempt is stamped in a sidecar; an unfindable
     posting is not retried for 30 days. Same lesson as research.py, where a
     failed run once stamped 303 companies.

Costs API credits, so it is never automatic — `make mirror` only, with a
spend guard. The in-session twin is "run the mirror pass".
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from . import store
from .models import Entry, normalize_url

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / ".cache" / "mirror_checked.json"

MODEL = "claude-haiku-4-5"
MAX_CONCURRENCY = 6
AUTO_CAP = 60
RETRY_AFTER_DAYS = 30
MIN_CHARS = 400  # shorter than this isn't a description, it's a snippet

# ~$0.02/posting measured on the research pass's shape (one search + ~800
# output tokens on Haiku). Used only for the spend projection.
COST_PER_POSTING = 0.02

_SYSTEM = """You find the full text of a specific job posting that is published on more than one site.

You are given a company, a job title, and the URL where we saw it (often LinkedIn, Indeed, or a JS-only board we cannot read). Search the web for the SAME role published somewhere readable — the company's own careers page, its ATS (Ashby/Greenhouse/Lever/Workable), or another aggregator — and return that posting's description.

IDENTITY IS EVERYTHING. Company names collide constantly. Return the empty result unless the posting you find is unmistakably:
  - the same company (not a same-named other business), AND
  - the same role (not a different opening at that company).
A wrong description produces a wrong score on a real decision. Empty is always the safe answer.

Return ONLY this, no preamble:

COMPANY: <the company name exactly as the mirror states it, or leave blank>
SOURCE: <the URL you read the description from, or leave blank>
DESCRIPTION: <the posting's own text: what the role does, requirements, and any stated years of experience, seniority, comp, or location. Reproduce the posting's substance in plain prose. Do not summarize into one line, do not editorialize, do not invent detail the posting doesn't state.>

If you cannot confirm identity, return exactly:
COMPANY:
SOURCE:
DESCRIPTION:
"""


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def _parse(text: str) -> dict:
    """The three-field reply -> dict, or {} when identity wasn't confirmed."""
    out = {}
    for field in ("company", "source", "description"):
        m = re.search(rf"^{field}:\s*(.*?)(?=^\s*(?:company|source|description):|\Z)",
                      text or "", re.I | re.M | re.S)
        out[field] = (m.group(1).strip() if m else "")
    if len(out["description"]) < MIN_CHARS or not out["company"]:
        return {}
    return out


def _title_is_real(title: str) -> bool:
    """A searchable single role. Some imports carry a URL where a title
    belongs, and newsletter listings merge several openings into one string
    ("Growth Marketing Manager; Founding Account Executive") — measured
    2026-08-18, those are what the first live batch failed on, because no
    single posting anywhere matches the combined string."""
    t = (title or "").strip()
    if not t or t.lower().startswith("http") or len(t) <= 3:
        return False
    if ";" in t or " and " in t.lower() or "," in t and len(t) > 60:
        return False
    return True


def candidates(entries: list[Entry], cap: int | None = AUTO_CAP) -> list[Entry]:
    """Walled postings worth paying to recover, best-first."""
    from .prefilter import (_EXCLUDE_TITLE, _EXCLUDE_UNLESS_FOUNDING, _FOUNDING,
                            _SENIOR_OK, _TOO_SENIOR, bigco_host)

    cache = store.load()
    state = _load_state()
    stale = (date.today() - timedelta(days=RETRY_AFTER_DAYS)).isoformat()

    def worth(e: Entry) -> bool:
        if (e.status or "review") != "review":
            return False
        if len(cache.get(normalize_url(e.url), "")) >= 300:
            return False
        if not e.url.startswith("http") or not _title_is_real(e.title):
            return False
        if bigco_host(e.url):
            return False
        t = e.title
        if _EXCLUDE_TITLE.search(t):
            return False
        if _TOO_SENIOR.search(t) and not _SENIOR_OK.search(t):
            return False
        if _EXCLUDE_UNLESS_FOUNDING.search(t) and not _FOUNDING.search(t):
            return False
        return (state.get(normalize_url(e.url)) or "") < stale

    out = [e for e in entries if worth(e)]
    # Newest first: a fresh posting is likelier to still be mirrored live.
    out.sort(key=lambda e: e.first_seen or e.date_added or "", reverse=True)
    return out[:cap] if cap else out


def recover(entries: list[Entry], model: str = MODEL) -> dict:
    """Ask for each posting's text elsewhere. Returns counts; writes the cache."""
    import anthropic

    from .models import normalize_company

    client = anthropic.Anthropic()
    errors: list[str] = []

    def one(e: Entry):
        prompt = (f"Company: {e.company}\nJob title: {e.title}\n"
                  f"Where we saw it: {e.url}\n"
                  + (f"Location as listed: {e.location}\n" if e.location else ""))
        try:
            resp = client.messages.create(
                model=model, max_tokens=1600,
                system=[{"type": "text", "text": _SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[{"type": "web_search_20250305", "name": "web_search",
                        "max_uses": 2}],
                messages=[{"role": "user", "content": prompt}],
            )
            text = " ".join(b.text for b in resp.content
                            if getattr(b, "type", "") == "text")
            return e, _parse(text)
        except Exception as exc:  # noqa: BLE001 — one posting must not kill the run
            errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
            return e, None  # None = errored: no stamp, retry later

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        results = list(pool.map(one, entries))

    if errors and len(errors) == len(entries):
        print(f"  mirror pass: ALL calls failed — first error: {errors[0]}",
              file=sys.stderr)
        return {"checked": len(entries), "recovered": 0, "unfound": 0,
                "errored": len(errors)}

    cache = store.load()
    state = _load_state()
    today = date.today().isoformat()
    counts = {"checked": 0, "recovered": 0, "unfound": 0, "errored": 0,
              "identity_rejected": 0}
    for e, found in results:
        if found is None:
            counts["errored"] += 1
            continue
        counts["checked"] += 1
        state[normalize_url(e.url)] = today  # answered, even if answered-empty
        if not found:
            counts["unfound"] += 1
            continue
        # Second identity check on our side: the mirror's company name must
        # normalize to ours. The prompt already refuses strangers; this
        # catches a model that answered anyway.
        if normalize_company(found["company"]) != normalize_company(e.company):
            counts["identity_rejected"] += 1
            continue
        key = normalize_url(e.url)
        if len(cache.get(key, "")) < 300:
            body = found["description"]
            if found.get("source"):
                body += f"\n\n[recovered from {found['source']}]"
            cache[key] = body[: store.MAX_CHARS]
            counts["recovered"] += 1
    store.save(cache)
    _save_state(state)
    return counts


def estimate_cost(n: int) -> float:
    return round(n * COST_PER_POSTING, 2)


def run(entries: list[Entry], cap: int | None = AUTO_CAP,
        max_spend: float = 5.0, yes: bool = False) -> dict | None:
    """`make mirror` — recover walled descriptions, with a spend guard."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  mirror pass needs ANTHROPIC_API_KEY (the API is parked by "
              "default — say 'run the mirror pass' in a session instead)",
              file=sys.stderr)
        return None
    batch = candidates(entries, cap)
    if not batch:
        print("nothing walled and worth recovering right now.")
        return None
    projected = estimate_cost(len(batch))
    print(f"{len(batch)} walled postings worth recovering · "
          f"projected ${projected:.2f}")
    if projected > max_spend and not yes:
        print(f"refusing to start: ${projected:.2f} exceeds the ${max_spend:.2f} "
              "guard — raise --max-spend, cap with --limit, or pass --yes",
              file=sys.stderr)
        return None
    counts = recover(batch)
    print(f"  recovered {counts['recovered']} descriptions · "
          f"{counts['unfound']} not findable · "
          f"{counts['identity_rejected']} rejected on identity · "
          f"{counts['errored']} errored")
    if counts["recovered"]:
        print("run `make rescore` to score them (the floor screens the weak ones).")
    return counts
