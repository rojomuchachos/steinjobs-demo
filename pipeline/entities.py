"""Companies and People as first-class views.

The feed tracks postings, but postings are the most perishable of the three
things Eric actually works with: a posting expires, the company and the people
persist. This module gives the other two their own existence WITHOUT a parallel
database that could drift:

  - Companies and People are DERIVED from the feed on every read — rollups of
    postings, founders, scores, history.
  - Two small overlay files hold only what can't be derived: companies Eric
    tracks that have no posting yet, networking contacts, notes, and manual
    connection signals. data/companies.json and data/people.json.

Connection signals are the point of the People view: same school, same city,
same weird interest — the things that turn a cold email into a warm one. The
automatic pass matches against Eric's actual background (from master_cv.md);
manual signals can be added per person, because "met at a show in Atlanta"
isn't derivable from anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Entry, normalize_company, today

ROOT = Path(__file__).resolve().parent.parent
COMPANIES = ROOT / "data" / "companies.json"
PEOPLE = ROOT / "data" / "people.json"

# Eric's background, as matchable signals. Sources: master_cv.md. Each maps a
# needle (matched case-insensitively in bios/notes/context) to a human label.
BACKGROUND_SIGNALS = {
    "emory": "Emory",
    "northfield mount hermon": "NMH",
    "nmh": "NMH",
    # The official name is what an Experience entry carries, but a headline
    # almost never uses it — "Analytics, NY Jets" fired nothing until 2026-08-14.
    # Two rejected on measurement, both of which would put "we both worked for
    # the Jets" in front of the wrong person: bare "jets" (private/business jets
    # is ordinary English and this repo sees aerospace), and "the jets" — the
    # WINNIPEG Jets are in SPORTS_NEEDLES below, and their employees write "the
    # Jets" too. A miss here is recoverable; a wrong claim in an email is not.
    "new york jets": "NY Jets",
    "ny jets": "NY Jets",
    "jets football": "NY Jets",
    "nfl": "NFL / football analytics",
    "big data bowl": "Big Data Bowl",
    "ucsf": "UCSF",
    "neuroscape": "UCSF Neuroscape",
    "carter center": "Carter Center",
    "foxino": "Foxino",
    "ciee": "CIEE Prague",
    "prague": "Prague",
    "czech*": "Czech Republic",
    "atlanta": "Atlanta",
    "powerlift*": "powerlifting",
    "personal train*": "personal training",
    "pacific crest trail": "the PCT",
    "thru-hik*": "thru-hiking",
    "wwoof*": "WWOOF / farming",
    "trash compactor": "Trash Compactor",
}

# Sports is Eric's strongest opener after the schools — he did NFL data science
# for the Jets. All of it collapses to ONE "Sports" label: signals_in dedupes by
# label, so the first hit short-circuits the rest, and a generic chip can't
# license a claim the evidence doesn't support ("NBA: Jane" when Jane worked at
# a vendor selling TO the NBA). His own three — NY Jets, NFL, Big Data Bowl —
# keep their specific labels above; they're his résumé, not merely adjacent.
#
# THE RULE FOR ADDING ONE (Eric, 2026-08-07): worked in sports, not played —
# unless the league was pro. So no `varsity`, no `student-athlete`, no bare
# sport nouns; a pro career lands anyway because the bio names the league.
#
# THE RULE FOR SAFETY: a needle ships only if it cannot be ordinary English, a
# tech term, or a finance term. Every rejection below was measured firing on
# real text in this repo's own data.
SPORTS_NEEDLES = (
    # --- leagues & governing bodies -----------------------------------------
    # Rejected: f1 (UUID fragments — and "F1 score", the ML metric, in Eric's
    # own field), mls (Multiple Listing Service — real-estate tech is live
    # here), epl (forward-dEPLoyed, rEPLit — 158 hits), sec (this repo has an
    # EDGAR board), atp (ATP synthase), ioc (indicators of compromise),
    # "serie a" (survives next to "Series A" on one space).
    "nba", "wnba", "mlb", "nhl", "ncaa", "nwsl", "nascar", "indycar",
    "fifa", "uefa", "concacaf", "premier league", "major league soccer",
    "la liga", "bundesliga", "champions league", "formula 1", "formula one",
    "pga tour", "lpga", "atp tour", "ufc", "world cup", "olympic*",
    "paralympic*", "minor league baseball", "g league", "us soccer",
    # --- teams: CITY-QUALIFIED, never a bare mascot -------------------------
    # Measured false positives from bare mascots, anchoring already applied:
    # united→"United States", bills→"veterinary bills", city→"New York City"
    # (11 hits), stars→"GitHub stars", player→"adtech player", magic→"Magic
    # Compass Holdings". The mascot is a coin flip; the city makes it a fact.
    # "new york jets" is deliberately absent — it has its own label above.
    "arizona cardinals", "atlanta falcons", "baltimore ravens", "buffalo bills",
    "carolina panthers", "chicago bears", "cincinnati bengals", "cleveland browns",
    "dallas cowboys", "denver broncos", "detroit lions", "green bay packers",
    "houston texans", "indianapolis colts", "jacksonville jaguars",
    "kansas city chiefs", "las vegas raiders", "los angeles chargers",
    "los angeles rams", "miami dolphins", "minnesota vikings",
    "new england patriots", "new orleans saints", "new york giants",
    "philadelphia eagles", "pittsburgh steelers", "san francisco 49ers",
    "seattle seahawks", "tampa bay buccaneers", "tennessee titans",
    "washington commanders",
    "atlanta hawks", "boston celtics", "brooklyn nets", "charlotte hornets",
    "chicago bulls", "cleveland cavaliers", "dallas mavericks", "denver nuggets",
    "detroit pistons", "golden state warriors", "houston rockets",
    "indiana pacers", "los angeles clippers", "los angeles lakers",
    "memphis grizzlies", "miami heat", "milwaukee bucks", "minnesota timberwolves",
    "new orleans pelicans", "new york knicks", "oklahoma city thunder",
    "orlando magic", "philadelphia 76ers", "phoenix suns",
    "portland trail blazers", "sacramento kings", "san antonio spurs",
    "toronto raptors", "utah jazz", "washington wizards",
    "arizona diamondbacks", "atlanta braves", "baltimore orioles",
    "boston red sox", "chicago cubs", "chicago white sox", "cincinnati reds",
    "cleveland guardians", "colorado rockies", "detroit tigers", "houston astros",
    "kansas city royals", "los angeles angels", "los angeles dodgers",
    "miami marlins", "milwaukee brewers", "minnesota twins", "new york mets",
    "new york yankees", "oakland athletics", "philadelphia phillies",
    "pittsburgh pirates", "san diego padres", "san francisco giants",
    "seattle mariners", "st louis cardinals", "st. louis cardinals",
    "tampa bay rays", "texas rangers", "toronto blue jays",
    "washington nationals",
    "anaheim ducks", "boston bruins", "buffalo sabres", "calgary flames",
    "carolina hurricanes", "chicago blackhawks", "colorado avalanche",
    "columbus blue jackets", "dallas stars", "detroit red wings",
    "edmonton oilers", "florida panthers", "los angeles kings", "minnesota wild",
    "montreal canadiens", "nashville predators", "new jersey devils",
    "new york islanders", "new york rangers", "ottawa senators",
    "philadelphia flyers", "pittsburgh penguins", "san jose sharks",
    "seattle kraken", "st louis blues", "st. louis blues",
    "tampa bay lightning", "toronto maple leafs", "vancouver canucks",
    "vegas golden knights", "washington capitals", "winnipeg jets",
    "atlanta united", "austin fc", "charlotte fc", "chicago fire fc",
    "fc cincinnati", "colorado rapids", "columbus crew", "dc united",
    "fc dallas", "houston dynamo", "inter miami", "la galaxy", "lafc",
    "minnesota united", "nashville sc", "new england revolution",
    "new york city fc", "new york red bulls", "orlando city",
    "philadelphia union", "portland timbers", "real salt lake",
    "san jose earthquakes", "seattle sounders", "sporting kansas city",
    "toronto fc", "vancouver whitecaps",
    "new york liberty", "las vegas aces", "seattle storm", "chicago sky",
    "angel city fc", "gotham fc", "portland thorns", "washington spirit",
    "north carolina courage", "san diego wave",
    # European clubs — every one qualified, because bare "arsenal", "chelsea"
    # (a NYC neighborhood), "liverpool" (a city) and "ajax" (the web technique!)
    # are all ordinary words.
    "manchester united", "manchester city", "liverpool fc", "arsenal fc",
    "chelsea fc", "tottenham hotspur", "real madrid", "fc barcelona",
    "atletico madrid", "bayern munich", "borussia dortmund",
    "paris saint-germain", "juventus", "ac milan", "inter milan", "afc ajax",
    "celtic fc", "rangers fc",
    # --- sports-adjacent employers ------------------------------------------
    # Rejected: puma (the Ruby app server — engineer bios), "on running" ("on
    # running a distributed team"), equinox, peloton (Peloton Therapeutics).
    "draftkings", "fanduel", "sportradar", "genius sports", "stats perform",
    "second spectrum", "hudl", "catapult sports", "teamworks", "statsbomb",
    "pro football focus", "bleacher report", "front office sports", "sportico",
    "sports business journal", "swish analytics", "alt sports data", "sorare",
    "onefootball", "fanatics", "athletes unlimited", "topgolf", "gatorade",
    # --- work language: PHRASES ONLY ----------------------------------------
    # Bare `sports` is rejected on measured evidence — it fires once on the
    # person surface today and it's wrong: a sweep note reading "Corgi = YC,
    # insurance infra; sports/entertainment arm is Golden by Corgi" describes
    # the employer's SIBLING division. Also rejected: coach ("executive coach",
    # "recovery coach"), draft ("draft comms"), scout (this repo's own `make
    # scout`), league ("Ivy League"), cycling ("battery-reCYCLING"), player
    # ("adtech player"), franchise ("private banking franchises").
    "sports analytics", "sports science", "sports medicine",
    "sports performance", "sports tech", "sports technology", "sports-tech",
    "sports betting", "sports media", "sports marketing", "sports management",
    "sports business", "sports agency", "sports agent", "sports data",
    "sports franchise", "sports team", "sports league", "sports nutrition",
    "sports psychology", "sports partnerships", "sports operations",
    "sports content", "sports information", "sportsbook", "esports", "e-sports",
    "fantasy sports", "women's sports", "womens sports", "youth sports",
    "professional sports", "pro sports",
    # `athletic*` was missing and cost a real find: Riley Okafor's "Marketing
    # Intern, Stanford Athletics" fired nothing, because the list only had the
    # spelled-out phrases below and a college athletic department is named
    # "<School> Athletics" (validated against 9 real profiles, 2026-08-07).
    # Safe: all 3 corpus hits are Citadel Athletics, Southeastern Louisiana
    # Athletics, and a UND Athletic Department — and "athlete" is a different
    # word, so the amateur markers stay out of reach.
    "athletic*",
    "athletic director", "athletic department", "athletic trainer",
    "head coach", "assistant coach", "coaching staff",
    "strength and conditioning", "strength coach",
    "player development", "player personnel", "player scouting",
    "sabermetrics", "statcast", "next gen stats",
    # Sport nouns only inside work phrases — bare "soccer"/"baseball" would
    # catch "varsity soccer" and "D3 baseball", which is exactly the amateur
    # playing Eric excludes.
    # Bare sport nouns, added 2026-08-07 after a real-connections run missed
    # "Football Solutions at The 33rd Team". They were excluded originally
    # because "varsity soccer" would fire — but the amateur guard now handles
    # exactly that, and every corpus hit for these is real: Jobs In Football,
    # Canadian Football League, USA Hockey, Nashville Soccer Club, USA Rugby.
    "football", "basketball", "baseball", "hockey", "soccer", "lacrosse",
    "rugby", "softball", "volleyball",
    "sumersports",   # football analytics; the name hides "sports" mid-word
    "soccer analytics", "basketball operations", "baseball operations",
    "football operations", "hockey operations", "soccer operations",
    "professional athlete", "professional soccer", "professional basketball",
    "professional baseball", "professional football", "professional hockey",
)

# Music: Eric co-founded a band and ran its growth, marketing and bookings to
# profitability, so a fellow musician is a real bond — and unlike sports, the
# amateur COUNTS (Eric, 2026-08-07: "worked in anything involving music, play
# music, consider them"). No worked-not-played guard here on purpose.
#
# The trap is different from sports too: music's vocabulary is tech's
# vocabulary. Measured in this repo's corpus — `label` hits "Labeled BD",
# `producer` hits a contract producer role (and Kafka), `band` hits "maps onto
# the band", `tour` hits "Tour-of-duty language", `studio` hits "Game-studio
# live-ops", `records` hits "Student Records Lead", `sound` hits "sounds like
# execution", `mix` hits "sales mixed", `genius` hits "Genius AI" x11. All
# rejected; every needle below is a phrase or an unambiguous noun. Bare
# `music*` was checked and is safe: 33 corpus hits, all real music companies.
MUSIC_NEEDLES = (
    # `music*` was one needle until "musical chairs of vendors" fired on it —
    # the adjective is business-speak, the noun isn't. Closed tail, and the
    # real "musical …" phrases are spelled out.
    "music", "musician*", "musical director", "musical theater",
    "musical theatre",
    "guitarist", "bassist", "drummer", "vocalist", "saxophonist",
    "pianist", "violinist", "cellist", "trumpeter", "percussionist",
    "keyboardist", "songwriter", "composer", "lyricist", "disc jockey",
    "record label", "indie label", "major label", "label services",
    "tour manager", "tour management", "touring musician", "on tour with",
    "a&r", "recording studio", "recording artist", "session musician",
    "session player", "live sound", "sound engineer", "audio engineer",
    "mixing engineer", "mastering engineer", "concert venue", "booking agent",
    "talent buyer", "concert promoter", "artist relations",
    "artist development", "artist management", "band manager",
    "spotify", "bandcamp", "soundcloud", "songkick", "bandsintown",
    "live nation", "aeg presents", "sofar sounds", "splice", "distrokid",
    "tunecore", "unitedmasters", "beatport", "discogs", "pitchfork",
    "hardcore band", "punk band", "metal band*", "jazz ensemble", "orchestra",
    "symphony", "choir", "marching band", "conservatory", "berklee",
    "juilliard",
)

# Neuroscience: research OR a company working in it (Eric, 2026-08-07).
# `neuro*` is the whole game and it is SAFE — "neural" cannot match it
# ("neur-A-l" vs "neur-O"), so neural networks and Neuralink don't fire, while
# neuroscience / neurotech / neurology / neuroimaging all do.
NEURO_NEEDLES = (
    "neuro*", "cognitive science", "brain-computer interface",
    "brain computer interface", "bci", "fmri", "eeg", "electrophysiology",
    "psychophysics", "epilep*", "connectome", "optogenetic*",
)

# Psychedelics: "anything involving psychedelics" (Eric, 2026-08-07) — the
# broadest of the three, because the field is small enough that a mention is
# almost always the real thing. `maps` is deliberately absent despite being
# THE psychedelics nonprofit: the corpus says "maps onto the band".
PSYCHEDELIC_NEEDLES = (
    "psychedelic*", "psilocybin", "mdma", "ketamine", "ayahuasca", "ibogaine",
    "mescaline", "entheogen*", "microdos*", "5-meo",
    "multidisciplinary association for psychedelic",
    "compass pathways", "atai life sciences", "mindmed", "usona",
    "field trip health", "numinus", "mindbloom", "journey clinical",
)

BACKGROUND_SIGNALS.update({n: "Sports" for n in SPORTS_NEEDLES})
BACKGROUND_SIGNALS.update({n: "Music" for n in MUSIC_NEEDLES})
BACKGROUND_SIGNALS.update({n: "Neuroscience" for n in NEURO_NEEDLES})
BACKGROUND_SIGNALS.update({n: "Psychedelics" for n in PSYCHEDELIC_NEEDLES})

# Worked, not played (Eric, 2026-08-07) — unless the league was pro. A pro
# career needs no special handling: the bio names the league and the league
# needle fires. But a COLLEGE athlete's bio names a league too — "former NCAA
# Division I athlete" hits `ncaa` — so an otherwise-good sports hit is dropped
# when amateur-playing language sits right beside it. Same shape as _negated,
# different cues, and it looks forward as well as back because the giveaway
# usually trails the league name.
_AMATEUR = re.compile(
    r"\b(varsity|intramural|walk[- ]?on|student[- ]athlete|club team|"
    r"colleg(?:e|iate) athlete|division\s+(?:i{1,3}|[123])\b|d[123]\b|"
    # LinkedIn's own education-section header. Everything filed under it is a
    # club or a team someone PLAYED on — "Activities and societies: Women's
    # Basketball" is exactly the amateur case Eric excludes, and bare sport
    # nouns would otherwise fire on it.
    r"activities and societies)", re.I
)
_AMATEUR_WINDOW = 70


def _amateur_context(low: str, start: int, end: int) -> bool:
    """Is this sports mention someone's college playing career, not their job?"""
    window = low[max(0, start - _AMATEUR_WINDOW):end + _AMATEUR_WINDOW]
    return bool(_AMATEUR.search(window))


