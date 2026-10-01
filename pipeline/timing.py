"""Who to contact, through which channel, and when — as data rather than vibes.

Eric writes the email himself; this answers the three questions that come
before the writing and that actually stall a send: **who exactly**, **through what channel**,
and **why now**.

Every rule here traces to the research pass in `docs/research-landscape.md`. The
sourcing matters because the rules disagree with intuition in places, and because a
rule with a citation can be argued with later:

- **Who.** Under ~20 people or through Series A, the CEO is the hiring manager — mail
  them directly, not a recruiter (Kamerow). Past that the target shifts to the VP of
  the relevant function. At the stage this pipeline targets, that's almost always the
  founder, which is why founder enrichment is the enrichment.
- **When.** The single best moment is just after a funding round: the money is there
  and the roles often aren't posted yet (Lang, Keeley). SEC Form D filings land within
  15 days of a raise, so `boards/edgar.py` already gives us that trigger with a date —
  this module just reads it. Second-best trigger is a genuinely fresh posting.
- **Channel.** Warm beats cold, and an investor forward is a real path — one student
  landed an Eight Sleep internship that way (Keeley). Otherwise direct email beats
  LinkedIn messaging, with LinkedIn kept for the research that makes the email specific
  (Kamerow). X is a live channel for founders who actually post there, but "warm up
  first, then DM" is a slow play, so it ranks below email unless it's the only address.
- **Send window.** Tue-Thu, and early morning or evening for founders, who read their
  own inboxes outside business hours. Treat this as a nudge, not a science: the
  published response-rate tables are B2B *sales* benchmarks (~0.5% replies), and a
  personal note to a seed founder about a job is a different act entirely.

What this deliberately does NOT do: send anything, schedule anything, or turn a
personal email into a campaign. It ranks a short list and gets out of the way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .models import Entry

# Kamerow's stage rule. Bucket 1-2 is 1-50 people; a Series B+ company has a VP layer
# and the founder is no longer the hiring manager.
_LATE_STAGE = {"series_b", "series_c", "series_d", "series_e", "growth", "public", "ipo"}

# Post-funding hiring window. Form D is filed within 15 days of first sale, so a filing
# under ~60 days old is a live signal; past ~120 the moment has passed.
FUNDING_HOT_DAYS = 60
FUNDING_WARM_DAYS = 120

# A posting this new is worth jumping on — "apply within the first day or two" is
# repeatedly cited as a real edge.
POSTING_HOT_DAYS = 4
POSTING_WARM_DAYS = 21


@dataclass
class Assessment:
    company: str
    title: str
    url: str
    who: str = ""              # name of the person to contact
    who_title: str = ""
    who_reason: str = ""       # why this person, per the stage rule
    channels: list[str] = field(default_factory=list)   # ordered, best first
    trigger: str = ""          # why now, in one line
    urgency: int = 0           # 0-40, feeds priority
    send_when: str = ""        # next good send slot
    blockers: list[str] = field(default_factory=list)
    warm_path: str = ""
    priority: int = 0

    @property
    def ready(self) -> bool:
        return not self.blockers


# Written by funding.apply(); also the shape edgar entries carry in posted_at.
_FILED = re.compile(r"Form D filed (\d{4}-\d{2}-\d{2})")


def _funding_age(entry: Entry, today_: date, raised: str = "") -> int | None:
    """Days since the round closed, from whichever field records it. `raised`
    is the overlay's last_raised date — backfilled Form D history that never
    passed through a news flag, so it exists on no entry field."""
    m = _FILED.search(entry.funding or "")
    if m:
        return _days_since(m.group(1), today_)
    # A dated edgar entry carries the filing date in posted_at; an UNdated one
    # (the ghost rows sends builds for saved companies since the play
    # retirement) must still fall through to the overlay.
    if entry.source == "edgar" and entry.posted_at:
        return _days_since(entry.posted_at, today_)
    return _days_since(raised, today_)


def _days_since(iso: str, today_: date) -> int | None:
    if not iso:
        return None
    try:
        return (today_ - date.fromisoformat(iso[:10])).days
    except ValueError:
        return None


def pick_contact(entry: Entry) -> tuple[dict, str]:
    """(contact, reason). The stage rule decides whether the founder is the right door."""
    founders = entry.founders or []
    late = (entry.stage or "").lower() in _LATE_STAGE
    if not founders:
        return {}, ""
    ranked = sorted(
        founders,
        key=lambda f: (
            0 if re.search(r"\b(ceo|founder|co-?founder)\b", f.get("title", ""), re.I) else 1,
            0 if f.get("linkedin") else 1,
        ),
    )
    top = ranked[0]
    if late:
        return top, (
            "past Series A — the founder may no longer be the hiring manager; "
            "consider the VP of the function instead"
        )
    return top, "early stage — the founder is the hiring manager, so mail them directly"


def address_candidates(name: str, site: str) -> list[str]:
    """Likely address patterns for a founder — explicitly guesses, never facts.

    At early stage the overwhelmingly common pattern is firstname@company.com, moving
    to firstname.lastname@ around Series B (Kamerow). We generate the shortlist rather
    than asserting one, because a wrong address is a silently lost email and a
    confidently-wrong one is worse than a blank.

    Verify before sending — the drafts label these `pattern-guess` for that reason.
    """
    if not name or not site:
        return []
    domain = re.sub(r"^https?://(www\.)?", "", site).split("/")[0].lower()
    if not domain or "." not in domain:
        return []
    parts = [p for p in re.split(r"\s+", name.strip().lower()) if p.isalpha()]
    if not parts:
        return []
    first, last = parts[0], (parts[-1] if len(parts) > 1 else "")
    out = [f"{first}@{domain}"]
    if last:
        out += [f"{first}.{last}@{domain}", f"{first[0]}{last}@{domain}"]
    return out


def _channels(contact: dict, warm: str, site: str = "") -> tuple[list[str], list[str]]:
    """(ordered channels, blockers)."""
    out, blockers = [], []
    if warm:
        out.append(f"warm intro — {warm}")
    email = contact.get("email", "")
    conf = contact.get("email_confidence") or "none"
    if email:
        out.append(f"email {email}" + (f" ({conf})" if conf != "confirmed" else ""))
    else:
        guesses = address_candidates(contact.get("name", ""), site)
        if guesses:
            out.append("email (verify first) — try " + ", ".join(guesses))
        elif contact.get("name"):
            blockers.append("no email and no company domain to guess one from")
    if contact.get("x") or contact.get("twitter"):
        out.append(f"X {contact.get('x') or contact.get('twitter')}")
    if contact.get("linkedin"):
        out.append("LinkedIn — for research before writing, not as the send channel")
    if not contact.get("name"):
        blockers.append("no founder identified")
    return out, blockers


def next_send_slot(today_: date) -> str:
    """Next Tue-Thu, phrased as guidance rather than a scheduler."""
    wd = today_.weekday()          # Mon=0
    if wd in (1, 2, 3):
        return "today (Tue-Thu is the window) — early morning or after 6pm reads best"
    days_ahead = (1 - wd) % 7 or 7
    target = today_ + timedelta(days=days_ahead)
    return f"{target.isoformat()} (Tue) — founders read their own inbox early or late"


def assess(entry: Entry, today_: date | None = None, site: str = "",
           raised: str = "") -> Assessment:
    today_ = today_ or date.today()
    contact, reason = pick_contact(entry)
    channels, blockers = _channels(contact, entry.warm_path, site or entry.company_url)

    a = Assessment(
        company=entry.company,
        title=entry.title,
        url=entry.url,
        who=contact.get("name", ""),
        who_title=contact.get("title", ""),
        who_reason=reason,
        channels=channels,
        blockers=blockers,
        warm_path=entry.warm_path,
        send_when=next_send_slot(today_),
    )

    # --- why now -----------------------------------------------------------
    # A round is a round whoever spotted it. Gating this on source == "edgar"
    # meant a company `make funding` had just flagged scored zero urgency unless
    # EDGAR also happened to discover it — which defeats the watch, whose whole
    # job is companies already in the feed from other boards. The funding line
    # carries the filing date, so read it wherever it came from (today.py
    # already did; these two disagreed).
    funding_age = _funding_age(entry, today_, raised)
    posting_age = _days_since(entry.posted_at or entry.first_seen, today_)

    if funding_age is not None and funding_age <= FUNDING_HOT_DAYS:
        a.trigger = (
            f"filed a Form D {funding_age}d ago — money just landed and the roles "
            f"usually aren't posted yet. This is the best moment there is."
        )
        a.urgency = 40
    elif funding_age is not None and funding_age <= FUNDING_WARM_DAYS:
        a.trigger = f"raised ~{funding_age}d ago — still hiring, less of an edge"
        a.urgency = 22
    elif entry.funding and re.search(r"\b(seed|series a|pre-seed)\b", entry.funding, re.I):
        # Cut on a word boundary — "(…Accel, IA Ve)" read like a typo.
        fund = entry.funding[:64]
        if len(entry.funding) > 64:
            fund = fund[: fund.rfind(" ")] + "…"
        a.trigger = f"recently funded — {fund}"
        a.urgency = 18
    elif posting_age is not None and posting_age <= POSTING_HOT_DAYS:
        a.trigger = f"posting is {posting_age}d old — early applicants get read properly"
        a.urgency = 30
    elif posting_age is not None and posting_age <= POSTING_WARM_DAYS:
        a.trigger = f"posting is {posting_age}d old"
        a.urgency = 12
    else:
        a.trigger = "no time trigger — send on the strength of the role, not urgency"
        a.urgency = 0

    # --- priority ----------------------------------------------------------
    score = entry.score or 0
    a.priority = (
        int(score * 0.5)                      # fit still dominates
        + a.urgency
        + (15 if entry.warm_path else 0)      # warm beats cold, always
        - (25 if blockers else 0)             # can't send it yet
    )
    return a


# Statuses where a send is the next action. Past `applied` the follow-up queue owns it.
_SENDABLE = {"review", "saved"}


def queue(entries: list[Entry], limit: int = 10, min_score: int = 65,
          today_: date | None = None, views: list | None = None) -> list[Assessment]:
    """The short list of people to write to, best first.

    `views` lets a caller that has already derived the company rollup hand it
    over — the dashboard builds it for the Companies tab anyway, and deriving
    it twice re-reads the description store and both overlays for nothing.
    """
    from . import entities

    today_ = today_ or date.today()
    # Company sites live on the derived company view, not the entry — that's where a
    # guessable email domain comes from.
    cos = views if views is not None else entities.companies(entries)
    sites = {c.key: c.site for c in cos}
    from .models import normalize_company

    # Backfilled raise dates live only in the overlay (last_raised) — a round
    # spotted after the fact must rank exactly like one caught as news.
    overlay = entities.load_company_overlay()
    raised = {k: (v.get("last_raised") or {}).get("filed", "")
              for k, v in overlay.items()}

    out = []
    for e in entries:
        if e.status not in _SENDABLE:
            continue
        if (e.score or 0) < min_score:
            continue
        key = normalize_company(e.company)
        out.append(assess(e, today_, site=sites.get(key, ""),
                          raised=raised.get(key, "")))

    # Saved COMPANIES with a live raise trigger rank alongside the roles —
    # since the play retirement (2026-08-12) the trigger doesn't need a
    # posting to carry it. No score floor: saving the company was the yes.
    covered = {normalize_company(a.company) for a in out}
    for c in cos:
        if not c.tracked or c.not_interested or c.key in covered:
            continue
        filed = raised.get(c.key, "")
        age = _days_since(filed, today_)
        if age is None or age > FUNDING_WARM_DAYS:
            continue
        lr_url = (overlay.get(c.key, {}).get("last_raised") or {}).get("url", "")
        ghost = Entry(title="fresh raise — no posting yet", company=c.name,
                      url=lr_url or c.site, source="edgar", location="",
                      score=c.best_score, why="", founders=c.founders,
                      stage=c.stage)
        out.append(assess(ghost, today_, site=c.site, raised=filed))

    out.sort(key=lambda a: -a.priority)
    return out[:limit]


def render(assessments: list[Assessment], today_: date | None = None) -> str:
    today_ = today_ or date.today()
    if not assessments:
        return (
            "Nothing queued to send.\n"
            "Either nothing is shortlisted above the score floor, or everything already "
            "went out — check `make status`."
        )
    lines = [f"Send queue — {today_.isoformat()}", "=" * 46, ""]
    ready = [a for a in assessments if a.ready]
    blocked = [a for a in assessments if not a.ready]

    for a in ready:
        lines.append(f"[{a.priority:>3}] {a.company} — {a.title}")
        lines.append(f"       who: {a.who}" + (f", {a.who_title}" if a.who_title else ""))
        if a.who_reason:
            lines.append(f"            ({a.who_reason})")
        lines.append(f"       why now: {a.trigger}")
        for c in a.channels:
            lines.append(f"       via: {c}")
        lines.append(f"       when: {a.send_when}")
        lines.append(f"       {a.url}")
        lines.append("")

    if blocked:
        lines.append("-- not sendable yet " + "-" * 26)
        for a in blocked:
            lines.append(f"    {a.company} — {', '.join(a.blockers)}")
        lines.append("")
    lines.append("Nothing here sends anything — the email itself is Eric's to write.")
    return "\n".join(lines)
