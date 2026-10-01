"""The free stage of scoring: drop obvious non-matches before spending tokens.

Deliberately conservative. A false drop here is invisible — the posting never
reaches the scorer and never appears in the feed — whereas a false pass just
costs a fraction of a cent. So when a signal is missing or ambiguous, it passes.

The Getro and ATS adapters hand us `stage`, `head_count`, `location` and comp as
structured fields, which covers most of the rubric's mechanical criteria (stage
25%, location 10%, comp 10%) and the "growth-stage / 100+ people" hard exclude.
What's left — role shape and agency fit, 35% — is genuine judgment, and that's
what the scorer is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .models import HEADCOUNT_CLEARLY_TOO_BIG, RawPosting, headcount_label

# Eric's rule is "last month, ideally two weeks, sooner is better". Applied as a
# hard drop at 30 days it deleted his own top matches: Hera's Founding GTM
# (evangelist) — the 88 — was 75 days old, and Conduct's London growth
# generalist, the one role that explicitly sponsors visas, was 82.
#
# The reason is structural, not incidental. At seed stage a founding role stays
# open for months precisely because the company is being picky about the first
# commercial hire. Age is a weak proxy for "filled" on exactly the roles this
# pipeline exists to find. Meanwhile the genuinely dead listings are obvious:
# Kingdom and Ataraxis both carry evergreen "looking for another role?" posts
# from 2021 and 2024.
#
# So the hard drop moves to where it catches the dead ones without the live
# ones, and 30+ becomes a scoring penalty instead (see recency.py). "Sooner is
# better" is honoured as a gradient, which is what it actually describes.
STALE_DAYS = 120
IDEAL_DAYS = 14
FRESH_DAYS = STALE_DAYS  # kept for the existing call signature
MAX_YEARS = 5   # Eric is ~0-1 yrs out; beyond this the role is not entry-level friendly

# CLAUDE.md hard excludes, as title patterns. Only unambiguous ones.
_EXCLUDE_TITLE = re.compile(
    r"\b("
    r"data scientist|data science|data analyst|analytics engineer|research scientist"
    r"|machine learning engineer|ml engineer|business intelligence|bi analyst"
    r"|software engineer|backend|frontend|full[\s-]?stack|devops|sre|qa engineer"
    r"|security engineer|mobile engineer|ios|android|platform engineer|data engineer"
    r"|consultant|consulting"
    r"|nurse|physician|therapist|clinician|pharmacist|technician|caregiver"
    r"|driver|warehouse|barista|cashier|janitor|custodian|maintenance"
    r"|accountant|controller|bookkeeper|paralegal|attorney|counsel"
    r"|recruiter|sales development representative|sdr|customer support"
    # K-12 / higher-ed staffing. Getro's Techstars network carries whole school
    # districts under a startup's name (Irving ISD appears as "Ripl", UW-La Crosse
    # as "FundMiner") — see _org_is_suspect in boards/getro.py. Excluding by role
    # kills this noise whether or not the attribution is wrong.
    r"|teacher|teaching assistant|substitute|principal|superintendent|professor"
    r"|instructional (coach|aide|coordinator)|paraprofessional|school counselor"
    r"|librar(y|ian)|registrar|bursar|dyslexia|special education|esl|bilingual aide"
    r"|academic advisor|admissions|athletic|head coach|assistant coach"
    # Plant / warehouse / trades. Same cause: food manufacturers surface under
    # pre-seed startup names.
    r"|machine operator|forklift|sanitation|production (line )?(lead|supervisor)"
    r"|operator mechanic|refrigeration|plumber|electrician|welder|hvac"
    r"|assembler|packer|picker|line cook|server|dishwasher|housekeep"
    r"|secretary|receptionist|clerk|administrative assistant|executive assistant"
    r")\b",
    re.I,
)

# Sales/service EXECUTION seats — measured 2026-08-17 as the biggest noise in
# the review pile (53 Account Executives, 30 Account Managers, 18 Customer
# Success, 19 Designers, all scoring under 55). These run someone else's
# playbook, which is the exact opposite of the spec. "Founding X" is exempt:
# a first-hire seat is early-employee shape and deserves a scored look.
# Solutions Engineer is deliberately NOT here — it's the technical-GTM family
# (GTM Engineer / FDE / Deployment Strategist) that CLAUDE.md actively wants.
_EXCLUDE_UNLESS_FOUNDING = re.compile(
    r"\b(account executive|account manager|customer success|designer)\b", re.I)
_FOUNDING = re.compile(r"\bfounding\b", re.I)

# Graduate-degree requirements (Eric, 2026-08-18). REQUIRED only — a
# "master's preferred" or "PhD a plus" survives, because preferred is not a
# gate and Eric's profile competes fine there. Bare "MS"/"MD" are never
# matched alone (Microsoft, Maryland); the degree word must sit in a
# requirement phrasing.
_GRAD_DEGREE = re.compile(
    r"(?:\b(?:ph\.?d|doctorate|doctoral degree|master'?s(?: degree)?|"
    r"graduate degree|m\.s\.|m\.sc|mba)\b[^.\n]{0,40}\b(?<!not )(?:required|is a requirement)\b"
    r"|\b(?:required|must (?:have|hold|possess))\b[^.\n]{0,40}"
    r"\b(?:ph\.?d|doctorate|doctoral degree|master'?s(?: degree)?|graduate degree|m\.sc)\b)",
    re.I,
)


def requires_grad_degree(text: str) -> bool:
    """True when the description makes a Masters/PhD a hard requirement."""
    return bool(_GRAD_DEGREE.search(text or ""))


# Where a posting LIVES says who actually employs you (Eric, 2026-08-18).
# Measured: 130+ walled rows were acquired brands filed under their startup
# name but hosted on the acquirer's careers site — Airkit on salesforce.com,
# Frame.io on adobe.com, Webroot on opentext.com, OPOWER on oracle.com,
# Instana on ibm.com. The acquired-brand rule says those ARE the acquirer,
# and CLAUDE.md hard-excludes big-company narrow-scope roles. The host is
# stated fact, not a guess, so this filter never invents an employer.
#
# myworkdayjobs.com is included on the same logic: Workday's enterprise
# contract floor means only large orgs run it. No early-stage startup does.
_BIGCO_HOSTS = re.compile(
    r"(?:^|\.)(?:"
    r"salesforce|oracle|ibm|adobe|hpe|hp|opentext|sap|microsoft|apple|meta|"
    r"cisco|intel|dell|vmware|servicenow|workday|intuit|paypal|ebay|netflix|"
    r"nvidia|qualcomm|siemens|accenture|deloitte|kpmg|pwc|ey|mckinsey|"
    r"jpmorganchase|goldmansachs|walmart|target|comcast|verizon|att"
    r")\.com$"
    r"|\.myworkdayjobs\.com$"
    r"|(?:^|\.)amazon\.jobs$",
    re.I,
)


def bigco_host(url: str) -> bool:
    """True when the posting is hosted on a mega-corp's own careers domain."""
    from urllib.parse import urlparse

    host = (urlparse(url or "").netloc or "").split(":")[0].lower()
    return bool(_BIGCO_HOSTS.search(host))