def signal_pattern(needle: str) -> re.Pattern:
    """Compile one needle. Shared by both matchers — two copies of this logic
    is how they drifted apart in the first place (the word-anchoring fix landed
    in enrich.py on 2026-08-07 and never reached signals_in, which was still
    minting an NFL signal for twelve companies off "influencers"/"inflection").

    Three rules, each paid for:

    1. `\\b` at the START — a bare substring test claimed Eric went to Emory off
       text saying "mEMORY", and found the NFL inside minified "uNFLatten".
    2. `\\b` at the END, unless the needle opts out with a trailing `*`. An open
       tail reads "$NFLX" as the NFL. Only the deliberate stems star themselves.
    3. Internal whitespace matches `\\s+` — strip_markup() turns every tag into a
       space, so "<b>New York</b> <b>Jets</b>" arrives as "New York   Jets" and
       a literal-space needle missed the strongest thread Eric has.
    """
    open_tail = needle.endswith("*")
    core = needle[:-1] if open_tail else needle
    body = r"\s+".join(re.escape(tok) for tok in core.split())
    return re.compile(r"\b" + body + (r"\w*" if open_tail else r"\b"), re.I)


# Precompiled once — the needle count grows with the sports list, and
# find_warm_thread runs this over whole pages.
_SIGNAL_PATTERNS = [(signal_pattern(n), label) for n, label in BACKGROUND_SIGNALS.items()]


