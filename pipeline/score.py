"""Scoring against the CLAUDE.md rubric.

Two backends, chosen by SCORER_BACKEND (auto | api | agent):

  api   — calls the Anthropic API directly. Unattended-capable, so `make scout`
          can run from cron. Needs ANTHROPIC_API_KEY.
  agent — writes data/candidates.json for Claude Code to score in-session and
          hand back. No key required. This is the fallback when no key is set,
          so the pipeline is useful on day one.

Why a model call at all: the rubric's two heaviest criteria — role shape /
agency fit (35%) and industry fit (20%) — are judgment. The board adapters
already supply stage, headcount, location and comp as structured fields, so the
model is asked to judge shape and fit, not to re-derive facts it was given.
"""

from __future__ import annotations

import json
import os
import sys
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .models import RawPosting, headcount_label

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_PATH = ROOT / "data" / "candidates.json"

# Opus 4.8 is the default per Anthropic's current guidance. Scoring is high
# volume, so if cost matters more than judgment quality here, set SCORER_MODEL
# to claude-sonnet-5 — that's a deliberate choice, not a default we make.
DEFAULT_MODEL = "claude-haiku-4-5"
MAX_CONCURRENCY = 8
DESC_CHARS = 3500  # enough to judge shape; keeps token spend sane

# Seniority as the DESCRIPTION states it, not as the title implies (Eric,
# 2026-08-18). The prefilter's senior screen is a title regex, which misses
# the mis-titled traps in both directions: a "Product Lead" that is really an
# entry seat, an "Associate" that wants eight years. Only the scorer reads
# the text, so only the scorer can call this — and it must return "" when the
# text doesn't say, never an inference from the title, or it silently rebuilds
# the regex prefilter while looking like it read something.
LEVELS = ("intern", "entry", "mid", "senior", "exec")

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "why": {"type": "string"},
        "escape_hatch": {"type": "boolean"},
        "level": {"type": "string",
                  "enum": ["intern", "entry", "mid", "senior", "exec", ""]},
    },
    "required": ["score", "why", "escape_hatch", "level"],
    "additionalProperties": False,
}


@dataclass
class Scored:
    posting: RawPosting
    score: int | None
    why: str
    escape_hatch: bool = False
    level: str = ""      # "" means the description didn't say — never inferred


_LEVEL_SYSTEM = """You read one job description and report the EXPERIENCE BAR it sets.

Answer with exactly one word from: intern, entry, mid, senior, exec, unknown

  intern  — internship, co-op, working-student, or explicitly a student role
  entry   — 0-2 years, new grad, "early career", first job in the field
  mid     — roughly 3-5 years of relevant experience
  senior  — 6+ years of experience demanded
  exec    — the role requires having ALREADY been a VP/C-level operator, or
            demands a decade-plus track record
  unknown — the description does not say, or says something incoherent

This measures ONE thing: how much prior experience a candidate must have to be
taken seriously. It is not a measure of scope, ownership, or title prestige.

That distinction decides most hard cases, and getting it backwards is the
failure mode this replaced (measured 2026-08-18: 60 postings came back "exec",
including five Chief-of-Staff roles the reader actively wants):

  - "Founding <anything>" at an early-stage startup is an EARLY-EMPLOYEE seat.
    Broad ownership is what early employees get. Judge it by the years it asks
    for — a Founding Growth Lead wanting 2 years is entry, not senior.
  - "Chief of Staff" at a startup is likewise not exec unless the description
    demands prior executive experience. Read the requirements.
  - Head of / Director / Lead / GM in a 10-person company usually means "the
    only person doing this", not an executive layer.
  - Conversely, an "Associate" or "Specialist" title asking for eight years IS
    senior. The title misleads in both directions.

Read the REQUIREMENTS, not the title. If the text states no experience bar at
all, answer unknown — that is a real and useful answer. Guessing from the title
adds a confident-looking label with no new information behind it, since the
title was already read by a regex before you saw it.

One word. No punctuation, no explanation."""


