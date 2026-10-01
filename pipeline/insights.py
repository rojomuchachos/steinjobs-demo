"""`make insights` — what has the app learned from Eric, and where is it wrong?

The pipeline already learns in two places: every `make mark NOTE=...` becomes a
calibration example the scorer reads, and history records everything that
happens. But nothing read that accumulation back, so systematic errors stayed
invisible until they bit — the GTM-Engineer mistake sat in plain sight for days
(Eric's own shortlist had one as a Top match while the scorer kept marking them
35) and was only caught because he said something.

This is the readback. Four sections:

  1. Agreement — where the scorer and Eric's verdicts disagree most, in both
     directions. High-score passes and low-score shortlists are the bugs.
  2. Your taste, in your own words — recurring reasons from pass notes, since
     a pattern in the whys is a rule the rubric doesn't state yet.
  3. Source ROI — which boards actually produce shortlists, not just volume.
     (This is the analysis that retired Techstars, now standing.)
  4. Outreach — which proof points get replies. Empty until sends happen, but
     the instrumentation records from draft one, so the answer exists the day
     there's data.

Read-only. The output is meant to be acted on by editing CLAUDE.md, the
prefilter, or the boards list — the report names the lever where it can.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import history as hist
from .models import Entry

POSITIVE = {"saved", "applied", "interviewing", "offer", "rejected", "dormant"}

# Words that carry no taste signal in pass-notes.
_STOP = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "it",
    "its", "this", "that", "to", "of", "in", "at", "for", "with", "on", "not",
    "no", "too", "so", "role", "company", "job", "than", "they", "their",
}

BAR = "─" * 72


def agreement(entries: list[Entry]) -> dict:
    judged = [e for e in entries if e.score is not None and e.status in POSITIVE | {"uninterested"}]
    pos = [e for e in judged if e.status in POSITIVE]
    neg = [e for e in judged if e.status == "uninterested"]

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    # The interesting rows are the disagreements.
    false_high = sorted((e for e in neg if (e.score or 0) >= 65), key=lambda e: -(e.score or 0))
    false_low = sorted((e for e in pos if (e.score or 0) <= 55), key=lambda e: (e.score or 0))
    return {
        "n_pos": len(pos),
        "n_neg": len(neg),
        "mean_pos": mean([e.score for e in pos]),
        "mean_neg": mean([e.score for e in neg]),
        "false_high": false_high[:6],
        "false_low": false_low[:6],
    }


def pass_reasons(calibration: list[dict]) -> list[tuple[str, int]]:
    """Recurring bigrams in Screened-out whys — taste the rubric doesn't state."""
    texts = [
        c.get("why", "") for c in calibration
        if c.get("verdict") == "Screened out" and c.get("why")
    ]
    grams: Counter = Counter()
    for t in texts:
        words = [w for w in re.findall(r"[a-z][a-z-]+", t.lower()) if w not in _STOP]
        grams.update(" ".join(p) for p in zip(words, words[1:]))
    return [(g, n) for g, n in grams.most_common(10) if n >= 2]


def source_roi(entries: list[Entry]) -> list[dict]:
    by: dict[str, dict] = defaultdict(lambda: {"n": 0, "scored": 0, "hits": 0, "live": 0})
    for e in entries:
        s = by[e.source or "?"]
        s["n"] += 1
        if e.score is not None:
            s["scored"] += 1
            if e.score >= 70:
                s["hits"] += 1
        if e.status in POSITIVE:
            s["live"] += 1
    out = []
    for src, s in by.items():
        rate = (s["hits"] / s["scored"] * 100) if s["scored"] else None
        out.append({"source": src, **s, "hit_rate": rate})
    return sorted(out, key=lambda r: -(r["hit_rate"] or -1))


def outreach_outcomes(entries: list[Entry]) -> dict:
    """Join outreach drafts (history carries the proof point) to replies."""
    h = hist.load()
    sent, replied = Counter(), Counter()
    for e in entries:
        evs = h.for_entry(e.url)
        drafts = [ev for ev in evs if ev.kind == "artifact" and "outreach" in ev.text]
        if not drafts:
            continue
        # the recorded detail carries "proof: <name>" when outreach logged it
        m = re.search(r"proof: ([^|]+)", drafts[-1].detail or "")
        proof = m.group(1).strip() if m else "(unrecorded)"
        if e.status in ("applied", "interviewing", "offer", "rejected"):
            sent[proof] += 1
            if e.status in ("interviewing", "offer"):
                replied[proof] += 1
    return {"sent": sent, "replied": replied}