# A sweep note that RULES a school OUT still contains the school's name, and a
# plain substring match reads that as a hit: "No Emory/NMH tie" made Riley Okafor
# display as both an Emory and an NMH alum (2026-08-06). That is the exact
# failure CLAUDE.md forbids — a "same school" claim about a stranger who never
# went there, one step from an email to them. So a mention inside a negation's
# reach doesn't count; only an unnegated one does.
_NEGATION = re.compile(r"\b(no|not|never|non|without|nothing|n't)\b")
# How far back a negation cue still governs, and what cuts its scope short.
_NEG_WINDOW = 48
_CLAUSE_END = re.compile(r"[.;!?,\n]")


def _negated(low: str, start: int) -> bool:
    """Is the mention at `start` inside the reach of a preceding negation?"""
    window = low[max(0, start - _NEG_WINDOW):start]
    # A clause boundary ends the negation's reach: in "not Emory. Emory Law",
    # the second mention stands on its own.
    last_break = None
    for m in _CLAUSE_END.finditer(window):
        last_break = m.end()
    if last_break is not None:
        window = window[last_break:]
    return bool(_NEGATION.search(window))


def signals_with_evidence(text: str) -> list[tuple[str, str]]:
    """(label, the words that matched, in context) for every signal in `text`.

    The evidence half stopped being optional once profile text moved to a
    sidecar cache: a chip can now come from a paragraph Eric never sees, and
    "Sports" with nothing behind it is one hover away from him writing "I saw
    you worked in sports too" to someone whose profile said something else.
    Same snippet shape find_warm_thread already produces.
    """
    raw = text or ""
    low = raw.lower()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pattern, label in _SIGNAL_PATTERNS:
        if label in seen:
            continue
        for m in pattern.finditer(low):
            if _negated(low, m.start()):
                continue
            if label == "Sports" and _amateur_context(low, m.start(), m.end()):
                continue
            snippet = " ".join(raw[max(0, m.start() - 60):m.end() + 60].split())
            seen.add(label)
            out.append((label, f"…{snippet}…"))
            break
    return out


def signals_in(text: str) -> list[str]:
    return [label for label, _ in signals_with_evidence(text)]


def _profile_sections(linkedin: str) -> list[tuple[str, str]]:
    """Captured profile, split by provenance, strongest section first."""
    if not linkedin:
        return []
    from . import profiles

    return profiles.split_sections(profiles.get(linkedin))


def _employer_signals(company: str) -> list[tuple[str, str]]:
    """Signals a person inherits from WHERE THEY WORK.

    Riley Okafor's profile never says "sports" — it says she does BI at Elevate,
    and Elevate is a sports agency. That is her own text about her own job, and
    the classification comes from the company's own description, so it clears
    the honesty rule: neither half is Eric-side context. Without this the only
    thing that fired was a student internship from six years earlier.
    """
    if not company:
        return []
    key = normalize_company(company)
    rec = load_company_overlay().get(key) or {}
    blob = " ".join(x for x in (rec.get("description", ""), rec.get("why", "")) if x)
    # FAMILY labels only. Eric's own three — NY Jets, NFL / football analytics,
    # Big Data Bowl — are his credentials, and inheriting them from an
    # employer's blurb overclaims: Priya Castell does partnerships at a sports
    # agency whose description happens to list leagues, and she came out
    # tagged "NFL / football analytics" (caught live, 2026-08-07). Working at
    # a sports company makes someone Sports, never a Big Data Bowl finalist.
    return [(l, w) for l, w in signals_with_evidence(blob)
            if l not in ("NY Jets", "NFL / football analytics", "Big Data Bowl")]


def _profile_text(linkedin: str) -> str:
    """Captured LinkedIn profile text for one person, or "" if never swept.
    Lazy import: the cache is a leaf module and entities is imported everywhere.
    """
    if not linkedin:
        return ""
    from . import profiles

    return profiles.get(linkedin)