def levels_for(postings: list[RawPosting], model: str = DEFAULT_MODEL) -> dict[str, str]:
    """url -> level, for postings whose description we already have cached.

    A dedicated pass rather than a rescore: the judgment needs the same input
    but a one-word output, so it costs a fraction of what re-running the whole
    rubric would (measured 2026-08-18: ~$0.27 batched for 595 postings vs
    several dollars to rescore them).
    """
    import anthropic

    client = anthropic.Anthropic()
    out: dict[str, str] = {}

    def one(p: RawPosting) -> tuple[str, str]:
        try:
            resp = client.messages.create(
                model=model, max_tokens=8,
                system=[{"type": "text", "text": _LEVEL_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content":
                           f"Title: {p.title}\n\n{(p.description or '')[:DESC_CHARS]}"}],
            )
            text = " ".join(b.text for b in resp.content
                            if getattr(b, "type", "") == "text")
            return p.url, _level(text.strip().split()[0] if text.strip() else "")
        except Exception:  # noqa: BLE001 — one posting must not kill the run
            return p.url, ""

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        for url, lvl in pool.map(one, postings):
            if lvl:
                out[url] = lvl
    return out


def _level(v) -> str:
    """Only a value the model actually returned from the enum; else ''."""
    s = str(v or "").strip().lower()
    return s if s in LEVELS else ""


def _rubric_prompt() -> str:
    """The scoring contract, built from the spec files rather than duplicated here.

    CLAUDE.md and master_cv.md are the source of truth (CLAUDE.md says so
    explicitly), so the prompt reads them at runtime. Editing the spec changes
    the scorer, which is the behaviour the spec promises.
    """
    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    cv = (ROOT / "data" / "master_cv.md").read_text(encoding="utf-8")
    # Only the positioning half of the CV matters for scoring; the bullet detail
    # is for tailoring. Cut at "## Work experience" to keep the prompt tight.
    cv_head = cv.split("## Work experience")[0]

    return f"""You are scoring job postings for Eric Steinberg against his own written spec.

Here is that spec verbatim — it is the authority, not your own judgment about
what makes a good job:

<spec>
{claude_md}
</spec>

<candidate_positioning>
{cv_head}
</candidate_positioning>

Score each posting 0-100 on the rubric in the spec. Notes on how to weigh it:

- Role shape / agency fit (35%) is the heaviest criterion and the one that
  needs real judgment. Judge the SHAPE of the work, not the title: does this
  person own an outcome end to end, wear many hats, face outward? A "Marketing
  Associate" who owns a channel scores higher than a "Head of Growth" who runs
  someone else's playbook.
- Stage (25%), location (10%), and comp (10%) are given to you as structured
  fields where the board provided them. Use them; do not guess when absent —
  score the criterion neutrally instead.
- Apply the hard excludes strictly. A pure IC data-science role scores low even
  if everything else fits — the spec is explicit that Python/R/SQL are a
  tiebreaker skill and never the core job function.
- `level` is the seniority the DESCRIPTION states, one of: intern, entry, mid,
  senior, exec. Read the requirements, not the title — a "Product Lead" asking
  for 1-2 years is entry, an "Associate" asking for 8 is senior. If the text
  gives you nothing to go on, return "" — an empty level is correct and useful,
  a guessed one is a lie the title already told.
- The escape hatch is real but rare: if a role is genuinely exceptional and
  breaks the rules, set escape_hatch true and score on gut. Expect to use this
  on well under 1 in 50 postings.
- `why` is one line, concrete, and about THIS posting. "Great early-stage fit"
  is useless. "Owns first GTM hire at a 6-person sleep-tracking company" is not.
  Wrap the 2-4 most decision-relevant WORDS OR SHORT PHRASES in **double
  asterisks** — the specific hooks or dealbreakers ("**first growth hire**",
  "**requires 5+ yrs**"), never whole sentences.

Four clarifications Eric gave directly, which override any contrary reading of
the spec:

1. HYBRID TECHNICAL-GTM ROLES ARE A PLUS, NOT AN EXCLUDE. "Growth Engineer",
   "GTM Engineer", "Forward Deployed Engineer", "Deployment Strategist" and
   similar are roles he actively wants — they sit exactly where his data
   background meets the outward-facing work he's after. Do NOT treat them as
   engineering hard-excludes. The hard exclude is a pure IC seat with no
   outward-facing or ownership component; a hybrid that faces customers and
   owns an outcome is among the best fits available. (His own shortlist marked
   "GTM Engineer" at Ease Health a Top match.)

2. CHIEF OF STAFF SPLITS IN TWO, AND ONLY THE DESCRIPTION TELLS YOU WHICH.
   A CoS who builds — owns projects, runs special initiatives, has real scope —
   is close to ideal and should score high. A CoS who is an executive assistant
   with a better title — calendar, travel, expenses, scheduling, inbox — should
   score low no matter how good the company is. Read for which one it is and
   say so in `why`. If the posting leads with scheduling and logistics, that is
   an EA seat.

3. EUROPE IS IN SCOPE, BUT VISA SPONSORSHIP IS DECISIVE. Eric is a US citizen
   and would need sponsorship plus relocation. So for any non-US role:
     - explicitly sponsors, or is at a company that clearly does → score it
       normally, treating the location as fine
     - explicitly does NOT sponsor, or says "must have the right to work" →
       cap it low regardless of fit, and say why. It is not a real option.
     - silent on sponsorship → score on merit but note the uncertainty in `why`
   Cities he named with some enthusiasm: Dublin, Amsterdam, Prague, London,
   anywhere in Italy. He is open-minded beyond those. Prague is worth a small
   extra nudge — he studied there (CIEE) and worked for a Czech startup
   (Foxino), so he has genuine ties. COMP FOR NON-US ROLES IS JUDGED AGAINST
   THE LOCAL MARKET, not the US floor — European early-stage salaries run
   lower and he accepts that; never penalise an EU role for paying EU rates.

4. HE IS BEING SELECTIVE, NOT URGENT. Start date is not pressing. Prefer
   precision over volume: a smaller number of genuinely excellent matches beats
   a long list of plausible ones. Do not inflate a mediocre role because it is
   available.

Be honest and calibrated. A feed where everything scores 80 is worthless — most
postings genuinely are not a fit, and saying so is the useful part of this job.
{_calibration_block()}"""