# Too senior for someone 0-1 years out. "senior"/"sr" added at Eric's explicit
# request — they were slipping through and wasting scoring budget.
_TOO_SENIOR = re.compile(
    r"\b(vp|vice president|svp|evp|chief|cto|ceo|cfo|coo|cmo|director"
    r"|principal|staff|distinguished|head of|senior|sr\.?)\b"
    r"|\(senior\)",
    re.I,
)

# Not career seats. Werkstudent is the German boards' working-student role.
_INTERNSHIP = re.compile(
    r"\b(intern|internship|co-?op|werkstudent|working student|summer 20\d\d"
    r"|apprentice(ship)?)\b",
    re.I,
)
# ...except these, where the title is senior-sounding but the role is 0->1.
_SENIOR_OK = re.compile(r"\b(chief of staff|founding|co-?founder)\b", re.I)

_YEARS = re.compile(r"(\d+)\+?\s*(?:-\s*\d+\s*)?year", re.I)


@dataclass
class Verdict:
    keep: bool
    reason: str = ""


def _too_experienced(description: str) -> bool:
    """True only if EVERY stated requirement exceeds 5 years.

    Postings often say "3-5 years" and later "10 years of industry context";
    requiring all matches to be high avoids dropping on an incidental mention.
    """
    years = [int(y) for y in _YEARS.findall(description or "")]
    return bool(years) and min(years) > MAX_YEARS