# --- overlays ---------------------------------------------------------------


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8") or "null") or default
    except json.JSONDecodeError:
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_company_overlay() -> dict:
    """{normalized_name: {name, why, notes, tracked, added, site}}"""
    return _load_json(COMPANIES, {})


def save_company_overlay(d: dict) -> None:
    _save_json(COMPANIES, d)


def load_people_overlay() -> list[dict]:
    """[{name, company, role, linkedin, relationship, signals, notes, added}]"""
    return _load_json(PEOPLE, [])


def save_people_overlay(rows: list[dict]) -> None:
    _save_json(PEOPLE, rows)


def track_company(name: str, why: str = "", site: str = "", notes: str = "",
                  linkedin: str = "", description: str = "",
                  industry: str = "", stage: str = "", location: str = "",
                  follow: bool = True) -> dict:
    d = load_company_overlay()
    key = normalize_company(name)
    rec = d.get(key, {"name": name, "added": today()})
    if why:
        if rec.get("why") != why:
            rec["why_date"] = today()
        rec["why"] = why
    if site:
        rec["site"] = site
    if linkedin:
        rec["linkedin"] = linkedin.rstrip("/")
    if description:
        rec["description"] = description
    if industry:
        rec["industry"] = industry
    if stage:
        # One spelling per stage — variants made dead filter rows in the UI.
        from .models import normalize_stage as _ns

        rec["stage"] = _ns(stage) or rec.get("stage", "")
    if location:
        rec["location"] = location
    if notes:
        rec["notes"] = (rec.get("notes", "") + "\n" + notes).strip()
    if not follow:
        rec["edited"] = today()   # the edit form came through here
    if follow:
        # The edit form reuses this path with follow=False so fixing a typo
        # in a description can't silently move a company to Saved.
        rec["tracked"] = True
    d[key] = rec
    save_company_overlay(d)
    return rec


def discover_company(d: dict) -> bool:
    """A board found a company with no posting to hang it on (EDGAR fresh
    raises since the play retirement, docs/outreach-plays-retirement.md).

    Writes it into the overlay in review — tracked/not_interested are never
    touched, so a discovery can neither un-save nor resurrect a dismissed
    company. Data honesty rules apply: fills are blanks-only, last_raised
    moves forward only, and a re-seen filing (same URL) is not news.

    Returns True when this was actually new — a new company, or a newer
    filing on a known one — so the scout can count discoveries honestly.
    """
    ov = load_company_overlay()
    key = normalize_company(d.get("name", ""))
    if not key:
        return False
    rec = ov.get(key)
    lr = d.get("last_raised") or {}
    prior_lr = (rec or {}).get("last_raised") or {}
    is_new_company = rec is None
    newer_filing = bool(lr.get("filed")) and lr.get("filed", "") > prior_lr.get("filed", "")
    if rec is None:
        rec = ov[key] = {"name": d["name"], "added": today()}
    for f in ("site", "location", "stage", "industry", "description"):
        if d.get(f) and not rec.get(f):
            rec[f] = d[f]
    if d.get("founders") and not rec.get("founders"):
        rec["founders"] = d["founders"]
    # The why is the timing trigger — a NEWER raise may replace an older one,
    # but never a hand-written reason (why_date from an edit means Eric wrote it).
    if d.get("why") and (not rec.get("why") or (newer_filing and not rec.get("edited"))):
        rec["why"] = d["why"]
        rec["why_date"] = today()
    if newer_filing:
        rec["last_raised"] = {k: lr.get(k, "") for k in ("filed", "amount", "url")}
    save_company_overlay(ov)
    return is_new_company or (newer_filing and lr.get("url", "") != prior_lr.get("url", ""))


COMPANY_MODES = ("review", "saved", "uninterested")
LEGACY_COMPANY_MODE = {"following": "saved", "dismissed": "uninterested"}


def set_company_mode(name: str, mode: str) -> dict:
    """One knob instead of two toggles: review (undecided, the default),
    saved, or uninterested. Mutually exclusive by construction."""
    mode = LEGACY_COMPANY_MODE.get(mode, mode)
    if mode not in COMPANY_MODES:
        raise ValueError(f"unknown company mode: {mode!r} (use {', '.join(COMPANY_MODES)})")
    d = load_company_overlay()
    key = normalize_company(name)
    rec = d.setdefault(key, {"name": name, "added": today()})
    if mode == "saved" and not rec.get("tracked"):
        rec["saved_at"] = today()   # "sort saved companies by when I chose them"
    rec["tracked"] = mode == "saved"
    rec["not_interested"] = mode == "uninterested"
    save_company_overlay(d)
    if mode == "saved":
        # Favoriting a company queues its alumni/mutual sweep automatically.
        queue_alumni(name)
    if mode == "uninterested":
        # A company Eric passed on shouldn't leave its people sitting in his
        # actionable pile (Eric, 2026-08-18). Only UNTOUCHED people follow it
        # down: anyone already saved, contacted or further along represents a
        # real relationship, and a company decision must never quietly bury a
        # conversation in progress.
        n = 0
        dropped_names = []
        for r in load_people_overlay():
            if normalize_company(r.get("company", "")) != key:
                continue
            if (r.get("status") or "review") != "review":
                continue
            set_person_status(r["name"], r.get("company", ""), "uninterested",
                              method="cascade",
                              detail=f"company marked uninterested: {name}")
            dropped_names.append((r.get("name") or "").lower().strip())
            n += 1
        if n:
            rec["people_cascaded"] = n
        # Dequeue what the cascade just retired. Without this the Chrome
        # sittings keep spending their capped slots reading profiles at a
        # company Eric already passed on — the sitting drains by name/company
        # and never consults status (flagged by the sitting session,
        # 2026-08-18). Dropping here beats skipping at drain time, which
        # silently wastes a slot per stale entry.
        _dequeue_alumni(key)
        if dropped_names:
            _dequeue_person_fills(dropped_names)
    return rec


def _dequeue_alumni(company_key: str) -> int:
    """Remove a company from the alumni sweep worklist."""
    import json as _json

    try:
        q = _json.loads(ALUMNI_QUEUE.read_text())
    except (OSError, ValueError):
        return 0
    keep = [e for e in q
            if normalize_company(e.get("company", "")) != company_key]
    if len(keep) != len(q):
        ALUMNI_QUEUE.write_text(_json.dumps(keep, indent=2, ensure_ascii=False) + "\n")
    return len(q) - len(keep)


def _dequeue_person_fills(names: list[str]) -> int:
    """Remove people from the profile-fill worklist, matched by name."""
    import json as _json

    wanted = {n for n in names if n}
    if not wanted:
        return 0
    try:
        q = _json.loads(PERSON_FILL_QUEUE.read_text())
    except (OSError, ValueError):
        return 0
    keep = [e for e in q if (e.get("name") or "").lower().strip() not in wanted]
    if len(keep) != len(q):
        PERSON_FILL_QUEUE.write_text(
            _json.dumps(keep, indent=2, ensure_ascii=False) + "\n")
    return len(q) - len(keep)


ALUMNI_QUEUE = ROOT / "data" / "alumni_queue.json"

