"""`make today` — the morning view: what should I actually do right now?

The answer is currently spread across four surfaces — `status` for the funnel,
`followups` for nudges, the dashboard for browsing, `log` for a thread's story.
This collapses them into one prioritised list, ordered by what compounds:

  1. Replies you owe        (responded but untouched — a human is waiting)
  2. Nudges due             (day 4 / day 11; two is the budget)
  3. Stalled threads        (drafted but never sent, applied gone quiet)
  4. Ready to work          (shortlisted with artifacts already generated)
  5. Worth a look           (top unworked finds — capped, this is a morning
                             view, not the dashboard)

Everything here is derived from feed + history; nothing is stored. It's a lens,
not another state file.
"""

from __future__ import annotations

import re
from datetime import date

from . import history as hist
from . import status as st
from .models import Entry
from .brief import APPLICATIONS, slug

BAR = "─" * 72

# Mirrors funding.NEWS_DAYS — a round stops being a reason to write eventually.
FUNDING_NEWS_DAYS = 45


def _newsletter_leads() -> list[dict]:
    """Pending people leads from the substack queue (display only — the queue
    is cleared by `make person`, this lens stores nothing)."""
    import json

    from .boards.substack import PEOPLE_QUEUE

    try:
        rows = json.loads(PEOPLE_QUEUE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return rows if isinstance(rows, list) else []


def _days(iso: str) -> int | None:
    try:
        return (date.today() - date.fromisoformat(iso)).days
    except (ValueError, TypeError):
        return None


def _artifacts(e: Entry) -> list[str]:
    d = APPLICATIONS / slug(e.company)
    out = []
    for name, label in (("outreach.md", "outreach"), ("brief.md", "brief"), ("resume.pdf", "resume")):
        if (d / name).exists():
            out.append(label)
    return out


def build(entries: list[Entry], today_: date | None = None) -> dict:
    ref = today_ or date.today()

    replies_owed = []
    for e in entries:
        if e.status != "interviewing":   # post-rename: "they answered" state
            continue
        age = _days(e.last_touched)
        replies_owed.append((e, age or 0))
    replies_owed.sort(key=lambda t: -t[1])

    nudges = st.followups(entries, ref)

    # The pre-rename "drafted" state is gone, so drafted-and-never-sent no
    # longer exists as a distinct thing; applied-and-quiet is followups' job.
    stalled: list = []

    ready = [
        e for e in entries
        if e.status == "saved" and _artifacts(e)
    ]
    ready.sort(key=lambda e: -(e.score or 0))

    fresh = [
        e for e in entries
        if e.status == "review" and (e.score or 0) >= 70
    ]
    fresh.sort(key=lambda e: -(e.score or 0))

    # A round that closed in the last few weeks is the strongest reason to write
    # to anyone today, so it outranks the general "new and high-scoring" list.
    # The date comes from the entry's funding line when a news flag wrote one,
    # else from the overlay's backfilled last_raised — a round spotted after
    # the fact is the same trigger as one caught live.
    from . import entities as _ent
    from .models import normalize_company as _nc

    _overlay = _ent.load_company_overlay()
    just_raised = []
    for e in entries:
        if e.status == "uninterested":
            continue
        m = re.search(r"Form D filed (\d{4}-\d{2}-\d{2})", e.funding or "")
        filed = m.group(1) if m else (
            _overlay.get(_nc(e.company), {}).get("last_raised") or {}).get("filed", "")
        if not filed:
            continue
        age = (ref - date.fromisoformat(filed)).days
        if 0 <= age <= FUNDING_NEWS_DAYS:
            just_raised.append((e, age))
    just_raised.sort(key=lambda t: t[1])
    # One row per company — several open roles at one raise is still one event.
    seen_co, deduped = set(), []
    for e, age in just_raised:
        key = e.company.lower()
        if key in seen_co:
            continue
        seen_co.add(key)
        deduped.append((e, age))

    return {
        "replies_owed": replies_owed,
        "nudges": nudges,
        "stalled": stalled,
        "ready": ready,
        "fresh": fresh[:6],
        "just_raised": deduped[:6],
    }


def render(entries: list[Entry], today_: date | None = None) -> str:
    plan = build(entries, today_)
    out = ["", BAR, f"TODAY — {date.today().isoformat()}", BAR]
    n = 0

    if plan["replies_owed"]:
        out += ["", "ANSWER FIRST — they replied and are waiting on you:"]
        for e, age in plan["replies_owed"]:
            n += 1
            out.append(f"  {n}. {e.title} — {e.company}  ({age}d since their reply)")
            out.append(f"     make log COMPANY=\"{e.company}\"")

    if plan["nudges"]:
        out += ["", "NUDGES DUE (two is the whole budget):"]
        for e, age, num in plan["nudges"]:
            n += 1
            f = (e.founders or [{}])[0]
            who = f" — {f['name']}" if f.get("name") else ""
            out.append(f"  {n}. nudge #{num}: {e.title} — {e.company}{who}  ({age}d quiet)")

    if plan["just_raised"]:
        out += ["", "JUST RAISED — the best window there is:"]
        for e, age in plan["just_raised"]:
            n += 1
            f = (e.founders or [{}])[0]
            who = f" — write to {f['name']}" if f.get("name") else ""
            out.append(f"  {n}. {e.company} ({age}d ago){who}")
            out.append(f"     {e.funding[:70]}")

    leads = _newsletter_leads()
    if leads:
        out += ["", "NEWSLETTER LEADS — people to review:"]
        for r in leads[:6]:
            n += 1
            at = f" @ {r['company']}" if r.get("company") else ""
            role = f" ({r['role']})" if r.get("role") else ""
            hint = f"  [{r['contact_hint']}]" if r.get("contact_hint") else ""
            out.append(f"  {n}. {r.get('name', '?')}{at}{role}{hint} — per {r.get('pub', '?')}")
            if r.get("quote"):
                out.append(f"     “{r['quote'][:90]}”")
            out.append(f"     make person NAME=\"{r.get('name', '')}\" COMPANY=\"{r.get('company', '')}\""
                       f" NOTE=\"per {r.get('pub', '')}: {r.get('quote', '')[:60]}\"")
        if len(leads) > 6:
            out.append(f"     … and {len(leads) - 6} more in data/substack_people_queue.json")

    if plan["stalled"]:
        out += ["", "DRAFTED BUT NEVER SENT:"]
        for e, age in plan["stalled"]:
            n += 1
            out.append(f"  {n}. {e.title} — {e.company}  (draft sat {age}d)")
            out.append(f"     applications/{slug(e.company)}/outreach.md — send it or kill it")

    if plan["ready"]:
        out += ["", "READY TO SEND (shortlisted, artifacts already built):"]
        for e in plan["ready"][:5]:
            n += 1
            arts = ", ".join(_artifacts(e))
            out.append(f"  {n}. {e.score:>2}  {e.title} — {e.company}  [{arts}]")

    if plan["fresh"]:
        out += ["", "WORTH A LOOK (top unworked finds):"]
        for e in plan["fresh"]:
            n += 1
            out.append(f"  {n}. {e.score:>2}  {e.title} — {e.company}  ({e.location or 'location n/a'})")

    if n == 0:
        out += ["", "Nothing urgent. Run `make scout` for fresh postings, or work the", "shortlist in the dashboard: make dash"]

    out += ["", BAR,
            f"{len(plan['replies_owed'])} replies owed · {len(plan['nudges'])} nudges due · "
            f"{len(plan['stalled'])} stalled drafts · {len(plan['ready'])} ready · "
            f"{len(plan['fresh'])} fresh",
            ""]
    return "\n".join(out)