# Display-grade extraction (2026-08-11), stricter than the reject filter
# above: a shown tag that says "2+ yrs" because the blurb mentioned "2 years
# of runway" is invented data, so an "N years" phrase only counts when its
# neighborhood talks about the candidate. The reject filter can stay loose —
# min() just makes it conservative — but a rendered value can't.
_YOE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*\d{1,2}\s*)?(?:or more\s*)?\+?\s*"
    r"(years?|yrs?|yoe)\b",
    re.I)
_YOE_CTX = re.compile(
    r"experience|\bexp\b|background|track record|working|professional|"
    r"relevant|prior|proven|in (?:sales|marketing|growth|operations|"
    r"partnerships?|product|design|engineering|a similar)", re.I)


def required_years(text: str) -> int | None:
    """The posting's stated experience requirement, or None — never a guess.

    Takes the minimum across qualifying mentions ("2-4 years in sales, 6+
    preferred" is a 2), ignores "N years ago / of runway / since founding",
    and refuses numbers over 15 as obvious non-requirements.
    """
    if not text:
        return None
    found = []
    for m in _YOE.finditer(text):
        if "ago" in text[m.end():m.end() + 8].lower():
            continue
        window = text[max(0, m.start() - 70):m.end() + 70]
        # "2+ YOE" spells out its own context; plain "years" needs the window
        if m.group(2).lower() != "yoe" and not _YOE_CTX.search(window):
            continue
        n = int(m.group(1))
        if n <= 15:
            found.append(n)
    return min(found) if found else None


def check(p: RawPosting, fresh_days: int = FRESH_DAYS) -> Verdict:
    title = p.title or ""

    if _EXCLUDE_TITLE.search(title):
        return Verdict(False, "hard-excluded role type")

    if _EXCLUDE_UNLESS_FOUNDING.search(title) and not _FOUNDING.search(title):
        return Verdict(False, "sales/service execution seat — someone else's playbook")

    if _TOO_SENIOR.search(title) and not _SENIOR_OK.search(title):
        return Verdict(False, "too senior")

    if _INTERNSHIP.search(title):
        return Verdict(False, "internship — not a career seat")

    # Only drop on a date the board actually stated. Boards without dates
    # (generalist, Work at a Startup) carry every current top find — inferring
    # an age and deleting them would remove exactly what we're looking for.
    # See recency.py; those get a scoring penalty instead.
    if p.posted_at and p.posted_at < date.today() - timedelta(days=fresh_days):
        return Verdict(False, f"older than {fresh_days} days — evergreen/abandoned")

    # Growth-stage exclude. Bucket 3 (51-200) straddles the 100-person line, so
    # only 4+ is a confident drop — anything ambiguous goes to the scorer.
    if (
        p.head_count_bucket is not None
        and p.head_count_bucket >= HEADCOUNT_CLEARLY_TOO_BIG
    ):
        return Verdict(
            False, f"headcount {headcount_label(p.head_count_bucket)} — playbook written"
        )

    # Stage exclude, only when stated and clearly late.
    if p.stage in {"series_c", "series_d", "series_e", "public", "ipo"}:
        return Verdict(False, f"stage {p.stage}")

    # A board-stated minimum beats parsing prose out of the description.
    if p.min_experience is not None and p.min_experience > MAX_YEARS:
        return Verdict(False, f"requires {p.min_experience}+ years")

    if p.min_experience is None and _too_experienced(p.description):
        return Verdict(False, "requires >5 years")

    if requires_grad_degree(p.description):
        return Verdict(False, "requires a graduate degree")

    if bigco_host(p.url):
        return Verdict(False, "hosted on a mega-corp careers site — big-company seat")

    return Verdict(True)


def run(postings: list[RawPosting]) -> tuple[list[RawPosting], dict[str, int]]:
    kept: list[RawPosting] = []
    dropped: dict[str, int] = {}
    for p in postings:
        v = check(p)
        if v.keep:
            kept.append(p)
        else:
            dropped[v.reason] = dropped.get(v.reason, 0) + 1
    return kept, dropped