# The sweep itself is a Chrome-session task (LinkedIn is login-walled and the
# repo never scrapes it headlessly). This queue is the worklist that session
# walks — throttling happens there, a handful of companies per sitting, not
# here. Fully-checked companies (every pass in alumni_checked) never queue.
#
# The third pass is not a school: Eric did NFL data science for the Jets, so a
# former Jets employee is the same kind of warm thread an Emory alum is, and
# the same keyword page finds them (LinkedIn's company keyword matches
# EMPLOYMENT as well as education — the thing that makes it noisy for schools
# is exactly what makes it work here). Validated 2026-08-14 on KPMG US: 5 hits,
# and the two checked were both real Jets alumni. `alumni_checked` keeps its
# name; the values are sweep passes, not degrees.
ALUMNI_PASSES = ("emory", "nmh", "jets")


def queue_alumni(name: str) -> bool:
    import json as _json

    key = normalize_company(name)
    overlay = load_company_overlay()
    rec = overlay.get(key, {})
    checked = rec.get("alumni_checked", {})
    if set(ALUMNI_PASSES) <= set(checked):
        return False
    # A blocked company has no reachable page at all. Without this it re-enters
    # the worklist on every favorite and every sitting re-decides not to sweep
    # it. An explicit not-interested is the same waste for a different reason:
    # Eric already said no, so don't spend LinkedIn loads finding him alumni there.
    if rec.get("sweep_blocked") or rec.get("not_interested"):
        return False
    try:
        q = _json.loads(ALUMNI_QUEUE.read_text())
    except (OSError, ValueError):
        q = []
    if any(normalize_company(x.get("company", "")) == key for x in q):
        return False
    q.append({"company": rec.get("name", name), "linkedin": rec.get("linkedin", ""),
              "queued": today(),
              "pending": [s for s in ALUMNI_PASSES if s not in checked]})
    ALUMNI_QUEUE.write_text(_json.dumps(q, indent=2, ensure_ascii=False) + "\n")
    return True


PERSON_FILL_QUEUE = ROOT / "data" / "person_fill_queue.json"

def queue_person_fill(rec: dict) -> bool:
    """Queue a person's profile for the Chrome sitting when the record is
    missing what the fill provides (role or location). Saving or manually
    adding an unenriched person triggers this — pasting a URL never does
    (Eric, 2026-08-06). No LinkedIn URL means nothing to visit: skip."""
    import json as _json

    url = (rec.get("linkedin") or "").rstrip("/")
    if "linkedin.com/in/" not in url:
        return False
    if rec.get("role") and rec.get("location"):
        return False
    try:
        q = _json.loads(PERSON_FILL_QUEUE.read_text())
    except (OSError, ValueError):
        q = []
    if any((x.get("linkedin") or "").rstrip("/") == url for x in q):
        return False
    q.append({"linkedin": url, "name": rec.get("name", ""), "queued": today()})
    PERSON_FILL_QUEUE.write_text(_json.dumps(q, indent=2, ensure_ascii=False) + "\n")
    return True


def set_company_interest(name: str, interested: bool) -> dict:
    """Mark a company not-interested (hidden from the default list) or restore it."""
    d = load_company_overlay()
    key = normalize_company(name)
    rec = d.setdefault(key, {"name": name, "added": today()})
    rec["not_interested"] = not interested
    if not interested:
        rec["tracked"] = False        # can't follow and dismiss at once
    save_company_overlay(d)
    return rec


def set_company_linkedin(name: str, url: str) -> None:
    """Store a company's LinkedIn page — found once, never searched again."""
    d = load_company_overlay()
    key = normalize_company(name)
    rec = d.setdefault(key, {"name": name, "added": today()})
    rec["linkedin"] = url.rstrip("/")
    save_company_overlay(d)


def resolve_company(name: str) -> str:
    """Map a board's label onto the entity that actually exists.

    Getro files postings under brands their owner bought years ago — HPE jobs
    under "Nimble Storage", Airbnb's under "HotelTonight". Renaming the feed
    once fixed the rows on the page and nothing else: the very next scout
    re-imported four more HPE postings under the dead brand (2026-08-07). The
    rule has to run at IMPORT, so it reads the acquirer's own `prior_names`
    and re-files on the way in.
    """
    key = normalize_company(name)
    for rec in load_company_overlay().values():
        for prior in rec.get("prior_names") or []:
            if normalize_company(prior) == key:
                return rec.get("name", name)
    return name


def mark_alumni_checked(name: str, school: str) -> None:
    """Remember that a school sweep ran for this company (worklist skips it)."""
    d = load_company_overlay()
    key = normalize_company(name)
    rec = d.setdefault(key, {"name": name, "added": today()})
    rec.setdefault("alumni_checked", {})[school] = today()
    save_company_overlay(d)


# The outreach pipeline for a person, in order. "review" is the implicit
# default for derived people (surfaced, not chosen); a hand-recorded person
# starts at "saved". Dormant is the honest state for threads that went quiet;
# uninterested is Eric's own pass.
PERSON_STAGES = ("review", "saved", "contacted", "conversation", "met",
                 "dormant", "uninterested")

# Pre-rename spellings (2026-07) — normalized on every read and write.
LEGACY_PERSON_STAGE = {
    "to contact": "saved",
    "reached out": "contacted",
    "replied": "conversation",
    "meeting": "met",
}


def record_nudge(name: str, company: str = "", when: str = "") -> None:
    """A follow-up went out. History only — the stage doesn't move; make
    followups' day-4/day-11 budget is the policy, this is the receipt."""
    from . import history as hist

    hist.record(person_key(name, company), "nudge", "follow-up sent", at=when or "")


def person_key(name: str, company: str = "", linkedin: str = "") -> str:
    """Stable history key: their LinkedIn URL when known, else name@company."""
    if linkedin:
        from .models import normalize_url

        return normalize_url(linkedin)
    return f"person:{name.lower().strip()}|{normalize_company(company)}"


def set_person_follow(name: str, company: str, following: bool) -> dict:
    """Follow a person. Feed-derived founders get an overlay row on first
    follow, so the flag survives rebuilds like every other manual fact."""
    rows = load_people_overlay()
    key = (name.lower().strip(), normalize_company(company))
    for r in rows:
        if (r["name"].lower().strip(), normalize_company(r.get("company", ""))) == key:
            r["following"] = bool(following)
            save_people_overlay(rows)
            return r
    rec = {"name": name.strip(), "company": company.strip(), "role": "",
           "linkedin": "", "relationship": "networking", "signals": [],
           "notes": "", "added": today(), "following": bool(following)}
    rows.append(rec)
    save_people_overlay(rows)
    return rec


def set_person_status(name: str, company: str, status: str, when: str = "",
                      create: bool = False, method: str = "", detail: str = "") -> dict:
    status = LEGACY_PERSON_STAGE.get(status, status)
    if status not in PERSON_STAGES:
        raise ValueError(f"unknown person stage: {status!r} (use {', '.join(PERSON_STAGES)})")
    from . import history as hist

    rows = load_people_overlay()
    key = (name.lower().strip(), normalize_company(company))
    for r in rows:
        if (r["name"].lower().strip(), normalize_company(r.get("company", ""))) == key:
            was = r.get("status", "review")   # matches the render-time default
            r["status"] = status
            r["status_date"] = when or today()
            if method:
                # HOW the outreach happened — and the detail fills the channel
                # field it belongs to, so the address used is never lost.
                r["contact_method"] = method
                d = (detail or "").strip()
                if d:
                    if method == "email" and not r.get("email"):
                        r["email"] = d
                    elif method == "x" and not r.get("x"):
                        r["x"] = d
                    elif method == "phone" and not r.get("phone"):
                        r["phone"] = d
                    elif method in ("ig", "irl"):
                        tag = "IG" if method == "ig" else "met IRL"
                        r["notes"] = (r.get("notes", "") + f"\n{tag}: {d}").strip()
            save_people_overlay(rows)
            hist.record(person_key(r["name"], r.get("company", ""), r.get("linkedin", "")),
                        "status", f"moved to {status}",
                        f"from {was}" + (f" · via {method}" if method else ""), at=when)
            return r
    if create:
        # The dashboard triaging a feed-derived founder: the person is real
        # (they came from the feed), they just have no overlay row yet. CLI
        # callers keep the KeyError so a typo can't invent someone.
        rec = {"name": name.strip(), "company": company.strip(), "role": "",
               "linkedin": "", "relationship": "networking", "signals": [],
               "notes": "", "added": today(),
               "status": status, "status_date": when or today()}
        rows.append(rec)
        save_people_overlay(rows)
        hist.record(person_key(name, company), "status",
                    f"moved to {status}", "first triage", at=when)
        return rec
    raise KeyError(f"no person named {name!r} at {company!r} — add them first")