# Bucket labels from Eric's hand-built shortlists, mapped to the score band each
# implies. Used only to describe his verdicts back to the scorer.
_BUCKET_BANDS = {
    "Top match": "80-90",
    "Worth a look": "62-75",
    "Stretch/senior": "48-60",
    "Screened out": "under 40",
}


def _calibration_block(limit: int = 40) -> str:
    """Eric's own verdicts, as worked examples.

    This is what stops the scorer drifting toward the rubric's letter and away
    from his actual taste. Rejections are included deliberately and sampled
    evenly against acceptances — a set of only positives teaches nothing about
    where the line is.
    """
    path = ROOT / "data" / "calibration.json"
    if not path.exists():
        return ""
    try:
        records = json.loads(path.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        return ""
    if not records:
        return ""

    by_verdict: dict[str, list[dict]] = {}
    for r in records:
        by_verdict.setdefault(r.get("verdict", ""), []).append(r)

    # Round-robin across verdicts so no single bucket dominates the examples.
    picked: list[dict] = []
    ordered = [v for v in _BUCKET_BANDS if v in by_verdict] + [
        v for v in by_verdict if v not in _BUCKET_BANDS
    ]
    i = 0
    while len(picked) < limit and any(by_verdict.get(v) for v in ordered):
        v = ordered[i % len(ordered)]
        if by_verdict.get(v):
            picked.append(by_verdict[v].pop())
        i += 1

    if not picked:
        return ""

    lines = [
        "",
        "Here is how Eric has actually judged real postings. These are his own",
        "verdicts, not the rubric's — where they and your reading of the rubric",
        "disagree, his verdicts are the better guide to what he wants.",
        "",
    ]
    for r in picked:
        band = _BUCKET_BANDS.get(r.get("verdict", ""), "")
        ctx = " · ".join(x for x in (r.get("stage_note"), r.get("yoe"), r.get("location")) if x)
        lines.append(
            f'- "{r.get("title")}" at {r.get("company")}'
            + (f" ({ctx})" if ctx else "")
            + f' → {r.get("verdict")}'
            + (f" [{band}]" if band else "")
            + (f': {r.get("why")}' if r.get("why") else "")
        )
    return "\n".join(lines)


def _posting_prompt(p: RawPosting) -> str:
    known, unknown = [], []
    for label, value in (
        ("Stage", p.stage),
        ("Company size", headcount_label(p.head_count_bucket)),
        ("Location", p.location),
        ("Industry tags", ", ".join(p.industry_tags[:10])),
        ("Posted", p.posted_at.isoformat() if p.posted_at else ""),
    ):
        (known if value else unknown).append(f"{label}: {value}" if value else label)

    comp = ""
    if p.comp_min or p.comp_max:
        comp = f"Comp: ${p.comp_min or '?'}-${p.comp_max or '?'}"
        if p.offers_equity:
            comp += " + equity"
        known.append(comp)

    desc = p.description[:DESC_CHARS] if p.description else ""
    body = f"\n\nFull description:\n{desc}" if desc else (
        "\n\n(No description available from this board — score on title, company, "
        "and the structured fields. Be more conservative when the title alone is "
        "ambiguous rather than assuming the best case.)"
    )

    return (
        f"Title: {p.title}\n"
        f"Company: {p.company}\n"
        f"URL: {p.url}\n"
        + "\n".join(known)
        + (f"\nNot reported by the board: {', '.join(unknown)}" if unknown else "")
        + body
    )


# --- api backend ---


def _score_api(postings: list[RawPosting], model: str) -> list[Scored]:
    import anthropic

    client = anthropic.Anthropic()
    system = [
        {
            "type": "text",
            "text": _rubric_prompt(),
            # The rubric is identical across every posting in the run, so it is
            # the cache prefix. (Silently a no-op if under the model's minimum.)
            "cache_control": {"type": "ephemeral"},
        }
    ]

    def one(p: RawPosting) -> Scored:
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=400,
                system=system,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": _posting_prompt(p)}],
            )
            if resp.stop_reason == "refusal":
                return Scored(p, None, "scoring refused by safety classifier")
            text = next(b.text for b in resp.content if b.type == "text")
            d = json.loads(text)
            return Scored(
                p,
                max(0, min(100, int(d["score"]))),
                str(d["why"]).strip(),
                bool(d.get("escape_hatch")),
                _level(d.get("level")),
            )
        except Exception as exc:  # noqa: BLE001 — one bad posting must not kill the run
            return Scored(p, None, f"scoring failed: {type(exc).__name__}")

    if len(postings) >= BATCH_MIN:
        try:
            return _score_api_batch(client, postings, model, system)
        except Exception as exc:  # noqa: BLE001 — batch trouble falls back to live calls
            print(f"  batch scoring failed ({type(exc).__name__}) — falling back to live calls",
                  file=sys.stderr)
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        return list(pool.map(one, postings))