def render(entries: list[Entry], calibration: list[dict]) -> str:
    # Migration rows are bookkeeping, not verdicts: the play retirement
    # (docs/outreach-plays-retirement.md, 2026-08-12) marked 51 EDGAR rows
    # uninterested with a status_note naming the doc. Counting them here
    # would read as "EDGAR produced 51 rejects" — noise the analysis section
    # would then recommend acting on.
    # Same for rule-change sweeps (senior titles, execution seats): their
    # status_note carries "migration (Eric," so bulk-applied policy is never
    # read as hundreds of individual taste verdicts.
    # And the automatic sweeps (score floor, stale review): the scorer
    # screening its own low scores is not Eric disagreeing with the scorer.
    entries = [e for e in entries
               if "outreach-plays-retirement" not in (e.status_note or "")
               and "migration (Eric," not in (e.status_note or "")
               and "auto-screened (policy" not in (e.status_note or "")
               and (e.status_note or "") != "swept — stale review, never touched"]
    out = ["", BAR, "INSIGHTS — what the app has learned, and where it's wrong", BAR]

    a = agreement(entries)
    out += ["", f"AGREEMENT  ({a['n_pos']} positive verdicts · {a['n_neg']} passes)"]
    if a["mean_pos"] is not None and a["mean_neg"] is not None:
        gap = a["mean_pos"] - a["mean_neg"]
        out.append(
            f"  scorer gives your keeps {a['mean_pos']:.0f} and your passes "
            f"{a['mean_neg']:.0f} on average (gap {gap:+.0f} — bigger is better)"
        )
    if a["false_high"]:
        out.append("  scored high, you passed — the scorer overrates these:")
        for e in a["false_high"]:
            out.append(f"    {e.score:>3}  {e.title[:40]:40} {e.company[:18]:18} ↳ {e.status_note[:40]}")
    if a["false_low"]:
        out.append("  scored low, you kept — the scorer underrates these:")
        for e in a["false_low"]:
            out.append(f"    {e.score:>3}  {e.title[:40]:40} {e.company[:18]}")
    if not a["false_high"] and not a["false_low"]:
        out.append("  no strong disagreements on record — feed it more verdicts to find bias")

    reasons = pass_reasons(calibration)
    if reasons:
        out += ["", "YOUR TASTE, IN YOUR OWN WORDS  (recurring pass reasons)"]
        for g, n in reasons:
            out.append(f"  {n:>2}×  “{g}”")
        out.append("  ↳ a recurring reason is a rule CLAUDE.md doesn't state yet — consider adding it")

    out += ["", "SOURCE ROI  (hit = scored 70+; the Techstars analysis, now standing)"]
    out.append(f"  {'source':<18}{'entries':>8}{'scored':>8}{'70+':>6}{'hit rate':>10}{'live':>6}")
    for r in source_roi(entries):
        rate = f"{r['hit_rate']:.0f}%" if r["hit_rate"] is not None else "—"
        out.append(f"  {r['source']:<18}{r['n']:>8}{r['scored']:>8}{r['hits']:>6}{rate:>10}{r['live']:>6}")

    oo = outreach_outcomes(entries)
    out += ["", "OUTREACH  (which proof points get replies)"]
    if not oo["sent"]:
        out.append("  no sends recorded yet — from your first `make mark ... STATUS=applied`,")
        out.append("  this section starts answering 'does the Jets or the band open more doors?'")
    else:
        for proof, n in oo["sent"].most_common():
            r = oo["replied"].get(proof, 0)
            out.append(f"  {proof:<34} sent {n:>2}  replied {r:>2}  ({r / n * 100:.0f}%)")

    out += ["", BAR,
            "Feed the loops: mark with NOTEs (taste), log replies (outreach),",
            "and this report gets sharper. It reads; you decide.", ""]
    return "\n".join(out)