def add_person(
    name: str,
    company: str = "",
    role: str = "",
    linkedin: str = "",
    relationship: str = "networking",
    signal: str = "",
    note: str = "",
    email: str = "",
    x: str = "",
    phone: str = "",
    warm: bool | None = None,
    warm_via: str = "",
    notes_replace: str | None = None,
    location: str = "",
) -> dict:
    rows = load_people_overlay()
    key = (name.lower().strip(), normalize_company(company))
    for r in rows:
        if (r["name"].lower().strip(), normalize_company(r.get("company", ""))) == key:
            # update, don't duplicate
            if role:
                r["role"] = role
            if linkedin:
                r["linkedin"] = linkedin
            if email:
                r["email"] = email
            if x:
                r["x"] = x
            if phone:
                r["phone"] = phone
            if location:
                r["location"] = location
            if warm is not None:
                r["warm"] = bool(warm)
            if warm_via:
                # Eric-side knowledge ("met through X") — kept OFF notes so
                # signals_in never turns it into a claim about the person.
                r["warm_via"] = warm_via
            if signal and signal not in r.get("signals", []):
                r.setdefault("signals", []).append(signal)
            if note:
                r["notes"] = (r.get("notes", "") + "\n" + note).strip()
            if notes_replace is not None:
                # The edit form owns the whole field — no auto-generation,
                # what Eric typed is what stays.
                r["notes"] = notes_replace.strip()
            save_people_overlay(rows)
            return r
    rec = {
        "name": name.strip(),
        "company": company.strip(),
        "role": role,
        "linkedin": linkedin,
        "email": email,
        "x": x,
        "phone": phone,
        "location": location,
        "warm": bool(warm) if warm is not None else False,
        "warm_via": warm_via,
        "relationship": relationship,  # networking | founder | hiring-manager | friend
        "signals": [s for s in ([signal] if signal else [])],
        "notes": (notes_replace.strip() if notes_replace is not None else note),
        "added": today(),
    }
    rows.append(rec)
    save_people_overlay(rows)
    return rec


# Industry, derived from what the company says about itself. Deliberately
# coarse — a dozen buckets Eric filters by, not a taxonomy. The overlay's
# `industry` field always wins, so a wrong guess is a one-line fix.
_INDUSTRIES = [
    ("digital health",   ("health", "care", "clinic", "patient", "medic", "therap",
                          "mental", "biomark", "diagnos", "medicare", "medicaid",
                          "disease")),
    ("fitness & wellness", ("fitness", "sleep", "recovery", "wellness", "longevity",
                            "supplement", "nutrition", "protein", "workout",
                            "probiotic", "microbiome", "gut health")),
    ("bio & pharma",     ("biotech", "pharma", "drug", "molecul", "psychedelic",
                          "clinical trial", "biolog")),
    ("fintech",          ("payment", "banking", "wealth", "invest", "insur", "lend",
                          "fund admin", "financial", "accounting", "tax",
                          "finance", "treasury")),
    ("ed-tech",          ("learn", "education", "student", "tutor", "school")),
    ("food & ag",        ("food", "agricult", "farm", "restaurant", "grocery", "beverage")),
    ("sports",           ("sports", "athlete", "league", "betting", "fan")),
    ("climate & energy", ("climate", "energy", "carbon", "solar", "battery", "electric")),
    ("dev & data tools", ("developer", "api", "data infra", "database", "devtool",
                          "observab", "llm", "ai agent", "ml platform")),
    ("robotics & hardware", ("robot", "hardware", "sensor", "drone", "manufactur")),
    ("commerce & CPG",   ("commerce", "retail", "brand", "consumer", "marketplace", "cpg")),
    ("legal & compliance", ("legal", "compliance", "grc", "contract", "regulat")),
    ("music & creative", ("music", "artist", "creator", "entertainment", "podcast",
                          "record label")),
    # Last on purpose: the B2B blob. On a needle-count tie a specific vertical
    # above wins; this only catches what nothing else claims (2026-08-11).
    ("enterprise software", ("enterprise", "saas", "b2b", "workflow", "automation",
                             "crm", "erp", "hiring", "recruiting", "hr tech",
                             "security", "cyber", "govtech", "government",
                             "law enforcement", "defense", "internal tools",
                             "agentic")),
]


def industry_of(text: str) -> str:
    low = (text or "").lower()
    best, hits = "", 0
    for label, needles in _INDUSTRIES:
        n = sum(1 for k in needles if k in low)
        if n > hits:
            best, hits = label, n
    return best


# --- derived views ----------------------------------------------------------


@dataclass
class CompanyView:
    key: str
    name: str
    blurb: str = ""
    best_score: int | None = None
    postings: list[dict] = field(default_factory=list)
    founders: list[dict] = field(default_factory=list)
    funding: str = ""
    stage: str = ""
    headcount: str = ""
    locations: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    status: str = ""          # the most-advanced posting status
    tracked: bool = False
    why: str = ""
    notes: str = ""
    site: str = ""
    linkedin: str = ""
    industry: str = ""
    not_interested: bool = False
    saved_at: str = ""        # when Eric chose to save it — the strip's sort key
    added: str = ""           # when the overlay row was created (hand-tracked)
    edited: str = ""
    why_date: str = ""
    signals: list[str] = field(default_factory=list)
    # Warm paths found on the company's OWN pages, one per posting that carried
    # one. Company-sourced, so they're safe to mine for signals — unlike `why`.
    warm_paths: list[str] = field(default_factory=list)
    # Verified people inside the company who share Eric's background —
    # derived from the people overlay, evidence lives on the person record.
    alumni: list[str] = field(default_factory=list)


_STATUS_RANK = {"offer": 7, "interviewing": 6, "applied": 5, "saved": 4,
                "dormant": 3, "rejected": 2, "review": 1, "uninterested": 0}


# Job-board hosts — a posting URL on one of these says nothing about the
# company's own site.
# Hosts that are job boards / aggregators, never the company's own site.
# wellfound was missing from this list, so every company found via a Wellfound
# posting "derived" https://wellfound.com as its website — and the edit form
# then persisted it into the overlay on any unrelated save.
_AGG_HOSTS = ("ashbyhq", "greenhouse", "lever.co", "workatastartup", "generalist.world",
              "linkedin", "getro", "breezy", "workable", "personio", "recruitee",
              "ycombinator", "notion.site", "sec.gov", "wellfound", "builtin",
              "landing.jobs", "climatebase", "teamworkonline")

# Boilerplate lead-ins that eat the blurb's budget without saying anything.
# Anchored to the COMPANY'S OWN NAME, not a greedy word-run — a generic
# {2,40}-char pattern chewed into real sentences ("About Structured AI We're
# building the AI work..." lost everything through "work").