def _score_api_batch(client, postings, model, system) -> list["Scored"]:
    """Score through the Message Batches API — 50% of live pricing.

    Results arrive in ANY order; key by custom_id, never position. Poll
    interval starts tight because small batches often finish in under a
    minute; most complete well within the hour.
    """
    import json as _json
    import time

    requests = [
        {
            "custom_id": f"p-{i}",
            "params": {
                "model": model,
                "max_tokens": 400,
                "system": system,
                "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
                "messages": [{"role": "user", "content": _posting_prompt(p)}],
            },
        }
        for i, p in enumerate(postings)
    ]
    batch = client.messages.batches.create(requests=requests)
    deadline = time.time() + 3600
    wait = 10.0
    while time.time() < deadline:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        time.sleep(wait)
        wait = min(wait * 1.4, 60)
    else:
        raise TimeoutError(f"batch {batch.id} still running after an hour")

    by_id: dict[str, Scored] = {}
    for result in client.messages.batches.results(batch.id):
        i = int(result.custom_id.split("-")[1])
        p = postings[i]
        if result.result.type == "succeeded":
            msg = result.result.message
            if msg.stop_reason == "refusal":
                by_id[result.custom_id] = Scored(p, None, "scoring refused by safety classifier")
                continue
            try:
                text = next(bl.text for bl in msg.content if bl.type == "text")
                d = _json.loads(text)
                by_id[result.custom_id] = Scored(
                    p, max(0, min(100, int(d["score"]))),
                    str(d["why"]).strip(), bool(d.get("escape_hatch")),
                    _level(d.get("level")))
            except (StopIteration, ValueError, KeyError, TypeError):
                by_id[result.custom_id] = Scored(p, None, "scoring failed: unparseable batch result")
        else:
            by_id[result.custom_id] = Scored(p, None, f"scoring failed: batch {result.result.type}")
    return [by_id.get(f"p-{i}", Scored(p, None, "scoring failed: missing from batch"))
            for i, p in enumerate(postings)]


# --- agent backend ---


def _score_agent(postings: list[RawPosting]) -> list[Scored]:
    """Emit candidates for in-session scoring, and return them unscored.

    The run still produces a complete, deduped, prefiltered candidate set — it
    just stops short of assigning numbers, and says so loudly rather than
    inventing them.
    """
    CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATES_PATH.write_text(
        json.dumps(
            {
                "instructions": (
                    "No ANTHROPIC_API_KEY was set, so `make scout` could not score "
                    "these itself. Score each posting 0-100 against the rubric in "
                    "CLAUDE.md, then write the results back into data/feed.json. "
                    "Include a `level` per posting (intern|entry|mid|senior|"
                    "exec) read from the DESCRIPTION's stated requirements, "
                    "never inferred from the title; use \"\" when the text "
                    "doesn't say. "
                    "The rubric prompt this run would have used is in "
                    "pipeline/score.py::_rubric_prompt."
                ),
                "count": len(postings),
                "postings": [
                    {
                        "title": p.title,
                        "company": p.company,
                        "url": p.url,
                        "source": p.source,
                        "location": p.location,
                        "stage": p.stage,
                        "headcount": headcount_label(p.head_count_bucket),
                        "industry_tags": p.industry_tags[:10],
                        "posted_at": p.posted_at.isoformat() if p.posted_at else "",
                        "description": p.description[:DESC_CHARS],
                    }
                    for p in postings
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return [Scored(p, None, "not scored — no API key (see data/candidates.json)") for p in postings]


# $/M tokens, input and output. Used only to project spend before a run — the
# real bill comes from usage, but a projection is enough to stop a runaway.
PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),   # intro pricing through 2026-08-31
    "claude-haiku-4-5": (1.0, 5.0),
}
DEFAULT_PRICE = (1.0, 5.0)
OUTPUT_TOKENS = 150   # a scored verdict is small and predictable
TAIL_TOKENS = 400     # per-posting text after the cached system prefix
BATCH_MIN = 10        # runs this large go through the Batch API at half price


def estimate_cost(n: int, model: str) -> float:
    """Project the spend for scoring `n` postings.

    The system prompt is identical across a run, so the first call writes it to
    cache (1.25x) and the rest read it (0.1x). Assumes the prompt clears the
    model's minimum cacheable prefix — it does at ~5.5k tokens.
    """
    if n <= 0:
        return 0.0
    p_in, p_out = PRICES.get(model, DEFAULT_PRICE)
    sys_tokens = len(_rubric_prompt()) // 4
    write = sys_tokens * 1.25
    reads = (n - 1) * sys_tokens * 0.1
    tails = n * TAIL_TOKENS
    cost = ((write + reads + tails) / 1e6) * p_in + ((n * OUTPUT_TOKENS) / 1e6) * p_out
    if n >= BATCH_MIN:
        cost *= 0.5  # runs this size go through the Batch API
    return cost


def resolve_backend() -> str:
    choice = (os.environ.get("SCORER_BACKEND") or "auto").lower()
    if choice == "auto":
        return "api" if os.environ.get("ANTHROPIC_API_KEY") else "agent"
    return choice


def run(postings: list[RawPosting]) -> tuple[list[Scored], str]:
    if not postings:
        return [], resolve_backend()
    backend = resolve_backend()
    model = os.environ.get("SCORER_MODEL") or DEFAULT_MODEL
    if backend == "agent":
        return _score_agent(postings), backend

    scored = _score_api(postings, model)

    # If the API failed for *everything*, the key is present but not usable —
    # wrong org, no credit, revoked. Without this the run reports "scored via
    # api", writes a feed full of nulls, and never emits candidates.json, so the
    # agent path silently stops working the moment a bad key exists. Fall back
    # rather than degrade to nothing.
    if scored and all(s.score is None for s in scored):
        reason = scored[0].why or "unknown error"
        print(
            f"  API scoring failed for all {len(scored)} postings ({reason});"
            " falling back to the agent backend",
            file=__import__("sys").stderr,
        )
        return _score_agent(postings), "agent (api unavailable)"

    return scored, backend