def _company_site(e: Entry) -> str:
    if e.company_url:
        return e.company_url.rstrip("/")
    from urllib.parse import urlparse

    host = urlparse(e.url or "").netloc.lower()
    if host and not any(a in host for a in _AGG_HOSTS):
        return f"https://{host}"
    return ""


# Sentences written to the CANDIDATE, not about the company. A company card's
# description slot showing "You'll operate as…" / "He's hiring a TPM…" is job
# copy leaking into the wrong slot (seen across dozens of substack-era cards).
_JOB_VOICE = re.compile(
    r"\byou[’']?(ll|d|re)\b|\byou (will|would|are)\b"
    r"|\bwe[’' ]?a?re? (looking|seeking|hiring)\b|\bis hiring\b"
    r"|\b(he|she|they)[’']s hiring\b|\bthis role\b|\bthe role\b|\bthe position\b"
    r"|\breports? to\b|\bresponsibilities\b|\bqualifications\b"
    r"|\byears? of exp\b|\bthe ideal candidate\b|\bapply (now|here|today|by)\b",
    re.I,
)
_IMPERATIVE_OPEN = re.compile(r"^(Own|Lead|Drive|Join|Run|Manage|Build|Take|Be|Help)\b")


def _blurb_from(desc: str, company: str = "", limit: int = 230) -> str:
    """First meaningful sentence-ish chunk of a description, boilerplate stripped.

    Returns whatever survives cleaning, even a fragment — callers gate on
    _blurb_ok and keep searching other postings when this is under 40 chars.
    Returns "" outright when the text is job copy with no company-voiced
    sentence: an honest blank beats a card that describes the vacancy.
    """
    import html as _html

    text = desc or ""
    # Substack listing lines carry their provenance suffix ("— via ai-operators
    # (Vol. 032, …)"). The whole line is a job listing, never company prose.
    if re.search(r"—\s*via\s+[\w.-]+\s*\(", text):
        return ""
    # Cached descriptions from before the ats.py order-fix carry literal tags;
    # strip defensively here so old cache entries can't leak markup into cards.
    text = _html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    # generalist's editorial "[Trust Evangelist]" prefixes are fun on the board
    # but read as noise repeated across 185 company cards.
    text = re.sub(r"^\s*\[[^\]]{2,40}\]\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Scrape artifact seen in the wild: "A bout Offstream Offstream is…" —
    # the split word dodges every boilerplate pattern below, so heal it first.
    text = re.sub(r"\bA bout\b", "About", text)
    # Generic section headers postings open with, company-agnostic.
    text = re.sub(
        r"^((about (us|the (company|team|role))|who we are|our mission|the company)[\s:,—-]*)+",
        "", text, flags=re.I,
    )
    if company:
        name = re.escape(company.strip())
        # "About X", "Why join X", "At X," — any stack of them, then punctuation.
        # The name may carry a suffix the feed omits ("Mecka" vs "Mecka AI") —
        # without allowing it, "About Mecka AI Mecka AI is…" strips to "AI Mecka…".
        boiler = re.compile(
            rf"^((about|why join|at)\s+(the\s+)?{name}"
            rf"(\s+(ai|labs|inc|hq|health|technologies))?[\s,:.!—-]*)+",
            re.I,
        )
        text = boiler.sub("", text)
    text = text.lstrip(" ,;:—-.")
    # Re-capitalise if the strip left us mid-sentence lowercase.
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    # Keep only company-voiced sentences; drop ones that address the reader.
    kept = [s for s in re.split(r"(?<=[.!?])\s+", text)
            if s and not _JOB_VOICE.search(s) and not _IMPERATIVE_OPEN.match(s)]
    if not kept:
        return ""
    text = " ".join(kept).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # break at the last sentence end, else last word
    for sep in (". ", "; ", ", "):
        i = cut.rfind(sep)
        if i > limit // 2:
            return cut[: i + 1].strip()
    return cut[: cut.rfind(" ")] + "…"


def _blurb_ok(b: str) -> bool:
    return len(b) >= 40


def companies(entries: list[Entry]) -> list[CompanyView]:
    from . import store

    descriptions = store.load()
    overlay = load_company_overlay()
    by: dict[str, CompanyView] = {}

    for e in entries:
        key = normalize_company(e.company)
        if not key:
            continue
        c = by.get(key)
        if c is None:
            c = by[key] = CompanyView(key=key, name=e.company)
        c.postings.append(
            {"title": e.title, "url": e.url, "score": e.score, "status": e.status,
             "first_seen": e.first_seen}
        )
        if e.score is not None and (c.best_score is None or e.score > c.best_score):
            c.best_score = e.score
        if e.founders and len(e.founders) > len(c.founders):
            c.founders = e.founders
        if e.funding and not c.funding:
            c.funding = e.funding
        if e.stage and not c.stage:
            c.stage = e.stage
        if e.headcount and not c.headcount:
            c.headcount = e.headcount
        if e.location and e.location not in c.locations:
            c.locations.append(e.location)
        if not c.site:
            c.site = _company_site(e)
        if not c.blurb or not _blurb_ok(c.blurb):
            from .models import normalize_url

            d = descriptions.get(normalize_url(e.url), "")
            if d:
                cand = _blurb_from(d, e.company)
                if _blurb_ok(cand) or len(cand) > len(c.blurb or ""):
                    c.blurb = cand
        if e.source and e.source not in c.sources:
            c.sources.append(e.source)
        if _STATUS_RANK.get(e.status, 0) > _STATUS_RANK.get(c.status, 0):
            c.status = e.status
        if e.warm_path and e.warm_path not in c.warm_paths:
            c.warm_paths.append(e.warm_path)

    # overlay-only companies (tracked by hand, no posting yet) + overlay fields
    for key, rec in overlay.items():
        c = by.get(key)
        if c is None:
            c = by[key] = CompanyView(key=key, name=rec.get("name", key))
        c.tracked = bool(rec.get("tracked"))
        c.why = rec.get("why", c.why)
        c.notes = rec.get("notes", "")
        if rec.get("site"):
            c.site = rec["site"]
        if rec.get("linkedin"):
            c.linkedin = rec["linkedin"]
        if rec.get("industry"):
            c.industry = rec["industry"]
        c.not_interested = bool(rec.get("not_interested"))
        c.saved_at = rec.get("saved_at", "") or rec.get("added", "")
        c.added = rec.get("added", "")
        c.edited = rec.get("edited", "")
        c.why_date = rec.get("why_date", "")
        if rec.get("stage"):
            c.stage = rec["stage"]
        # Overlay funding fills blanks only: boards publish live funding lines
        # (WaaS especially) and those stay authoritative over a researched one.
        if rec.get("funding") and not c.funding:
            c.funding = rec["funding"]
        # Same rule for headcount — a board-stated count beats a researched
        # range. Headcount is a stage SIGNAL, never a stage source: the stage
        # field stays sourced from named rounds only (Getro caveat, and Eric
        # 2026-08-13).
        if rec.get("headcount") and not c.headcount:
            c.headcount = rec["headcount"]
        if rec.get("location"):
            c.locations = [rec["location"]] + [x for x in c.locations if x != rec["location"]]
        if rec.get("description"):
            c.blurb = rec["description"]
        # Overlay founders (EDGAR officers since the play retirement,
        # 2026-08-12) — feed-derived founders win when they know more.
        if rec.get("founders") and len(rec["founders"]) > len(c.founders):
            c.founders = rec["founders"]

    # Derived industry fills the gaps AFTER overlay descriptions land, since
    # the classifier reads the blurb. Overlay-set industry always wins above.
    for c in by.values():
        if not c.industry:
            c.industry = industry_of(f"{c.blurb} {c.why}")

    # Signals come from what the COMPANY says about itself — its own
    # description, plus the warm path found on its own site. Deliberately NOT
    # `e.why`: that's the scorer's prose about ERIC, and mining it tagged
    # twelve companies with "NFL / football analytics" off fit lines reading
    # "outreach play matching Eric's Jets/sports proof point" (Eric,
    # 2026-08-07). Runs after the loop for the same reason industry does — the
    # blurb isn't final until overlay descriptions have landed.
    for c in by.values():
        for s in signals_in(" ".join([c.blurb or ""] + c.warm_paths)):
            if s not in c.signals:
                c.signals.append(s)

    # A company "has Emory inside" only when a VERIFIED person with that
    # signal is recorded there. Derived, so it can never drift from the
    # person-level evidence — delete the person and the flag disappears.
    for person in load_people_overlay():
        key = normalize_company(person.get("company", ""))
        c = by.get(key)
        if not c:
            continue
        for sig in person.get("signals", []):
            tag = f"{sig}: {person['name'].split(',')[0]}"
            if tag not in c.alumni:
                c.alumni.append(tag)

    return sorted(
        by.values(),
        key=lambda c: (
            -_STATUS_RANK.get(c.status, 0),
            -(c.best_score if c.best_score is not None else -1),
        ),
    )


@dataclass
class PersonView:
    name: str
    company: str
    role: str = ""
    linkedin: str = ""
    linkedin_confidence: str = ""
    relationship: str = ""     # founder | networking | hiring-manager | ...
    signals: list[str] = field(default_factory=list)
    # label -> the words that earned it. A signal matched inside a cached
    # profile comes from text Eric never sees on the card, so the chip has to
    # be able to show its own evidence.
    evidence: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    best_score: int | None = None
    company_status: str = ""
    status: str = "review"         # PERSON_STAGES
    status_date: str = ""
    email: str = ""
    x: str = ""
    phone: str = ""
    contact_method: str = ""
    following: bool = False
    added: str = ""
    location: str = ""
    src: str = "manual"       # "feed" when the person arrived via a posting
    warm: bool = False
    warm_via: str = ""


def people(entries: list[Entry]) -> list[PersonView]:
    company_score: dict[str, int] = {}
    company_status: dict[str, str] = {}
    for e in entries:
        key = normalize_company(e.company)
        if e.score is not None and e.score > company_score.get(key, -1):
            company_score[key] = e.score
        if _STATUS_RANK.get(e.status, 0) > _STATUS_RANK.get(company_status.get(key, ""), 0):
            company_status[key] = e.status

    seen: dict[tuple[str, str], PersonView] = {}

    for e in entries:
        key = normalize_company(e.company)
        for f in e.founders or []:
            name = (f.get("name") or "").strip()
            if not name:
                continue
            pk = (name.lower(), key)
            p = seen.get(pk)
            if p is None:
                p = seen[pk] = PersonView(
                    name=name,
                    company=e.company,
                    role=f.get("title", ""),
                    linkedin=f.get("linkedin", ""),
                    linkedin_confidence=f.get("linkedin_confidence", ""),
                    relationship="founder",
                    best_score=company_score.get(key),
                    company_status=company_status.get(key, ""),
                    src="feed",
                    added=e.date_added or e.first_seen or "",
                )
            fed = e.date_added or e.first_seen or ""
            if fed and (not p.added or fed < p.added):
                p.added = fed   # earliest sighting is when they were fed in
            if f.get("linkedin") and not p.linkedin:
                p.linkedin = f["linkedin"]
            # Person signals come ONLY from the person's own recorded text
            # (their title/bio). e.warm_path is Eric-side context about the
            # COMPANY — attributing it to the person would claim "same school"
            # about someone whose bio never said so, and that claim could end
            # up in an email. Company-level signals live on CompanyView.
            for s, why in signals_with_evidence(f.get("title", "")):
                if s not in p.signals:
                    p.signals.append(s)
                p.evidence.setdefault(s, why)

    for r in load_people_overlay():
        pk = (r["name"].lower().strip(), normalize_company(r.get("company", "")))
        p = seen.get(pk)
        if p is None:
            p = seen[pk] = PersonView(
                name=r["name"],
                company=r.get("company", ""),
                relationship=r.get("relationship", "networking"),
                best_score=company_score.get(pk[1]),
                company_status=company_status.get(pk[1], ""),
            )
        p.role = r.get("role", p.role)
        p.linkedin = r.get("linkedin", p.linkedin)
        p.notes = r.get("notes", "")
        # Auto-added people land in REVIEW (Eric, 2026-08-18). Sweeps,
        # imports and warm matches all record people the same way — no field
        # anywhere separates a form-add from a sweep-add — so "recorded means
        # chosen" was never true, and strangers were polluting the actionable
        # pile. set_person_status is the only writer of r["status"] and its
        # only caller is the UI dropdown, so a PRESENT status means Eric moved
        # that person by hand. Hence: render-time fallback only, never a
        # migration — writing "review" onto the 71 status-less records would
        # erase the very boundary this relies on.
        raw_st = r.get("status") or "review"
        p.status = LEGACY_PERSON_STAGE.get(raw_st, raw_st)
        p.status_date = r.get("status_date", "")
        p.email = r.get("email", "")
        p.x = r.get("x", "")
        p.phone = r.get("phone", "")
        p.contact_method = r.get("contact_method", "")
        p.following = bool(r.get("following"))
        if not p.added:                    # feed date wins; overlay fills gaps
            p.added = r.get("added", "")
        p.location = r.get("location", "")
        p.warm = bool(r.get("warm"))
        p.warm_via = r.get("warm_via", "")
        # Each field on its own: concatenating them let a multi-word needle
        # match across the seam once whitespace became flexible — notes ending
        # "…relocated to New York" plus role "Jets Fan Engagement" would have
        # read as the NY Jets.
        #
        # The captured profile is the third and by far the largest surface: a
        # headline says "Product Manager at Stripe" while the About section and
        # the job below it say four years at the Brooklyn Nets. Cached in
        # data/.cache/profiles.json rather than in notes, which stays Eric's.
        # Strongest provenance first, so the evidence a chip shows is the best
        # one available rather than whichever matched first. Riley Okafor's Sports
        # came from a 2019 student internship while the fact that matters —
        # she works at a sports agency NOW — ranked no higher (Eric,
        # 2026-08-07). Current role beats past role beats education.
        found: list[tuple[str, str]] = []
        found += [(l, f"current role — {w}") for l, w in
                  signals_with_evidence(r.get("role", ""))]
        found += [(l, f"employer {r.get('company','')} — {w}") for l, w in
                  _employer_signals(r.get("company", ""))]
        for sec, body in _profile_sections(r.get("linkedin", "")):
            tag = "" if sec == "profile" else f"{sec} — "
            found += [(l, tag + w) for l, w in signals_with_evidence(body)]
        found += [(l, f"note — {w}") for l, w in
                  signals_with_evidence(r.get("notes", ""))]
        for s in r.get("signals") or []:
            if s not in p.signals:
                p.signals.append(s)
        for s, why in found:
            if s not in p.signals:
                p.signals.append(s)
            p.evidence.setdefault(s, why)

    return sorted(
        seen.values(),
        key=lambda p: (-len(p.signals), -(p.best_score if p.best_score is not None else -1)),
    )
