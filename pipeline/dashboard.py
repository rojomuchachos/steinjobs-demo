"""The page `make app` serves — four tabs over the feed.

`make status` is the right tool for "what's live right now", but it tops out at
a dozen rows. With 1,800+ entries the actual job is browsing and triage: see who
to write to, filter to a city, read the reasoning, open the posting. That's a
screen, not a terminal.

Built fresh by app.py on every page load, so it can never show a stale pipeline.
No framework, no build step, no network: one HTML string with the feed inlined
as JSON. Nothing is uploaded — this runs on 127.0.0.1 and talks only to itself.

Send opens first because it is the point of the app. Postings render 60 at a
time with a show-more button — rendering all 1,884 put 41k nodes and a
240,000px scroll on the page. Rows expand on click (and on Enter/Space) into
description, company, founder leads, provenance, the "why this score" panel
with its feedback loop into calibration.

Type, spacing and radius come from the tokens in :root. Add values there rather
than inline, or the scale stops being a scale — tests pin this.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from .models import Entry, normalize_url
from .prefilter import required_years as _required_years

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "dashboard.html"

STATUS_ORDER = ["offer", "interviewing", "applied", "saved", "review",
                "dormant", "rejected", "uninterested", "expired", "incomplete"]

# Two weeks of silence after an active move means the thread has gone quiet.
# Dormant is DERIVED at read time, never written — undo stays trivial and a
# reply arriving on day 15 costs nothing to act on.
DORMANT_AFTER_DAYS = 14
_AUTO_DORMANT_POSTING = ("applied", "interviewing")
_AUTO_DORMANT_PERSON = ("contacted", "conversation")


def _days_since(iso: str) -> int | None:
    try:
        return (date.today() - date.fromisoformat(iso[:10])).days
    except (ValueError, TypeError):
        return None


def effective_status(status: str, since_iso: str, auto: tuple) -> str:
    d = _days_since(since_iso)
    if status in auto and d is not None and d >= DORMANT_AFTER_DAYS:
        return "dormant"
    return status

# City buckets for the dropdown. Order here IS the dropdown order — New York
# first by request, then the rest of the US, then the European cities Eric
# named, then catch-alls. (label, us?, [match keywords])
CITY_BUCKETS: list[tuple[str, bool, list[str]]] = [
    ("New York", True, ["new york", "nyc", "brooklyn", "manhattan"]),
    ("SF Bay Area", True, ["san francisco", "sf,", " sf ", "bay area", "palo alto",
                           "mountain view", "menlo park", "oakland", "berkeley", "south san"]),
    ("Remote", True, ["remote", "anywhere", "distributed"]),
    ("Boston", True, ["boston", "cambridge, ma"]),
    ("Los Angeles", True, ["los angeles", "santa monica", "culver city", " la,"]),
    ("Seattle", True, ["seattle", "bellevue", "redmond"]),
    # Eric consolidated the per-city EU buckets: one International bucket. The
    # locations still carry the raw city; this is just the filter granularity.
    ("International", False, ["london", "dublin", "amsterdam", "netherlands",
                              "prague", "czech", "berlin", "munich", "münchen",
                              "unterföhring", "paris", "milan", "rome", "italy",
                              "torino", "turin", "copenhagen", "denmark",
                              "stockholm", "sweden"]),
]
_US_HINTS = re.compile(
    r"\b(usa|united states|austin|chicago|denver|seattle|miami|atlanta|dallas"
    r"|washington|philadelphia|nashville|phoenix|san diego|salt lake|portland"
    r"|,\s?(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi"
    r"|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt"
    r"|va|wa|wv|wi|wy)\b)", re.I)


def city_bucket(location: str, remote: bool = False) -> tuple[str, bool]:
    """(bucket label, is_us). Unknowns land in Other US / Other Europe / Other."""
    loc = (location or "").lower()
    for label, us, keys in CITY_BUCKETS:
        if any(k in loc for k in keys):
            return label, us
    if remote and not loc:
        return "Remote", True
    if not loc:
        return "", True   # blank = unknown location, not "Other" (Eric, 2026-08-12)
    if _US_HINTS.search(loc):
        return "Other US", True
    if re.search(r"\b(uk|germany|france|spain|portugal|ireland|austria|belgium"
                 r"|switzerland|poland|norway|finland|estonia|lisbon|madrid"
                 r"|barcelona|vienna|zurich|zürich|geneva|warsaw|oslo|helsinki"
                 r"|tallinn|luxembourg|europe|emea)\b", loc):
        return "International", False
    return "Other", True


# Job-type buckets, matched against the title in this order — function beats
# stage, so "Founding Growth Lead" files under Growth and plain "Founding Team
# Member" lands in Founding & Generalist. First match wins, which is why
# Technical GTM sits above CoS & Strategy ("Deployment Strategist" is GTM, not
# strategy) and why the strategist pattern guards against the marketing kind
# ("Brand/Content Strategist" belongs to Marketing). A label may appear twice:
# "research" files under Data only AFTER Engineering has had its chance, so
# "Research Engineer" is Engineering while "AI Researcher" stays Data
# (2026-08-11 — the old single Data entry swallowed every research title).
ROLE_TYPES: list[tuple[str, str]] = [
    ("Technical GTM", r"gtm engineer|growth engineer|forward[ -]deploy|"
                      r"deployment strategist|solutions? engineer|sales engineer|"
                      r"solutions architect"),
    ("CoS & Strategy", r"chief of staff|special projects|strategic projects?|"
                       r"\bstrategy\b|(?<!content )(?<!brand )(?<!marketing )"
                       r"(?<!growth )(?<!sales )strategist"),
    ("Growth", r"growth"),
    ("Marketing", r"marketing|\bbrand\b|content|community|social media|communications"),
    ("Sales & BD", r"sales|business development|\bbd\b|partner|account exec|"
                   r"revenue|go[ -]to[ -]market|\bgtm\b|customer success|"
                   r"account manager|commercial"),
    ("People & Talent", r"recruit|talent|people ops|people operations|\bhr\b|"
                        r"\bpeople\b|sourcer"),
    ("Finance & Legal", r"finance|accounting|legal|counsel|controller|treasury"),
    ("Product & Design", r"\bproduct\b|\bpm\b|design"),
    ("Operations", r"operations|\bops\b|operating|office manager|"
                   r"project manager|program manager|supply chain|logistics|"
                   r"procurement|production manager|onboarding"),
    ("Data", r"\bdata\b|analytics|analyst|machine learning|\bml\b"),
    ("Engineering", r"engineer|developer|software|\bswe\b|devops|infrastructure|"
                    r"full[ -]stack|backend|frontend|\bqa\b|security"),
    ("Data", r"research"),
    ("Founding & Generalist", r"founding|founder|generalist|entrepreneur|"
                              r"early employee|first hire|venture"),
]
_ROLE_RES = [(label, re.compile(pat)) for label, pat in ROLE_TYPES]
# dict.fromkeys: a split label (Data appears twice) still lists once in the UI.
ROLE_ORDER = list(dict.fromkeys(label for label, _ in ROLE_TYPES)) + ["Other"]


def role_bucket(title: str) -> str:
    t = (title or "").lower()
    for label, rx in _ROLE_RES:
        if rx.search(t):
            return label
    return "Other"


# A review entry this stale and this unloved is a filled role, not a lead.
# Derived at build time (never written), so a later rescore that clears the
# bar un-sweeps it automatically. Escape-hatch entries are exempt.
SWEEP_AFTER_DAYS = 30
SWEEP_KEEP_SCORE = 65


# The score floor (Eric, 2026-08-18): a posting the scorer judged 50 or
# below goes straight to uninterested — the pile he triages should only
# hold what cleared the bar. Escape-hatch roles are exempt, and the note
# marks it as policy so insights/calibration never read it as taste.
SCORE_FLOOR = 50


def _swept(e: Entry) -> str:
    """Sweep reason for the WRITE pass (scout applies it), '' to keep."""
    if e.status != "review" or e.escape_hatch:
        return ""
    if e.score is not None and e.score <= SCORE_FLOOR:
        return f"score floor — scored {e.score}, auto-screened (policy, not a hand verdict)"
    if (e.score or 0) >= SWEEP_KEEP_SCORE:
        return ""
    age = _days_since(e.first_seen or e.date_added)
    if age is not None and age >= SWEEP_AFTER_DAYS:
        return "swept — stale review, never touched"
    return ""


def promote_complete(entries: list[Entry]) -> int:
    """`incomplete` -> `review` once a posting has both text and a score.

    The intake gate (Eric, 2026-08-18) holds text-less postings out of the
    pile. This is the other half: the moment the link sweep, the mirror pass
    or a duplicate supplies text AND the scorer judges it, the posting joins
    the pile on its own. Runs after every scoring pass — never on the page
    build, which must not write the ledger.
    """
    n = 0
    for e in entries:
        if e.status == "incomplete" and e.score is not None:
            e.status = "review"
            e.status_note = ""
            n += 1
    return n


def apply_sweep(entries: list[Entry]) -> int:
    """Move sweep candidates to uninterested with the auto note. Run by scout,
    never by build — the page must not write the ledger. Auto-swept entries
    are NOT calibration examples; only hand-marked passes teach the scorer."""
    n = 0
    for e in entries:
        note = _swept(e)
        if note:
            e.status = "uninterested"
            e.status_note = note
            n += 1
    return n


def _applied_on(events: list[dict]) -> str:
    for ev in reversed(events):
        if ev["k"] == "status" and "applied" in ev["t"]:
            return ev["at"]
    return ""


def _desc_snippet(url: str, descriptions: dict, limit: int = 600) -> str:
    from .models import normalize_url

    d = descriptions.get(normalize_url(url), "")
    if not d:
        return ""
    import html as _html

    d = re.sub(r"<[^>]+>", " ", _html.unescape(d))
    d = re.sub(r"\s+", " ", d).strip()
    if len(d) <= limit:
        return d
    cut = d[:limit]
    i = cut.rfind(". ")
    return (cut[: i + 1] if i > limit // 2 else cut[: cut.rfind(" ")] + "…").strip()


_SALARY = re.compile(
    r"\$\s?(\d{2,3})[,.]?(\d{3})?\s?[kK]?\s*[-–—]\s*\$?\s?(\d{2,3})[,.]?(\d{3})?\s?[kK]?")


def _salary_from(desc: str) -> str:
    """"$90K – $130K"-style range pulled from a posting's own text."""
    m = _SALARY.search(desc or "")
    if not m:
        return ""
    def amt(a, b):
        n = int(a) * (1000 if not b else 1) + (int(b) if b else 0)
        if b:
            n = int(a + b)
        return n if n > 1000 else n * 1000
    lo, hi = amt(m.group(1), m.group(2)), amt(m.group(3), m.group(4))
    if not (20_000 <= lo <= 900_000 and lo < hi <= 900_000):
        return ""
    return f"${lo//1000}K–${hi//1000}K"


def _rows(entries: list[Entry]) -> list[dict]:
    from . import entities, store
    from . import history as hist
    from .brief import APPLICATIONS, slug

    h = hist.load()
    descriptions = store.load()

    out = []
    for e in entries:
        founders = [
            {"n": f.get("name", ""), "t": f.get("title", ""), "l": f.get("linkedin", "")}
            for f in (e.founders or [])
        ]
        d = APPLICATIONS / slug(e.company)
        arts = [lbl for name, lbl in (("outreach.md", "outreach"),
                                      ("brief.md", "brief"),
                                      ) if (d / name).exists()]
        events = [
            {"at": ev.date, "k": ev.kind, "t": ev.text, "d": ev.detail}
            for ev in h.for_entry(e.url)
        ]
        city, us = city_bucket(e.location)
        out.append(
            {
                "t": e.title,
                "c": e.company,
                "u": e.url,
                "s": e.score,
                "w": e.why,
                "st": effective_status(e.status, e.last_touched, _AUTO_DORMANT_POSTING),
                "sn": e.status_note,
                "days": _days_since(e.last_touched),
                "loc": e.location,
                "city": city,
                "us": us,
                "cat": e.role_type or role_bucket(e.title),
                "sw": (e.status_note or "").startswith("swept"),
                "ivs": e.interview_stage,
                "stg": e.stage,
                "src": e.source,
                "aso": e.also_seen_on or [],
                "fund": e.funding,
                "touch": e.last_touched,
                "p": e.posted_at or "",
                "fs": e.first_seen or "",
                "ap": _applied_on(events),
                "eh": bool(e.escape_hatch),
                "fs_all": founders,
                "warm": e.warm_path,
                "visa": e.sponsors_visa,
                "sal": (f"${e.comp_min//1000}K–${e.comp_max//1000}K"
                        if e.comp_min and e.comp_max
                        else _salary_from(descriptions.get(normalize_url(e.url), ""))),
                # Board-stated first (WaaS ships it), else read from the cached
                # description; null when neither knows — the chip stays absent
                # rather than guessing (2026-08-11).
                "yoe": (e.min_experience if e.min_experience is not None
                        else _required_years(descriptions.get(normalize_url(e.url), ""))),
                "arts": arts,
                "hist": events,
                "slug": slug(e.company),
            }
        )
    return out


# Why a source is in the feed without being a board in the registry.
_DERIVED_SOURCES = {
    "shortlist": "hand-built import",
    "watchlist": "watchlist careers pages",
    "edgar": "SEC Form D sweep",
    "manual": "pasted by hand",
    "substack": "newsletter extraction",
}


def _conn_age_days() -> int | None:
    """Days since the LinkedIn export was refreshed; None when it's absent."""
    from .intros import CONNECTIONS_PATH

    try:
        mtime = date.fromtimestamp(CONNECTIONS_PATH.stat().st_mtime)
    except OSError:
        return None
    return (date.today() - mtime).days


def _n_connections() -> int:
    from .intros import load_connections

    try:
        return len(load_connections())
    except Exception:  # noqa: BLE001 — a malformed export shouldn't kill the build
        return 0


def _diagnostics(entries: list[Entry]) -> dict:
    """One honest panel: which sources feed the machine, what each produced,
    when it last produced it, and whether the loops around it are running."""
    import yaml as _yaml

    from . import store
    from .describe import SUMMARY_QUEUE
    from .enrich import QUEUE as ENRICH_QUEUE
    from .schedule import PLIST

    per: dict[str, dict] = {}
    for e in entries:
        d = per.setdefault(e.source or "?", {"entries": 0, "scored": 0, "hot": 0, "last": "", "live": 0})
        d["entries"] += 1
        if e.score is not None:
            d["scored"] += 1
        if (e.score or 0) >= 70:
            d["hot"] += 1
        fs = e.first_seen or e.date_added or ""
        if fs > d["last"]:
            d["last"] = fs
        if e.status not in ("review", "uninterested", "incomplete"):
            d["live"] += 1
    empty = {"entries": 0, "scored": 0, "hot": 0, "last": "", "live": 0}
    boards_path = ROOT / "data" / "boards.yaml"
    cfg = _yaml.safe_load(boards_path.read_text(encoding="utf-8"))["boards"] if boards_path.exists() else {}
    rows = []
    for name, bc in cfg.items():
        rows.append({"name": name, "tier": bc.get("tier", "api"),
                     "reason": bc.get("reason", ""), **(per.get(name) or empty)})
    for name in sorted(set(per) - set(cfg)):
        rows.append({"name": name, "tier": "derived",
                     "reason": _DERIVED_SOURCES.get(name, ""), **per[name]})
    rows.sort(key=lambda r: -r["entries"])

    def _sitting_hours() -> list[int]:
        # The scheduler's own store isn't on disk anywhere readable, so the
        # repo keeps a mirror — data/sitting_schedule.json — updated whenever
        # the task's schedule changes (noted in the sweep protocol).
        try:
            return sorted(json.loads(
                (ROOT / "data" / "sitting_schedule.json").read_text())["hours"])
        except (OSError, ValueError, KeyError):
            return []

    def _tail_log(path: Path) -> list[dict]:
        try:
            return json.loads(path.read_text())[-15:][::-1]
        except (OSError, ValueError):
            return []

    def _read_json_list(path) -> list:
        import json as _json
        try:
            v = _json.loads(path.read_text())
            return v if isinstance(v, list) else []
        except (OSError, ValueError):
            return []

    def _recent_swept() -> list[dict]:
        from .entities import (ALUMNI_PASSES, load_company_overlay,
                               load_people_overlay)
        from .models import normalize_company as _nc
        finds: dict[str, int] = {}
        for pr in load_people_overlay():
            if any(x in ("Emory", "NMH") for x in pr.get("signals", [])):
                k = _nc(pr.get("company", ""))
                finds[k] = finds.get(k, 0) + 1
        out = []
        for rec in load_company_overlay().values():
            ck = rec.get("alumni_checked", {})
            if set(ALUMNI_PASSES) <= set(ck):
                out.append({"c": rec.get("name", "?"), "at": max(ck.values()),
                            "n": finds.get(_nc(rec.get("name", "")), 0)})
        out.sort(key=lambda x: x["at"], reverse=True)
        return out[:16]

    def _qnames(path: Path, key: str) -> list[str]:
        # queue files differ in shape: lists of dicts, lists of strings, or a
        # wrapper dict with a "companies" list (enrichment_queue) — its top
        # keys once rendered as companies named "Instructions" and "Count".
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        if isinstance(raw, dict):
            raw = raw.get("companies") or raw.get("items") or []
        out: list[str] = []
        for x in raw:
            v = (x.get(key) or x.get("linkedin") or "?") if isinstance(x, dict) else str(x)
            if v not in out:
                out.append(v)
        return out[:20]

    def _qlen(path: Path) -> int:
        try:
            v = json.loads(path.read_text())
            return len(v) if isinstance(v, (list, dict)) else 0
        except (OSError, ValueError):
            return 0

    descs = store.load()
    cal = ROOT / "data" / "calibration.json"
    logs = sorted((ROOT / "data" / "logs").glob("scout-*.log")) if (ROOT / "data" / "logs").exists() else []
    return {
        "boards": rows,
        "totals": {
            "entries": len(entries),
            "unscored": sum(1 for e in entries if e.score is None),
            # what's actually WAITING: the review pile. Uninterested/expired
            # rows are unscored forever by design and must not inflate the nag
            "unscored_review": sum(1 for e in entries
                                   if e.score is None and (e.status or "review") == "review"),
            "hot": sum(1 for e in entries if (e.score or 0) >= 70),
            "descs": len(descs),
            "enrich_q": _qlen(ENRICH_QUEUE),
            "summarize_q": _qlen(SUMMARY_QUEUE),
            "calibration": _qlen(cal),
            "connections": _n_connections(),
        },
        "scheduled": PLIST.exists(),
        "conn_days": _conn_age_days(),
        "alumni_q": _qlen(ROOT / "data" / "alumni_queue.json"),
        "person_fill_q": _qlen(ROOT / "data" / "person_fill_queue.json"),
        "alumni_queue": _qnames(ROOT / "data" / "alumni_queue.json", "company"),
        "fill_queue": _qnames(ROOT / "data" / "person_fill_queue.json", "name"),
        "founder_queue": _qnames(ROOT / "data" / "enrichment_queue.json", "company"),
        "sitting_log": _tail_log(ROOT / "data" / "sitting_log.json"),
        "sitting_hours": _sitting_hours(),
        "recent_swept": _recent_swept(),
        "autofill_log": _read_json_list(ROOT / "data" / "autofill_log.json"),
        "board_sweep_queue": _read_json_list(ROOT / "data" / "board_sweep_queue.json"),
        "last_log": logs[-1].name if logs else "",
    }


def build(entries: list[Entry], out_path: Path = OUT) -> Path:
    rows = _rows(entries)
    scored = [r for r in rows if r["s"] is not None]
    counts = Counter(r["st"] for r in rows)

    stats = {
        "total": len(rows),
        "scored": len(scored),
        "live": sum(counts[s] for s in ("saved", "applied", "interviewing", "offer")),
        "unscored": sum(1 for r in rows if r["s"] is None),
        "sources": dict(Counter(r["src"] for r in rows).most_common()),
        "statuses": {s: counts.get(s, 0) for s in STATUS_ORDER if counts.get(s)},
    }

    from . import entities, timing

    views = entities.companies(entries)
    # The Send tab is gone, but its ranking survives: fit + timing + warm path
    # orders the Tracker's saved group, so "what's next" stays intelligent.
    sendpri = {a.url: a.priority for a in timing.queue(entries, limit=100, views=views)}

    # First-degree LinkedIn connections, matched locally against the export at
    # data/connections.csv (gitignored, never leaves the machine). Plain string
    # match by normalized company — same matcher `make intros` uses. If the
    # file isn't there, every card simply has no 1st° chip.
    from .intros import _company_key as _conn_key
    from .intros import load_connections

    conn_by_co: dict[str, list[str]] = {}
    for cn in load_connections():
        if cn.company and cn.name:
            conn_by_co.setdefault(_conn_key(cn.company), []).append(cn.name)

    from .entities import load_company_overlay as _lco

    _ov = _lco()
    from .entities import ALUMNI_PASSES
    from .entities import ALUMNI_QUEUE as _AQ
    from .models import normalize_company as _nco
    try:
        _queued = {_nco(x.get("company", "")) for x in json.loads(_AQ.read_text())}
    except (OSError, ValueError):
        _queued = set()
    fresh_line = (date.today() - timedelta(days=7)).isoformat()
    comps = []
    for c in views:
        new_posts = [
            p for p in c.postings
            if p["status"] == "review" and (p.get("first_seen") or "") >= fresh_line
        ]
        locs = c.locations[:3]
        cities = sorted({city_bucket(loc)[0] for loc in c.locations if loc})
        conns = conn_by_co.get(_conn_key(c.name), [])[:3]
        _rec = _ov.get(c.key, {})
        _checked = _rec.get("alumni_checked", {})
        sweep = ({"state": "blocked", "why": _rec.get("sweep_blocked", "")}
                 if _rec.get("sweep_blocked")
                 else {"state": "done", "when": max(_checked.values(), default="")}
                 if set(ALUMNI_PASSES) <= set(_checked)
                 else {"state": "queued"} if c.key in _queued
                 else {"state": "none"})
        comps.append(
            {
                "key": c.key, "name": c.name, "score": c.best_score, "status": c.status,
            "alumni": c.alumni, "linkedin": c.linkedin, "conn": conns, "sweep": sweep,
            "industry": c.industry, "ni": c.not_interested,
            "headcount": c.headcount,
                "postings": c.postings, "founders": c.founders, "funding": c.funding,
                "stage": c.stage, "locs": locs, "cities": cities, "sources": c.sources,
                "raised": _rec.get("last_raised"),
                "tracked": c.tracked, "saved_at": c.saved_at,
                "edited": c.edited, "why_date": c.why_date,
                "added": c.added or min((p.get("first_seen") or "9999" for p in c.postings),
                                        default="")[:10].replace("9999", ""),
                "via": ("added by hand" if c.added and not c.postings
                        else (c.sources[0] if c.sources else "watchlist")),
                "why": c.why, "notes": c.notes, "site": c.site,
                "blurb": c.blurb, "signals": c.signals,
                "descfull": (_ov.get(c.key, {}).get("description_full")
                             or _ov.get(c.key, {}).get("description") or "").strip(),
                "new_posts": len(new_posts),
                "best_url": max(
                    (p for p in c.postings if p["score"] is not None),
                    key=lambda p: p["score"], default={"url": ""},
                )["url"],
            }
        )
    from . import history as _hist
    from .entities import person_key as _pkey
    _hev = _hist.load().events
    def _nudges(pv):
        evs = [e for e in (_hev.get(_pkey(pv.name, pv.company)) or [])
               if e.get("kind") == "nudge"]
        return (len(evs), max((e.get("at", "") for e in evs), default=""))
    ppl = [
        {
            "name": pv.name, "company": pv.company, "role": pv.role,
            "linkedin": pv.linkedin, "conf": pv.linkedin_confidence,
            "rel": pv.relationship, "signals": pv.signals, "ev": pv.evidence,
            "notes": pv.notes,
            "score": pv.best_score, "cstatus": pv.company_status,
            "st": effective_status(pv.status, pv.status_date, _AUTO_DORMANT_PERSON),
            "std": pv.status_date, "days": _days_since(pv.status_date),
            "email": pv.email, "x": pv.x, "phone": pv.phone, "cm": pv.contact_method,
            "fol": pv.following,
            "added": pv.added, "src": pv.src, "loc": pv.location,
            "warm": pv.warm, "wvia": pv.warm_via,
            "nudges": _nudges(pv)[0], "nudged": _nudges(pv)[1][:10],
        }
        for pv in entities.people(entries)
    ]
    # The scoreboard: lifetime counts from history (a thread that later moved
    # on still counts as an application made), current people stages on top.
    from . import history as hist_mod

    h = hist_mod.load()
    applied_urls, responded_urls, offer_urls, rejected_urls = set(), set(), set(), set()
    for url, evs in h.events.items():
        for ev in evs:
            t = ev.get("text", "")
            if t == "moved to applied":
                applied_urls.add(url)
            elif t in ("moved to responded", "moved to interviewing"):
                responded_urls.add(url)
            elif t == "moved to offer":
                offer_urls.add(url)
            elif t == "moved to rejected":
                rejected_urls.add(url)
    # Daily goals: >=3 applications and >=3 outreach touches per day; the
    # streak counts consecutive goal-days, skipping weekends, and the weekly
    # counter tracks Eric's 5-days-a-week target.
    from collections import Counter as _Counter
    from datetime import timedelta as _td

    applied_by_day, outreach_by_day = _Counter(), _Counter()
    for url, evs in h.events.items():
        for ev in evs:
            day = ev.get("at", "")[:10]
            t = ev.get("text", "")
            if t == "moved to applied":
                applied_by_day[day] += 1
            elif t in ("moved to reached out", "moved to contacted"):
                outreach_by_day[day] += 1
    GOAL_APPLY, GOAL_REACH = 3, 3
    def _hit(day: str) -> bool:
        return applied_by_day[day] >= GOAL_APPLY and outreach_by_day[day] >= GOAL_REACH
    today_iso = date.today().isoformat()
    week_days = [(date.today() - _td(days=i)).isoformat() for i in range(7)]
    streak, d = 0, date.today()
    while True:
        if d.weekday() >= 5:                # weekends never break the streak
            d -= _td(days=1)
            continue
        if _hit(d.isoformat()):
            streak += 1
            d -= _td(days=1)
        else:
            # today isn't over — an unmet goal today shouldn't zero the streak
            if d == date.today():
                d -= _td(days=1)
                continue
            break

    ppl_all = ppl
    # Lifetime counts come from history, not current stage — a person who was
    # contacted and later reset (or went dormant, or answered and moved on)
    # still counts as ever-contacted. Same rule applied_urls already follows.
    ever_contact, ever_reply, ever_met = set(), set(), set()
    # Keys are linkedin URLs when known, "person:name|co" otherwise — so match
    # on the event text (these stage names are people-only), not the key shape.
    for key, evs in h.events.items():
        for ev in evs:
            t = ev.get("text", "")
            if t in ("moved to contacted", "moved to reached out"):
                ever_contact.add(key)
            elif t in ("moved to conversation", "moved to replied"):
                ever_reply.add(key)
            elif t in ("moved to met", "moved to meeting"):
                ever_met.add(key)
    contacted = [q for q in ppl_all
                 if q["st"] in ("contacted", "conversation", "met", "dormant")]
    game = {
        "applied": len(applied_urls) or sum(1 for r in rows if r["st"] in ("applied", "interviewing", "offer")),
        "responded": len(responded_urls) or sum(1 for r in rows if r["st"] in ("interviewing", "offer")),
        "offers": len(offer_urls) or sum(1 for r in rows if r["st"] == "offer"),
        "contacted": max(len(ever_contact), len(contacted)),
        "replied": max(len(ever_reply), sum(1 for q in ppl_all if q["st"] in ("conversation", "met"))),
        "meetings": max(len(ever_met), sum(1 for q in ppl_all if q["st"] == "met")),
        "saved": sum(1 for r in rows if r["st"] == "saved"),
        "rejected": max(len(rejected_urls), sum(1 for r in rows if r["st"] == "rejected")),
        "ppl_saved": sum(1 for q in ppl_all if q["st"] not in ("review", "uninterested")),
        "following": sum(1 for c in comps if c["tracked"]),
        "alums": sum(1 for q in ppl_all if any(x in ("Emory", "NMH") for x in q["signals"])),
        "today_applied": applied_by_day[today_iso],
        "today_reached": outreach_by_day[today_iso],
        "goal_apply": GOAL_APPLY, "goal_reach": GOAL_REACH,
        "week_hits": sum(1 for dd in week_days if _hit(dd)),
        "streak": streak,
    }

    # Activity: every recorded event, newest first, labeled with the thing
    # it happened to — the app's own changelog.
    from .models import normalize_url as _nu

    by_url = {_nu(e.url): e for e in entries if e.url}
    ppl_by_key = {}
    for r in entities.load_people_overlay():
        from .entities import person_key as _pk
        ppl_by_key[_pk(r["name"], r.get("company", ""), r.get("linkedin", ""))] =             f'{r["name"]}' + (f' ({r.get("company")})' if r.get("company") else "")
    activity = []
    for key, evs in h.events.items():
        e = by_url.get(key)
        if e:
            subj = f"{e.company} — {e.title}"
        elif key in ppl_by_key:
            subj = ppl_by_key[key]
        elif key.startswith("person:"):
            subj = key.split(":", 1)[1].split("|")[0].title()
        else:
            # a company-site URL from the funding watch — the domain is the name
            from urllib.parse import urlparse
            subj = (urlparse(key).netloc or key).removeprefix("www.")
        for ev in evs:
            activity.append({"at": ev.get("at", ""), "k": ev.get("kind", ""),
                             "t": ev.get("text", ""), "s": subj})
    activity.sort(key=lambda a: a["at"], reverse=True)
    activity = activity[:400]

    # X hiring tweets: the ledger data/x_tweets.json that the Grok sweep
    # writes. The tweet IS the lead, so the view carries it verbatim and Eric
    # promotes by hand; newest-found first is the reading order.
    try:
        tweets = json.loads(
            (ROOT / "data" / "x_tweets.json").read_text(encoding="utf-8")
        ).get("tweets", [])
    except (OSError, ValueError):
        tweets = []
    tweets = sorted(
        tweets, key=lambda x: (x.get("found") or "", x.get("date") or ""), reverse=True
    )

    payload = json.dumps(
        {"rows": rows, "stats": stats, "activity": activity, "tweets": tweets,
         "role_order": ROLE_ORDER, "diag": _diagnostics(entries),
         "companies": comps, "people": ppl, "sendpri": sendpri, "game": game,
         "built": date.today().isoformat()},
        ensure_ascii=False)
    page = _TEMPLATE.replace("__DATA__", payload).replace(
        "__GENERATED__", html.escape(str(len(rows)))
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return out_path


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SteinJobs</title>
<script>
  try { var t = localStorage.getItem("theme");
        if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; }
  catch (e) {}
</script>
<style>
  :root{
    /* Warm paper, moss, clay. All text tokens verified >=4.5:1 on card. */
    --bg:#f2ede0; --card:#fdfbf5; --ink:#2b2721; --muted:#6c6152; --line:#cfc3ab; --line2:#8f7b4e;
    --accent:#4f6b38; --hot:#2e6a50; --warn:#95682c; --kill:#a33d2e;
    --sh-rest:0 1px 2px rgba(43,39,33,.07);
    --founder-edge:#9a7616; --warmlead-edge:#9a4526;
    --emory-edge:#28457a; --nmh-edge:#4d9bd6;
    --t-xs:11px; --t-sm:12px; --t-md:14px; --t-lg:16px; --t-xl:24px;
    --sp-2:8px; --sp-3:12px; --sp-4:16px;
    --r-sm:6px; --r-md:10px; --r-pill:999px;
    --chip-h:22px; --gap-chip:4px; --chipfill:rgba(23,18,10,.06);
    --on-solid:#fff;
    --a0:rgba(255,255,255,0); --a1:rgba(255,255,255,.08);
    --a2:rgba(255,255,255,.16); --a3:rgba(255,255,255,.24);
    --a4:rgba(255,255,255,.32); --a5:rgba(255,255,255,.42);
    --a6:rgba(255,255,255,.92);
    --b1:rgba(0,0,0,.08); --b2:rgba(0,0,0,.15);
    --b3:rgba(0,0,0,.32); --b4:rgba(0,0,0,.45);
    --mossh:96; --goldh:43; --clayh:16; --redh:8;
    --emberh:28; --minth:160; --skyh:203; --roseh:340;
    --sig-alumni:hsla(217,52%,38%,.30); --sig-nmh:hsla(203,72%,58%,.30);
    --sig-sports:hsla(142,48%,38%,.30); --sig-music:hsla(280,45%,48%,.30);
    --sig-neuro:hsla(178,52%,36%,.30); --sig-inst:hsla(32,60%,44%,.30);
    --sig-founder:hsla(43,68%,46%,.30); --sig-conn:hsla(16,60%,45%,.30);
    --sig-recruiter:hsla(210,14%,52%,.30); --sig-family:hsla(340,42%,54%,.30);
    --sh-raised:0 4px 14px rgba(43,39,33,.16);
    --sh-overlay:0 10px 30px rgba(10,8,4,.3);
    --sh-modal:0 14px 48px rgba(10,8,4,.4);
    --ring:0 0 0 1px var(--line2);
    --dur-1:120ms; --dur-2:200ms; --dur-3:320ms;
    --ease-spring:cubic-bezier(.3,1.4,.4,1);
    --z-raised:10; --z-fab:60; --z-modal:80; --z-top:99;
    --hdr-bg:#182417; --hdr-ink:#edf0e2; --sub-bg:#e9e2d0;
    --serif:ui-serif,Georgia,'Times New Roman',serif;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
  }
  @media (prefers-color-scheme:dark){
    :root:not([data-theme="light"]){--bg:#15120e;--card:#262119;--ink:#f4eddc;--muted:#bfb29b;--line:#4d4433;--line2:#70634a;--accent:#b3c791;--hot:#98cfaa;--warn:#e0b168;--kill:#d97e66;--sh-rest:none;--founder-edge:#e2bb56;--warmlead-edge:#dc8d66;--emory-edge:#a3bdec;--nmh-edge:#a3d6f7;--hdr-bg:#0a100a;--hdr-ink:#eef3e3;--sub-bg:#1c1813;--chipfill:rgba(255,255,255,.07);--miss-ink:#e6dcc2;--sh-raised:0 0 0 1px var(--line2),0 8px 24px rgba(0,0,0,.5)}
  }
  *{box-sizing:border-box}
  html,.cardbox,.actlist,.kb,.dpre,.modal pre,.histbtn .histpop{scroll-behavior:smooth}
  @media (prefers-reduced-motion:reduce){html,.cardbox,.actlist,.kb,.dpre,.modal pre,.histbtn .histpop{scroll-behavior:auto}}
  [hidden]{display:none !important}
  body{margin:0;color:var(--ink);font-family:var(--serif);
    background:
      radial-gradient(1100px 520px at 85% -8%, color-mix(in srgb, var(--accent) 7%, transparent), transparent 70%),
      radial-gradient(900px 480px at -8% 34%, hsla(var(--goldh),54%,38%,.06), transparent 70%),
      radial-gradient(700px 420px at 55% 108%, color-mix(in srgb, var(--hot) 5%, transparent), transparent 70%),
      var(--bg);
    font-size:var(--t-md);line-height:1.5}
  .skip{position:fixed;left:8px;top:-100px;z-index:calc(var(--z-top) + 1);background:var(--accent);
    color:var(--on-solid);padding:8px 14px;border-radius:var(--r-sm);transition:top var(--dur-1)}
  .skip:focus{top:8px}

  /* Dark identity band on top — brand + tabs centered — with the per-tab
     filter row on the paper band below it. Two zones, clearly different. */
  header{position:sticky;top:0;z-index:var(--z-raised);border-bottom:1px solid var(--line);padding:0}
  /* Native-wrapper mode: the Mac shell draws the titlebar, tabs and the
     Liquid Glass backdrop. The page keeps only a translucent tint. */
  html.wrapper,html.wrapper body{background:transparent}
  html.wrapper body{background:color-mix(in srgb, var(--bg) 58%, transparent)}
  html.wrapper:not([data-theme="light"]) body,
  html.wrapper[data-theme="dark"] body{background:color-mix(in srgb, var(--bg) 50%, transparent)}
  html.wrapper .hlogo,html.wrapper .tabs{display:none}
  html.wrapper header{border-bottom:0}
  html.wrapper .hband{background:transparent;padding-top:58px}
  html.wrapper .controls,html.wrapper .hband{box-shadow:none}
  .hband{background:var(--hdr-bg);color:var(--hdr-ink);padding:10px 20px}
  .hrow{display:flex;align-items:center;justify-content:center;gap:var(--sp-4);
    flex-wrap:wrap;position:relative}

  .tabs{display:flex;gap:2px;background:var(--a1);
    backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
    border:1px solid var(--a2);border-radius:var(--r-pill);padding:4px}
  .tab{border:0;background:none;font:inherit;font-family:var(--sans);
    font-size:var(--t-sm);font-weight:600;
    color:color-mix(in srgb, var(--hdr-ink) 72%, transparent);letter-spacing:.2px;
    padding:6px 14px;min-height:32px;border-radius:var(--r-pill);cursor:pointer}
  .tab{position:relative;z-index:1}
  .tab.on{color:var(--on-solid);font-weight:700}
  .tabs{position:relative}
  #tabslide{position:absolute;top:3px;bottom:3px;left:0;width:0;z-index:0;
    background:var(--accent);border-radius:var(--r-pill);
    border:1px solid var(--a4);box-sizing:border-box;
    box-shadow:0 0 8px 1px hsla(var(--mossh),45%,55%,.35);
    transition:left var(--dur-3) var(--ease-spring),width var(--dur-3) var(--ease-spring)}
  .tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .hspace{display:none}
  .hlogo{position:absolute;left:6px;top:50%;transform:translateY(-50%);
    color:var(--hdr-ink);font-family:var(--serif);pointer-events:none}
  .hlogo .hword{pointer-events:auto}
  @keyframes logoparty{0%{transform:none;filter:none}
    25%{transform:rotate(-6deg) scale(1.14)}
    50%{transform:rotate(5deg) scale(1.22);filter:hue-rotate(150deg) saturate(2.2)}
    75%{transform:rotate(-3deg) scale(1.08);filter:hue-rotate(300deg)}
    100%{transform:none;filter:none}}
  .hword.party{animation:logoparty .9s var(--ease-spring)}
  .hlogo .hword{display:inline-block;cursor:pointer;
    font-size:var(--t-xl);font-style:italic;font-weight:800;
    letter-spacing:.7px;opacity:1;
    text-shadow:0 1px 0 var(--b3)}
  @media (max-width:1240px){.hlogo .hword{display:none}}
  @media (max-width:1120px){.hlogo{position:static;transform:none}}
  .sep{width:1px;align-self:stretch;background:var(--line)}
  .hbtns{display:flex;gap:10px;position:absolute;right:0;top:50%;transform:translateY(-50%)}
  /* Below ~1120px the absolute button cluster overlapped the centered tabs
     (the theme toggle sat on the People tab). Let it wrap under them instead. */
  @media (max-width:1120px){
    .hbtns{position:static;transform:none;margin-left:0}
    .hrow{row-gap:8px}
  }
  /* Solid mid-forest buttons: darker than the pills, lighter than the band —
     the translucent white version read as washed-out chips. */
  .hbtns>button{background:var(--a2);color:var(--hdr-ink);
    backdrop-filter:blur(12px) saturate(1.3);
    -webkit-backdrop-filter:blur(12px) saturate(1.3);
    border-color:var(--a3);font-family:var(--sans);
    box-shadow:inset 0 1px 0 var(--a2)}
  .hbtns>button:hover{background:var(--a3);border-color:var(--a4)}
  #themebtn{width:38px;height:38px;border-radius:50%;padding:0;min-height:38px}
  @keyframes themeflip{0%{transform:rotate(0) scale(1)}
    50%{transform:rotate(180deg) scale(.55)}100%{transform:rotate(360deg) scale(1)}}
  #themebtn.flip{animation:themeflip var(--dur-3) ease}
  @keyframes qflash{0%,100%{box-shadow:0 0 0 0 hsla(var(--mossh),45%,55%,0)}
    12%{box-shadow:0 0 0 5px hsla(var(--mossh),45%,55%,.4)}24%{box-shadow:0 0 0 0 hsla(var(--mossh),45%,55%,0)}}
  #queue-btn.hasq{background:var(--accent);border-color:var(--accent);color:var(--on-solid);
    animation:qflash 6s ease infinite}
  .controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;
    justify-content:center;
    background:color-mix(in srgb, var(--sub-bg) 82%, transparent);
    backdrop-filter:blur(12px) saturate(1.15);
    -webkit-backdrop-filter:blur(12px) saturate(1.15);padding:8px 20px;
    box-shadow:var(--sh-overlay)}
  .scoutbar{padding:0 20px 8px;background:var(--sub-bg)}
  input,textarea{font:inherit;font-family:var(--sans)}
  textarea{border:1px solid var(--line);border-radius:var(--r-md);background:var(--card);
    color:var(--ink);padding:8px 12px;font-size:var(--t-sm);width:100%;resize:vertical}
  input{font-size:var(--t-sm);padding:6px 12px;min-height:32px;
    border:1px solid var(--line);border-radius:var(--r-pill);background:var(--card);color:var(--ink)}
  input:disabled{opacity:.45;cursor:not-allowed}
  select{font:inherit;font-family:var(--sans);font-size:var(--t-sm);
    padding:6px 24px 6px 12px;min-height:32px;
    text-align:center;text-align-last:center;
    border:1px solid var(--line);border-radius:var(--r-pill);background:var(--card);
    color:var(--muted);cursor:pointer;-webkit-appearance:none;appearance:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%23908573'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 12px center}
  select:hover{border-color:var(--accent);color:var(--ink)}
  input:focus-visible,select:focus-visible,textarea:focus-visible,
  .chip:focus-visible,.btn:focus-visible,.linklike:focus-visible{
    outline:2px solid var(--accent);outline-offset:2px}
  input[type=search]{min-width:210px}
  .chip.qbtn{width:32px;height:32px;padding:0;justify-content:center;border-radius:50%;font-size:var(--t-lg);font-weight:700}
  .chip{font:inherit;font-family:var(--sans);padding:4px 12px;min-height:32px;
    display:inline-flex;align-items:center;
    border:1px solid var(--line);border-radius:var(--r-pill);
    background:var(--card);cursor:pointer;font-size:var(--t-sm);color:var(--muted)}
  .chip.empty0:not(.on){opacity:.45}
  .chip.swchip{border-style:dashed;opacity:.8;font-size:var(--t-xs)}
  .chip.on{background:var(--accent);border-color:var(--accent);color:var(--on-solid)}
  .modebar{position:relative;display:inline-flex;gap:2px;padding:4px;
    border:1px solid var(--line);border-radius:var(--r-pill);background:var(--card)}
  .modebar .chip{border:0;background:none;position:relative;z-index:1;min-height:26px}
  .modebar .chip.on{background:none;border:0;color:var(--on-solid)}
  .modeslide.kill{background:var(--kill)}
  .modeslide{position:absolute;z-index:0;background:var(--accent);
    border-radius:var(--r-pill);left:0;top:3px;width:0;height:0;
    transition:left var(--dur-2) var(--ease-spring),width var(--dur-2) var(--ease-spring)}
  .btn{font:inherit;font-family:var(--sans);font-size:var(--t-sm);font-weight:600;
    padding:8px 14px;min-height:32px;
    border:1px solid var(--line);border-radius:var(--r-sm);
    background:var(--card);color:var(--ink);cursor:pointer}
  /* Hover language, one voice: interactive things get an accent edge and a
     soft shadow; active/selected things get the solid accent fill. */
  .btn:hover{border-color:var(--accent);box-shadow:var(--sh-raised)}
  .chip:hover{border-color:var(--accent)}
  select:hover{border-color:var(--accent)}
  input:hover,textarea:hover{border-color:var(--line2)}
  .icn:hover{border-color:var(--accent);box-shadow:var(--sh-raised)}
  .tri:hover{box-shadow:var(--sh-raised)}
  .tab:not(.on):hover{background:var(--a1);color:var(--hdr-ink)}
  .btn:focus-visible,.chip:focus-visible,.tri:focus-visible,.icn:focus-visible,
  .kbpg:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:var(--on-solid)}
  .linklike{border:0;background:none;color:var(--accent);cursor:pointer;
    font:inherit;font-size:var(--t-xs);padding:0;text-decoration:underline}

  .scoutmsg{font-size:var(--t-xs);color:var(--muted);font-variant-numeric:tabular-nums}
  .scouttrack{height:4px;background:var(--line);border-radius:var(--r-pill);margin-top:4px}
  .scoutfill{height:100%;width:0;background:var(--accent);border-radius:var(--r-pill);
    transition:width var(--dur-3) ease}

  main{padding:14px 24px 60px;max-width:1720px;margin:0 auto}
  .empty{color:var(--muted);padding:28px;text-align:center;background:var(--card);
    border:1px solid var(--line);border-radius:var(--r-md);box-shadow:var(--sh-rest);
    max-width:420px;margin:24px auto}
  .tsec{margin:20px 0 8px;font-size:var(--t-lg);text-transform:uppercase;
    letter-spacing:.6px;color:var(--muted);font-family:var(--serif);
    display:flex;align-items:center;gap:14px}
  .tsec>span{display:flex;align-items:center;gap:8px}
  .tsec>span::before{content:"❧";color:var(--accent);font-size:var(--t-md)}
  .tsec::before{content:"";flex:1;height:1px;background:var(--line)}
  .tsec::after{content:"";flex:1;height:1px;
    background:linear-gradient(to right, var(--line), transparent)}
  .tgroup{margin:12px 0 6px;font-size:var(--t-sm);font-weight:600;color:var(--ink);
    font-family:var(--serif)}
  .cohead[role="button"]{cursor:pointer}
  .cohead[role="button"]:hover .colink{text-decoration:underline}
  .cohead{margin:16px 0 8px;font-size:var(--t-lg);font-weight:600;color:var(--ink);
    font-family:var(--serif);display:flex;align-items:center;gap:8px}
  #people .cohead:first-child{margin-top:2px}
  #people .cohead{justify-content:flex-start;margin:0 0 10px;flex-wrap:wrap;row-gap:4px}
  #people .cohead .cname{font-style:italic}
  .cogroup{background:
      linear-gradient(var(--a1),var(--a0) 42%),
      linear-gradient(hsla(var(--indh,96),36%,46%,.13),hsla(var(--indh,96),36%,46%,.13)),var(--card);
    box-shadow:inset 0 1px 0 var(--a2);
    border:1px solid var(--line2);border-radius:var(--r-md);
    padding:10px 12px 14px;position:relative}
  /* the company header is the box's divider tab — an absolute extension of
     the top edge sharing the box's exact background and border, so tab and
     box read as one die-cut card that traps the contact cards */
  #people .cogroup{margin-top:32px;padding-top:12px}
  /* bottom-anchored so the tab's base sits ON the box's outer edge no
     matter how paddings shift — top-anchoring guessed wrong twice.
     Centered on the box, tall enough that the title carries the same air
     the cards get at the box bottom. */
  #people .cohead{position:absolute;bottom:calc(100% + 1px);left:50%;
    transform:translateX(-50%);height:34px;white-space:nowrap;
    width:max-content;max-width:calc(100% - 16px);flex-wrap:nowrap;
    display:inline-flex;align-items:center;margin:0;padding:4px 16px 0;
    background:linear-gradient(hsla(var(--indh,96),42%,52%,.48),hsla(var(--indh,96),36%,46%,.13)),var(--card);
    border:1px solid var(--line2);border-bottom:none;
    box-shadow:inset 0 1px 0 var(--a3);
    border-radius:var(--r-md) var(--r-md) 0 0}
  /* the tab ends exactly at the box's top edge — this 1px strip erases the
     box border under the tab so they merge, while the tab's side borders
     land cleanly ON the border line instead of overshooting past it */
  #people .cohead::after{content:"";position:absolute;left:0;right:0;
    bottom:-1px;height:1px;
    background:linear-gradient(hsla(var(--indh,96),36%,46%,.13),hsla(var(--indh,96),36%,46%,.13)),var(--card)}
  #people .pgrid{display:block}
  #people .pgrid .pcard{margin-bottom:12px}
    #people .mcol>.pcard{margin-left:12px;margin-right:12px}
  #people .pgrid .pcard:last-child{margin-bottom:0}
  .colink{font:inherit;background:none;border:0;padding:0;color:inherit;cursor:pointer}
  /* an inline-block button with a 24px line-height inflates its host line
     box to ~36px — cap it so title rows stay one text line (2026-08-12) */
  .ptitle .colink{line-height:1.1;white-space:nowrap}
  /* the 24px separator dot carried a 36px line-height — it, not the text,
     set every title row's height (measured 2026-08-12) */
  .ptitle .tsep{line-height:1}
  .colink:hover{color:var(--accent);text-decoration:underline}
  .co{color:var(--muted);font-weight:400}
  .why{color:var(--ink);opacity:.82;font-size:var(--t-sm);margin-top:4px;
    font-family:var(--serif);font-style:italic}
  .meta{color:var(--muted);font-size:var(--t-xs);margin-top:4px;display:flex;gap:8px;flex-wrap:wrap}
  .meta span{white-space:nowrap}
  .meta a,.descr a,.why a{color:var(--accent)}
  .badge{font-size:var(--t-xs);text-transform:uppercase;letter-spacing:.4px;
    padding:4px 8px;border-radius:var(--r-sm);border:1px solid var(--line);
    color:var(--muted);white-space:nowrap}
  .b-saved{border-color:var(--hot);color:var(--hot)}
  .b-applied,.b-interviewing,.b-offer{border-color:var(--accent);color:var(--accent)}
  .b-dormant{border-color:var(--warn);color:var(--warn)}
  .b-rejected,.b-uninterested{opacity:.45}
  .sig,.ind,.cchip,.alum,.badge,.newpost,.warmtag,.kbdate,.salchip,.raisedchip,.tri,.icn,
  .pmeta,.meta,.tlabel,.kbhead,.acttime,.plab{font-family:var(--sans)}
  .sig{font-size:var(--t-xs);border:1px solid transparent;color:var(--ink);
    background:color-mix(in srgb, var(--warn) 18%, transparent);
    border-radius:var(--r-pill);padding:2px 8px;white-space:nowrap;font-weight:700;
    height:var(--chip-h);box-sizing:border-box;display:inline-flex;align-items:center}
  /* Same alpha recipe as the industry chips: 30% fill, 65% border. */
  .sig-alumni{background:var(--sig-alumni);color:var(--ink)}
  .sig-emory{background:var(--sig-alumni);color:var(--ink)}
  .sig-nmh{background:var(--sig-nmh);color:var(--ink)}
  /* Sports is its own warmth tier — Eric did NFL data science for the Jets, so
     it opens doors like a school does, but it is not an alum. Turf green keeps
     it clearly apart from the two blue school chips. */
  .sig-sports{background:var(--sig-sports);color:var(--ink)}
  /* Music — the band thread. Violet, well clear of the turf green. */
  .sig-music{background:var(--sig-music);color:var(--ink)}
  /* Neuroscience + psychedelics, one research tier. Teal. */
  .sig-neuro{background:var(--sig-neuro);color:var(--ink)}
  /* Somewhere Eric actually was — UCSF, Carter Center, Foxino, CIEE. Amber. */
  .sig-inst{background:var(--sig-inst);color:var(--ink)}
  .sig-founder{background:var(--sig-founder);color:var(--ink)}
  /* A 1st-degree connection is the warmest signal there is — deep terracotta. */
  .sig-conn{background:var(--sig-conn);color:var(--ink)}
  /* Recruiters are a channel, not a target — slate, same alpha recipe. */
  .sig-recruiter{background:var(--sig-recruiter);color:var(--ink)}
  .sig-family{background:var(--sig-family);color:var(--ink)}
  .warmtag.sig{background:var(--sig-conn);color:var(--ink)}
  .rel{font-size:var(--t-xs);background:var(--warn);border:1px solid var(--warn);
    color:var(--on-solid);border-radius:var(--r-pill);padding:2px 8px;white-space:nowrap;font-weight:600}
  .alum{font-size:var(--t-xs);border:1px solid var(--hot);color:var(--hot);
    border-radius:var(--r-pill);padding:2px 8px;white-space:nowrap;font-weight:600}
  .ind{font-size:var(--t-xs);border:1px solid var(--line);color:var(--ink);
    border-radius:var(--r-pill);padding:2px 8px;white-space:nowrap;font-weight:800;
    height:var(--chip-h);box-sizing:border-box;display:inline-flex;align-items:center}
  .newpost{font-size:var(--t-xs);background:var(--hot);color:var(--on-solid);border-radius:var(--r-pill);
    padding:2px 10px;white-space:nowrap;font-weight:600}
  .eh{color:var(--warn);font-weight:600}
  .visa-yes{color:var(--hot);font-weight:600}
  .visa-no{color:var(--muted);text-decoration:line-through}
  /* Freshness IS urgency: a 2-day-old posting wants action, a month-old one
     mostly wants skepticism. Fresh glows, stale fades. */
  /* The when-chip outranks its neighbors — filled, not just outlined. */
  .cchip.agechip{background:hsla(var(--ageh,96),45%,48%,.18);font-weight:600;
    color:hsl(var(--ageh,96),62%,27%)}
  :root:not([data-theme="light"]) .cchip.agechip{color:hsl(var(--ageh,96),78%,68%);
    background:hsla(var(--ageh,96),40%,45%,.24)}
  .agefresh{color:var(--hot);border-color:var(--hot);font-weight:600}
  .agemoss{color:hsl(var(--mossh),30%,45%);border-color:hsl(var(--mossh),32%,42%)}
  .ageold{opacity:.55}
  .age-warm{color:var(--warn)}
  .age-hot{color:var(--warn);font-weight:700}
  .art{font-size:var(--t-xs);border:1px solid var(--line);border-radius:var(--r-sm);
    padding:2px 6px;color:var(--muted)}
  .stsel{font-size:var(--t-xs);min-height:26px;padding:2px 24px 2px 10px}
  .cosel.m-saved{border-color:var(--accent);color:var(--accent);font-weight:600}
  .cosel.m-uninterested{border-color:var(--warn);color:var(--warn)}
  .datebtn{border:0;background:none;cursor:pointer;font-size:var(--t-md);padding:2px;color:var(--muted)}
  .datebtn:hover{color:var(--ink)}

  /* postings rows */
  .row{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
    padding:var(--sp-3) var(--sp-4);margin-bottom:8px;display:grid;
    grid-template-columns:44px 1fr auto;gap:12px;align-items:start;cursor:pointer}
  .row.noexp{cursor:default}
  .row[role=button]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .score{font-weight:700;font-size:var(--t-lg);text-align:right;font-variant-numeric:tabular-nums}
  /* Ring bands: moss for the 70s, gold for the 80s, hot green at 90+ —
     the grid's best rows glow without reading a single number. */
  .s-90{color:var(--hot)} .s-80{color:hsl(var(--goldh),62%,50%)} .s-70{color:hsl(var(--mossh),30%,52%)}
  .sgrad{color:hsl(var(--sh,96),72%,38%)}
  :root:not([data-theme="light"]) .sgrad{color:hsl(var(--sh,96),75%,58%)}
  .s-mid{color:var(--ink)} .s-lo{color:var(--muted)} .s-none{color:var(--muted);font-size:var(--t-sm)}
  .title{font-weight:600}
  .title a{color:inherit;text-decoration:none} .title a:hover{text-decoration:underline}
  .rowbtns{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;align-items:center}
  .rowbtns .btn{font-size:var(--t-xs);padding:6px 12px;min-height:32px}
  .exp{grid-column:1/-1;border-top:1px solid var(--line);margin-top:10px;padding-top:10px;
    font-size:var(--t-sm);cursor:default}
  .exp h4{margin:16px 0 6px;font-size:var(--t-xs);text-transform:uppercase;letter-spacing:.5px;
    color:var(--ink);font-weight:700;display:flex;align-items:center;gap:8px}
  .exp .twocol h4,.exp>h4:first-child{margin-top:2px}
  /* Long prose reads upright: serif stays, italics are for one-liners. */
  .exp .descr{color:var(--ink);line-height:1.65;font-family:var(--serif);
    font-size:var(--t-md)}
  .exp .descr b{font-weight:700}
  .exp .descr>.co{color:inherit}
  /* wide card frame, narrow prose column — 195-char lines are unreadable */
  .exp .descr,.exp .why,.exp .founder,.exp .warm{max-width:72ch}
  .exp .twocol{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:center}
  @media (max-width:900px){.exp .twocol{grid-template-columns:1fr}}
  .exp .pstack>div,.exp .tcleft>div,.exp .tcright>div,.exp .pplbox{
    background:linear-gradient(hsla(var(--indh,96),36%,50%,.04),hsla(var(--indh,96),36%,50%,.04)),var(--card);
    border:1px solid hsla(var(--indh,96),30%,40%,.35);
    border-radius:var(--r-md);padding:12px 16px 14px}
  .exp .pplbox{margin-top:14px}
  .exp .pplbox h4{margin-top:0}

  .exp .pplbox>.why{margin:0;font-size:var(--t-sm)}
  .exp .pplbox>.descr{margin:0;max-width:none}
  .exp .pplbox:has(>.descr:last-child){padding-bottom:10px}
  .exp .tcright>div+div{margin-top:14px}
  .exp h4{color:hsl(var(--indh,96),42%,30%);font-weight:800;letter-spacing:.8px;
    font-family:var(--sans);border-bottom:1px solid hsla(var(--indh,96),30%,40%,.3);
    padding-bottom:6px;margin-bottom:8px;text-align:center}
  :root:not([data-theme="light"]) .exp h4{color:hsl(var(--indh,96),40%,68%)}
  :root:not([data-theme="light"]) .exp .pstack>div,
  :root:not([data-theme="light"]) .exp .tcleft>div,
  :root:not([data-theme="light"]) .exp .tcright>div,
  :root:not([data-theme="light"]) .exp .pplbox{
    background:linear-gradient(hsla(var(--indh,96),36%,50%,.16),hsla(var(--indh,96),36%,50%,.16)),var(--card)}
  .exp .twocol .descr,.exp .pstack .descr{max-width:none}
  .exp .pstack>div+div{margin-top:14px}
  .exp .pstack>.fitbox{background:linear-gradient(hsla(var(--goldh),70%,52%,.12),hsla(var(--goldh),70%,52%,.12)),var(--card);
    border-color:hsla(var(--goldh),62%,45%,.55);border-left:4px solid var(--warn)}
  .exp .fitbox h4{color:hsl(var(--goldh),62%,32%);border-bottom-color:hsla(var(--goldh),62%,45%,.4)}
  :root:not([data-theme="light"]) .exp .pstack>.fitbox{
    background:linear-gradient(hsla(var(--goldh),60%,50%,.14),hsla(var(--goldh),60%,50%,.14)),var(--card)}
  :root:not([data-theme="light"]) .exp .fitbox h4{color:hsl(43,70%,66%)}
  .founder{font-size:var(--t-xs);margin-top:4px}
  .founder a{color:var(--accent)}
  .warm{font-size:var(--t-xs);margin-top:4px;color:var(--warn)}
  details.hist{margin-top:6px;font-size:var(--t-xs);color:var(--muted)}
  details.hist summary{cursor:pointer;user-select:none}
  details.hist .ev{padding:2px 0 2px 12px;border-left:2px solid var(--line);margin-left:4px}
  .whybox{background:var(--bg);border:1px solid var(--line);border-radius:var(--r-md);
    padding:10px 12px;margin-top:8px}
  .whybox textarea{width:100%;min-height:52px;font:inherit;font-size:var(--t-sm);margin-top:6px;
    border:1px solid var(--line);border-radius:var(--r-sm);background:var(--card);color:var(--ink);padding:6px}
  .check{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;padding:8px 10px;
    border:1px solid var(--line);border-radius:var(--r-md);background:var(--bg);font-size:var(--t-sm)}
  .check label{display:flex;gap:6px;align-items:center;cursor:pointer;user-select:none}
  .check input{margin:0;min-height:auto}
  .check .done{color:var(--accent)}
  .postlist{margin:4px 0 8px;padding-left:16px;font-size:var(--t-xs)}
  .postlist a{color:var(--accent)}

  /* stat tiles + kanban */
  /* Daily goals + lifetime scoreboard share one tile language on one row,
     goals condensed on the left of the break. A hit goal fills solid green. */
  .connbar{display:block;width:100%;text-align:left;font:inherit;font-family:var(--sans);
    font-size:var(--t-sm);color:var(--ink);cursor:pointer;margin:2px 0 12px;
    background:hsla(var(--goldh),62%,50%,.14);border:1px dashed hsla(var(--goldh),62%,50%,.65);
    border-radius:var(--r-md);padding:8px 14px}
  .connbar:hover{background:hsla(var(--goldh),62%,50%,.22)}
  .queuebar{background:hsla(var(--mossh),30%,45%,.10);border-color:hsla(var(--mossh),30%,40%,.55);cursor:default}
  .queuebar .qbtitle{font-weight:700}
  .queuebar .qbrow{margin-top:4px;color:var(--ink)}
  .queuebar .linklike{padding:0;font-size:inherit}
  .tilerow{display:flex;gap:12px;align-items:stretch;flex-wrap:wrap;margin:2px 0 0;
    justify-content:space-between}
  .tilerow+.tsec,.connbar~.tsec:first-of-type{margin-top:14px}
  .tiles{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
  .tilesep{width:1px;background:var(--line);align-self:stretch}
  .tilegroup{flex:1;display:flex;flex-direction:column;gap:6px;align-items:center}
  .tglabel{font-family:var(--sans);font-size:var(--t-xs);text-transform:uppercase;
    letter-spacing:.6px;color:var(--muted);font-weight:700;text-align:center}
  .tile{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);box-shadow:var(--sh-rest);
    padding:8px 10px;text-align:center;min-width:74px;
    display:flex;flex-direction:column;justify-content:center;align-items:center}
  .tiles .tnum{font-size:var(--t-lg)}
  .tile.goal-hit{background:var(--accent);border-color:var(--accent)}
  .tile.goal-hit .tnum,.tile.goal-hit .tlabel,.tile.goal-hit .co{color:var(--bg)}
  .tile.pulse{animation:tilepulse .9s ease 1}
  /* Goals are meters, lifetime is a ledger — pill-shaped and accent-edged
     so the two rows can't be misread as one. */
  .tilerow .tile{min-height:52px}
  .tile.zero{opacity:.5}
  .tile.zero .tnum{color:var(--muted)}
  @keyframes tilepulse{
    0%{transform:scale(1)}
    35%{transform:scale(1.08);box-shadow:0 0 0 6px color-mix(in srgb, var(--accent) 25%, transparent)}
    100%{transform:scale(1)}
  }
  .tnum{font-size:var(--t-xl);font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums;
    font-family:var(--serif)}
  .tlabel{font-size:var(--t-xs);color:var(--muted);margin-top:2px}
  .tsub{font-size:var(--t-xs);color:var(--hot);font-weight:600}
  .kb{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(220px,1fr);
    gap:var(--sp-2);overflow-x:auto;align-items:start;padding-bottom:6px}
  .kbcol{background:var(--bg);border:1px solid var(--line);border-radius:var(--r-md);
    padding:8px}
  .kbhead{font-size:var(--t-sm);text-transform:uppercase;letter-spacing:.5px;
    font-weight:700;color:var(--ink);padding:0 2px 10px;display:flex;align-items:center;gap:6px}
  .kbpager{margin-left:auto;display:flex;align-items:center;gap:2px;text-transform:none;letter-spacing:0}
  .kbpg{font:inherit;font-size:var(--t-sm);min-height:32px;min-width:26px;border:0;
    background:none;color:var(--accent);cursor:pointer;border-radius:var(--r-sm)}
  .kbpg:hover{background:var(--card)}
  #controls select.actv{background-color:var(--accent);color:var(--on-solid);
    border-color:var(--accent);background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%23fffdf5'/%3E%3C/svg%3E")}
  .addpill{border-radius:var(--r-pill);border-color:var(--accent);color:var(--accent);
    background:transparent;font-weight:600}
  .addpill:hover{background:var(--accent);color:var(--on-solid)}
  .kbpg[disabled]{color:var(--line);cursor:default}
  .pagefade{position:fixed;left:0;right:0;bottom:0;height:64px;z-index:5;
    pointer-events:none;background:linear-gradient(to bottom, transparent, var(--b3))}
  .kbempty{opacity:.6}
  .kbempty .kbhead{padding-bottom:0}
  .kbstub{display:flex;justify-content:center;padding:8px 4px;min-height:132px}
  .showmore{flex-basis:100%;display:flex;justify-content:center;margin:12px 0 4px}
  .kbstub .kbvlabel{writing-mode:vertical-rl;font-family:var(--sans);
    font-size:var(--t-xs);text-transform:uppercase;letter-spacing:.6px;
    color:var(--muted);display:flex;align-items:center;gap:6px}
  .kbstub.dragover .kbvlabel{color:var(--accent)}
  .kbnone{border:1px dashed var(--line2);border-radius:var(--r-md);padding:16px;
    text-align:center;color:var(--muted);font-style:italic;margin-bottom:4px}
  .kbcard{background:var(--card);border:1px solid var(--line);border-radius:var(--r-sm);box-shadow:var(--sh-rest);
    padding:8px 10px;margin-bottom:6px;cursor:pointer}
  .kbtags{padding:6px 0 0;column-gap:4px}
  .kbtags>*{margin-top:4px}
  .stallchip{color:var(--kill);background:color-mix(in srgb, var(--kill) 16%, transparent)}
  .kbcard:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
  .kbtitle{font-size:var(--t-sm);font-weight:600;line-height:1.3}
  .kbco{font-size:var(--t-xs);color:var(--muted);margin-top:2px}
  .kbdate{font-size:var(--t-xs);color:var(--hot);margin-top:2px}
  .kbdate.stall{color:hsl(var(--clayh),60%,52%)}
  .sweepdone{color:var(--hot);border-color:var(--hot)}
  .sweepq{border-style:dashed;color:hsl(var(--goldh),62%,45%);border-color:hsl(var(--goldh),62%,50%)}
  .kbacts{display:flex;gap:4px;margin-top:6px;align-items:center}

  /* companies grid */
  /* Postings flow as masonry, like companies: the card fits its own why. */
  .pmason{display:flex;gap:16px;align-items:flex-start}
  .pmason:has(.empty){display:block}
  .mcol{flex:1 1 0;min-width:0;display:flex;flex-direction:column;gap:16px}
  .chip[disabled]{opacity:.35;cursor:default;pointer-events:none}
  .cgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:10px;
    align-items:start}
  /* Masonry: vertical rhythm equals the column gap, tops stagger freely. */
  /* wrap is load-bearing: the Show More strip is a 100%-basis child, and
     without wrap it squeezed the masonry columns to 34px (2026-08-12) */
  #companies{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
  #companies.noflow{display:block}
  /* Concise cards end at their chip row; the ✓/✕ sit level with its last line. */
  #companies .ccard,.tgrid .ccard,#list .ccard,#cardhost .exp .ccard{
    padding-bottom:8px;min-height:0}
  #companies .ccard,.tgrid .ccard{padding-top:6px}
  #companies .ccard .chead,.tgrid .ccard .chead{margin-bottom:6px}
  /* Masonry, not grid: grid rows reserve the tallest card's height, leaving
     dead air under short cards. Columns pack clean at the cost of strict
     row alignment — the trade Eric asked for. */
  /* aligned card tops on the tracker strip — masonry staggered them */
  .tgrid{display:flex;gap:12px;align-items:flex-start;margin:4px 0 20px}
  .ccard{background:var(--card);border:1px solid var(--line2);border-radius:var(--r-md);box-shadow:var(--sh-rest);
    padding:8px 16px 10px;cursor:pointer;display:flex;flex-direction:column}
  .ccard:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .ccard:not(.open),.pcard:not(.open){transition:box-shadow var(--dur-1) ease,border-color var(--dur-1) ease}
  #list .ccard:not(.open):hover,#companies .ccard:not(.open):hover,
  .tgrid .ccard:not(.open):hover,.pmason .ccard:not(.open):hover{
    box-shadow:var(--sh-raised);border-color:var(--ink)}
  .fab,#tabslide{box-shadow:inset 0 1px 0 var(--a4),var(--sh-overlay)}
  .btn,.chip,.tab,.fab{transition:transform var(--dur-1) ease,filter var(--dur-1) ease,background var(--dur-1) ease}
  .btn:active,.chip:active,.tab:active,.fab:active{transform:scale(.96)}
  .btn:hover,.fab:hover{transform:scale(1.025)}
  .tri{transition:transform var(--dur-1) ease,background var(--dur-1) ease,color var(--dur-1) ease}
  .tri:hover{transform:scale(1.12)}
  .cchip.qedit,.ind.qedit{transition:transform var(--dur-1) ease}
  .cchip.qedit:hover,.ind.qedit:hover{transform:scale(1.06)}
  .tile{transition:transform var(--dur-1) ease}
  .tile:hover{transform:scale(1.02)}
  .ccard.open{grid-column:1/-1;cursor:default}
  .chead{display:flex;gap:8px;align-items:center;flex-wrap:nowrap}
  #companies .chead,.tgrid .chead,#list .chead,#cardhost .chead{flex-wrap:nowrap;align-items:center;margin-bottom:8px}
  #list .ccard:not(.open) .ptitle {white-space:normal;line-height:1.3}
  #list .ccard:not(.open) .chead{margin-bottom:2px}
  #list .ccard>.why{opacity:1;line-height:1.65;
    font-size:calc(var(--t-sm) + 1px);margin:6px 0 0}
  #companies .ccard>.why,.tgrid .ccard>.why{margin:0;opacity:1;
    line-height:1.65;font-size:calc(var(--t-sm) + 1px)}
  /* The 28px triage circles hang above the chip baseline — the footer row
     reserves their full height so they never crowd the description. */
  #companies .ccard>.cfoot,.tgrid .ccard>.cfoot,#list .ccard>.cfoot,
  #cardhost .exp .ccard>.cfoot{padding-top:0;min-height:32px;
    align-items:flex-start}
  /* company NAME gets two extra points (Eric, 2026-08-12) — line-height
     pinned to the old 24px box so the card's geometry doesn't move with it */
  #companies .cname,.tgrid .cname{overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;min-width:0;flex:0 1 auto;
    font-size:calc(var(--t-lg) + 2px);line-height:24px}

  #companies .chead .ind,#companies .chead .cchip{flex:none}
  #companies .ccard .ind,.tgrid .ccard .ind{font-weight:700;
    color:hsl(var(--indh,96),48%,18%)}
  :root:not([data-theme="light"]) #companies .ccard .ind,
  :root:not([data-theme="light"]) .tgrid .ccard .ind{color:hsl(var(--indh,96),45%,80%)}
  .cname{font-weight:700;font-size:var(--t-lg);font-family:var(--serif)}
  .ptitle{font-weight:600;font-size:var(--t-lg);font-family:var(--serif);
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:0 1 auto}
  .ptitle a{color:inherit;text-decoration:none}
  .ptitle a:hover{color:var(--accent);text-decoration:underline}
  .pco{font-weight:600;color:var(--ink);font-size:var(--t-md);margin-top:2px;
    display:flex;align-items:center;flex-wrap:wrap}
  .pco .colink,.pcobig{text-decoration:underline dotted;text-underline-offset:3px}
  .tsep{font-family:var(--serif);opacity:.45;margin:0 2px;font-size:var(--t-xl)}
  .pcotags{margin-left:8px;display:inline-flex;align-items:center;gap:6px}
  .ptitle a,.ptitle{font-weight:700;letter-spacing:.15px}
  .pco .colink{font-family:var(--serif);font-style:italic;font-weight:700;
    font-size:var(--t-md);color:hsl(var(--indh,96),48%,20%)}
  :root:not([data-theme="light"]) .pco .colink{color:hsl(var(--indh,96),45%,72%)}
  .extlink{margin-left:6px;font-size:var(--t-sm);text-decoration:none;color:var(--accent)}
  .pco .colink:hover,.pcobig:hover{text-decoration:underline solid}
  .pco .alum{font-weight:600}
  /* The ring IS the score: 50 draws a half circle, 100 closes it. */
  .cscore{font-weight:700;font-variant-numeric:tabular-nums;
    width:24px;height:24px;flex:none;border-radius:50%;padding:2px;
    display:inline-flex;font-size:var(--t-xs);font-family:var(--sans)}
  .cscorein{width:100%;height:100%;border-radius:50%;background:var(--card);
    display:flex;align-items:center;justify-content:center;font-weight:800}
  .sgrad .cscorein{color:hsl(var(--sh,96),80%,26%)}
  :root:not([data-theme="light"]) .sgrad .cscorein{color:hsl(var(--sh,96),80%,72%)}
  .ctoggles{display:flex;gap:4px;margin-left:auto}
  .tgl{border:1px solid var(--line);background:none;border-radius:var(--r-pill);
    cursor:pointer;font-size:var(--t-xs);padding:2px 10px;min-height:26px;color:var(--muted)}
  .tgl.on{background:var(--accent);border-color:var(--accent);color:var(--on-solid)}
  .tgl.off.on{background:var(--warn);border-color:var(--warn)}

  /* people cards */
  /* Inside the card modal both nested grids run single-column, full width —
     a lone posting card at half width beside a full-width person read broken. */
  #cardhost .exp .pgrid{grid-template-columns:repeat(auto-fill,minmax(380px,1fr))}
  .exp .cgrid{display:flex;gap:12px;align-items:flex-start}
  .pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:12px;
    margin-bottom:6px;align-items:start}
  .pcard{background:var(--card);border:1px solid var(--line2);box-shadow:var(--sh-rest);
    border-radius:var(--r-md) var(--r-md) var(--r-sm) var(--r-sm);
    display:flex;gap:10px;cursor:pointer;position:relative;overflow:hidden}
  /* collapsed = two comfortable lines: name + role, chips beneath. The
     ✓/✗ are a REAL flex member on the right (2026-08-12): the old float +
     64px padding reserve let min-content chips slide underneath it. */
  .pcard:not(.open){padding:8px 10px 8px 52px;align-items:center;min-height:72px}
  .pcard:not(.open) .icn{width:24px;height:24px;font-size:var(--t-xs);margin-right:0}
  .pcard:not(.open) .icn .big{font-size:var(--t-sm)}
  .pcard:not(.open) .pband{width:38px}
  .pcard:not(.open) .pbandin{width:28px;height:28px;font-size:var(--t-xs)}
  .pcard:not(.open) .pbody{display:block}
  .pcard:not(.open) .pname{font-size:var(--t-md);min-width:0;
    border-bottom:1px solid hsla(var(--indh,96),40%,38%,.28);padding-bottom:4px}
  /* the name NEVER shrinks — the role absorbs all the squeeze; only a name
     too long for the whole line clips against the card edge */
  .pcard:not(.open) .pnametxt{flex:none;max-width:100%}
  .pdiv{color:var(--muted);flex:none;font-weight:400}
  .pname .prole{flex:0 999 auto;min-width:0}
  /* line 2: one line, always — the company chip is the only shrinkable
     item, so it truncates before icons or ✓/✕ ever wrap (Eric, 2026-08-12) */
  .pline2{display:flex;align-items:center;gap:6px;margin-top:6px;
    flex-wrap:nowrap;overflow:hidden}
  /* distinct by WEIGHT and INK, not size — the t-md serif read obnoxious
     at chip scale (Eric, 2026-08-13) */
  .cchip.cochip{max-width:130px;overflow:hidden;text-overflow:ellipsis;font-weight:800;
    flex:0 1 auto;min-width:44px;color:var(--ink)}
  .pline2 .sigs{margin-left:0;display:flex;gap:4px}
  .pcard:not(:has(>.triage)) .cochip{max-width:220px}
  .pcard:not(.open) .citychip{display:none}
  /* the dashed + duplicates Edit in the expanded view; rows stay clean */
  .pcard:not(.open) .icn.addc{display:none}
  .pcard:not(.open) .prole{font-size:var(--t-md)}
  .pcard:not(.open) .pmeta.icons{display:flex;gap:4px;margin:0}
  .pcard.open{padding:12px 14px 12px 50px;align-items:flex-start}
  .pcard.open .pband{width:38px}
  .pcard.open .pbandin{width:28px;height:28px;font-size:var(--t-xs)}
  .pcard.open .pname{font-size:var(--t-md)}
  .pcard.open .prole{font-size:var(--t-md)}
  /* the expand's own rows (Origin, Alumni, …) already say what the chips
     say — the tag line is collapsed-view furniture */
  .pcard.open .pline2{display:none}
  .pcard:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  /* Card shading retired — the FILLED chips carry warm/school/founder now. */
  .warmtag{font-size:var(--t-xs);color:var(--warmlead-edge);font-weight:600}
  .pband{position:absolute;left:0;top:0;bottom:0;width:44px;
    display:flex;align-items:center;justify-content:center;
    color:var(--on-solid);font-size:var(--t-sm);font-weight:600}
  .pbandin{width:30px;height:30px;border-radius:50%;background:var(--a3);
    display:flex;align-items:center;justify-content:center}
  .pbody{min-width:0;flex:1}
  .pname{font-weight:600;font-size:var(--t-lg);font-family:var(--serif);
    display:flex;align-items:center;gap:6px}
  .pnametxt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .sigs{margin-left:auto;display:flex;gap:4px;align-items:center;flex:none}

  .pcard.open .pmeta.icons{display:none}
  .pmeta{font-size:var(--t-xs);color:var(--muted);margin-top:2px;overflow-wrap:anywhere}
  .prole{font-weight:600;color:var(--ink);opacity:.85;font-size:var(--t-sm);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    font-family:var(--serif);font-style:italic}
  .pmeta a{color:var(--accent)}
  .pcard.sel,.ccard.sel{outline:2px solid var(--accent);outline-offset:1px}
  .ghost{opacity:.45;filter:saturate(.5)}
  .pcard:not(.open) .pname,.pcard:not(.open) .pmeta{
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #splash{position:fixed;inset:0;z-index:var(--z-top);background:var(--hdr-bg);
    display:flex;align-items:center;justify-content:center;
    transition:opacity var(--dur-3) ease}
  #splash.splgo{opacity:0;pointer-events:none}
  .spllogo{font-size:var(--t-xl);transform:scale(2.6);color:var(--accent);font-family:var(--serif);
    animation:splpulse 1.15s ease-in-out infinite}
  @keyframes splpulse{0%,100%{transform:scale(2.6);opacity:.75}50%{transform:scale(2.95);opacity:1}}
  @media (prefers-reduced-motion:reduce){.spllogo{animation:none}}
  /* rolodex side-index: divider tabs down the whole left edge. Click a
     letter, or press and slide — the bubble tracks the letter under your
     finger and release jumps there. */

  .kbhint{position:fixed;left:18px;bottom:18px;z-index:calc(var(--z-fab) + 9);
    border-radius:var(--r-pill);border:1px solid var(--line);background:var(--card);
    color:var(--muted);font:inherit;font-size:var(--t-xs);cursor:pointer;
    padding:6px 12px;min-height:32px;box-shadow:var(--sh-overlay);
    opacity:1;transition:opacity var(--dur-2) ease,transform var(--dur-2) ease}
  .kbhint:hover{opacity:.95}
  .fabs{position:fixed;right:18px;bottom:18px;z-index:calc(var(--z-fab) + 9);display:flex;
    flex-direction:column;align-items:flex-end}
  .fab{min-height:44px;border-radius:var(--r-pill);border:1px solid var(--line2);
    background:linear-gradient(var(--a2),var(--a0) 55%),
      color-mix(in srgb, var(--card) 58%, transparent);
    backdrop-filter:blur(18px) saturate(1.5);
    -webkit-backdrop-filter:blur(18px) saturate(1.5);
    border-color:color-mix(in srgb, var(--line2) 55%, transparent);
    color:var(--ink);font:inherit;font-family:var(--sans);
    font-size:var(--t-sm);font-weight:600;cursor:pointer;
    box-shadow:var(--sh-overlay);
    display:flex;align-items:center;gap:8px;padding:0 16px 0 12px;
    transition:transform var(--dur-1) var(--ease-spring),box-shadow var(--dur-1) ease,
      background var(--dur-1) ease,opacity var(--dur-2) ease}
  .fab .fabicon{font-size:var(--t-lg);line-height:1}
  .fab .fabmag{font-size:var(--t-xl);font-weight:700}
  .fab:hover{box-shadow:var(--sh-modal);border-color:var(--accent);
    transform:translateY(-2px) scale(1.04)}
  .fab:active{transform:translateY(0) scale(.96)}
  .fab.fabout{opacity:0;transform:translateY(8px) scale(.9);pointer-events:none}
  .fab{max-height:44px;margin-top:10px;overflow:hidden}
  .fab.fabhide{opacity:0;transform:translateY(6px) scale(.92);pointer-events:none;
    max-height:0;min-height:0;margin-top:0;padding-top:0;padding-bottom:0;border-width:0}
  .fab-add{background:linear-gradient(var(--a3),var(--a0) 55%),
      color-mix(in srgb, var(--accent) 64%, transparent);
    backdrop-filter:blur(14px) saturate(1.5);-webkit-backdrop-filter:blur(14px) saturate(1.5);
    border-color:color-mix(in srgb, var(--accent) 72%, transparent);color:var(--on-solid)}
  .fab-add .fabicon{font-size:var(--t-xl)}
  /* Find New Roles reads as the machine's own verb — hot, unmistakable */
  .fab-scout{background:linear-gradient(var(--a3),var(--a0) 55%),
      color-mix(in srgb, var(--hot) 64%, transparent);
    backdrop-filter:blur(14px) saturate(1.5);-webkit-backdrop-filter:blur(14px) saturate(1.5);
    border-color:color-mix(in srgb, var(--hot) 72%, transparent);color:var(--bg)}
  .fab-scout[disabled]{opacity:.6;cursor:wait}
  @media (prefers-reduced-motion:reduce){.fab{transition:none}}
  .kbhelp.show~#kbhint,.kbhint.hide{opacity:0;transform:scale(.6);pointer-events:none}
  .kbhelp{position:fixed;right:18px;bottom:18px;z-index:calc(var(--z-fab) + 10);
    background:color-mix(in srgb, var(--hdr-bg) 80%, transparent);
    backdrop-filter:blur(12px) saturate(1.2);
    -webkit-backdrop-filter:blur(12px) saturate(1.2);
    color:var(--hdr-ink);border:1px solid var(--line);
    border-radius:var(--r-md);padding:16px 20px;box-shadow:var(--sh-modal);
    opacity:0;visibility:hidden;transform:translateY(6px);
    transition:opacity var(--dur-2) ease,visibility var(--dur-2),transform var(--dur-2) ease}
  .kbhelp.show{opacity:1;visibility:visible;transform:none}
  .kbtitle2{font-family:var(--serif);font-weight:700;font-size:var(--t-md);margin-bottom:10px}
  .kbgrid{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:6px 12px;
    font-size:var(--t-sm);align-items:center}
  .kbhelp kbd{font:inherit;font-size:var(--t-xs);border:1px solid var(--a4);
    border-radius:var(--r-sm);padding:2px 8px;text-align:center;min-width:24px;
    display:inline-block}
  .kbhelp .co{margin-top:10px;font-size:var(--t-xs)}
  #cardmodal{align-items:center}
  @keyframes popout{to{opacity:0;transform:translateY(22px) scale(.95)}}
  @keyframes fadebg{to{opacity:0}}
  .modal.closing{pointer-events:none;animation:fadebg var(--dur-1) ease forwards}
  .modal.closing .cardbox,.modal.closing .mbox{animation:popout var(--dur-1) ease forwards}
  @keyframes popin{from{opacity:0;transform:translateY(30px) scale(.94)}
    to{opacity:1;transform:none}}
  .cardbox{position:relative;width:min(1000px,96vw);max-height:90vh;overflow-y:auto;
    margin:0 auto;animation:popin var(--dur-2) var(--ease-spring)}
  .cardbox .ccard,.cardbox .pcard{cursor:default}
  /* The host card sits steady; nested cards keep their hover shadow. */
  #cardhost>.ccard:hover,#cardhost>.pcard:hover{box-shadow:var(--sh-modal)}
  #cardhost>.ccard,#cardhost>.pcard{border:3px solid var(--line2);
    box-shadow:var(--sh-modal)}
  #cardhost .exp .pcard{background:var(--card) !important}
  .exp .cgrid .ccard,.exp .pgrid .pcard,.exp .pplbox .pcard{border:1.5px solid var(--line2)}
  #cardhost>.ccard{padding:20px 24px 16px}
  #cardhost>.pcard{padding:20px 24px 16px 58px;height:auto}
  /* Sticky so it survives the box's own scroll; pulled down into the card's
     top-right corner so it doesn't add a header band of its own. */
  #triagebar{position:sticky;top:0;z-index:7;display:flex;justify-content:space-between;
    align-items:center;gap:10px;background:var(--accent);color:var(--on-solid);
    padding:8px 14px;font-family:var(--sans);font-size:var(--t-sm);border-radius:var(--r-md) var(--r-md) 0 0}
  #triagebar .tmode{font-weight:800;letter-spacing:.8px}
  #triagebar .tkeys{color:var(--a6)}
  #triagebar kbd{border:1px solid var(--a5);border-radius:var(--r-sm);
    padding:0 6px;font-family:var(--sans)}
  .cardbox .mclose{position:sticky;top:20px;float:right;z-index:6;
    margin:20px 20px -52px 0;width:32px;height:32px;padding:0;
    display:flex;align-items:center;justify-content:center;border-radius:50%}
  #cardhost .cname{font-size:var(--t-xl)}
  #cardhost>.ccard>.chead .ptitle{font-size:var(--t-xl);line-height:30px}
  .pcobig{font-family:var(--serif);font-size:var(--t-xl);font-weight:700;font-style:italic;
    padding:0;border:0;background:none;cursor:pointer;letter-spacing:.2px;
    color:hsl(var(--indh,96),42%,30%)}
  :root:not([data-theme="light"]) .pcobig{color:hsl(var(--indh,96),45%,72%)}
  .pcobig:hover{text-decoration:underline}
  .pcosm{font-size:var(--t-lg)}
  #list .ccard:not(.open) .tsep{font-size:var(--t-lg)}
  .cname{color:hsl(var(--indh,96),48%,20%)}
  .ccard.dull .cname{color:var(--muted)}
  :root:not([data-theme="light"]) .cname{color:hsl(var(--indh,96),45%,72%)}
  :root:not([data-theme="light"]) .ccard.dull .cname{color:var(--muted)}
  /* room for the floating ✕ so it never covers the added/posted tag */
  #cardhost>.ccard>.chead{padding-right:44px}
  #cardhost>.ccard>.chead .cscore{width:30px;height:30px;font-size:var(--t-sm)}
  #cardhost .cname{line-height:30px}
  #cardhost .exp h4{font-size:var(--t-sm)}
  .btn.editbtn{color:var(--on-solid);font-weight:600;
    transition:box-shadow var(--dur-1) ease,filter var(--dur-1) ease,transform var(--dur-1) ease}
  .btn.editbtn:hover{box-shadow:var(--sh-raised);filter:brightness(1.1)}
  #cardhost .chead{margin-bottom:6px;align-items:center;justify-content:center}
  #cardhost .cfoot{justify-content:center}
  #cardhost .exp>.pplbox:first-child{margin-top:2px}
  .addedtag{margin-left:auto;font-size:var(--t-xs);color:var(--ink);opacity:.75;
    font-weight:600;white-space:nowrap}
  /* One vertical beat: head → description → tags → rule → section header →
     cards → action row, all 14px apart. */
  #cardhost .why{margin:0 0 14px}
  #cardhost .cfoot{padding-top:0;margin:0}
  #cardhost .exp{margin-top:8px;padding-top:10px}
  #cardhost .exp>h4:first-child{margin-top:4px}
  #cardhost .exp>.why:first-child{margin-top:0;margin-bottom:10px}
  #cardhost .exp h4{margin:24px 0 8px;text-align:center}
  #cardhost .exp .pplbox h4,#cardhost .exp .twocol h4,#cardhost .exp .pstack h4{margin-top:0}
  #cardhost .exp .twocol h4{margin-top:0}
  #cardhost .exprow{margin-top:14px}
  #diagmodal .mbox{width:min(780px,94vw)}
  #queuemodal .mbox{width:min(680px,94vw)}
  #aboutmodal .mbox{width:min(720px,94vw);max-height:88vh;overflow:auto;
    background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
    padding:20px 24px}
  .aboutbody{font-size:var(--t-md);line-height:1.6}
  .aboutbody h4{margin:14px 0 6px;font-family:var(--sans);text-transform:uppercase;
    font-size:var(--t-xs);letter-spacing:.6px;color:var(--muted)}
  .aboutbody ul{margin:0;padding-left:20px}
  .aboutbody li{margin:4px 0}
  #queuemodal .mbox{max-height:90vh}
  #queuemodal{padding-top:16px;padding-bottom:16px}
  #actmodal .mbox,#diagmodal .mbox,#queuemodal .mbox{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
    max-width:640px;width:92%;max-height:76vh;margin:8vh auto;display:flex;flex-direction:column;
    box-shadow:var(--sh-modal)}
  #actmodal .mhead,#diagmodal .mhead,#queuemodal .mhead{display:flex;align-items:center;justify-content:space-between;
    padding:12px 16px;border-bottom:1px solid var(--line)}
  #actmodal .mhead h2,#diagmodal .mhead h2,#queuemodal .mhead h2{margin:0;font-size:var(--t-md);font-family:var(--serif)}
  .actlist{overflow-y:auto;padding:10px 16px 16px}
  .actday{font-family:var(--serif);font-weight:700;font-size:var(--t-sm);
    text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin:12px 0 6px}
  .actrow{display:grid;grid-template-columns:44px 1fr auto;gap:10px;
    font-size:var(--t-sm);padding:4px 0;align-items:baseline}
  #diag-list{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding-top:4px}
  .dsec{background:var(--bg);border:1px solid var(--line);border-radius:var(--r-md);
    padding:12px 14px}
  .dsec.dwide{grid-column:1/-1}
  .dsec h4{margin:0 0 8px;font-size:var(--t-xs);text-transform:uppercase;letter-spacing:.8px;
    color:var(--accent);font-weight:800;border-bottom:1px solid var(--line);padding-bottom:6px;
    font-family:var(--sans)}
  .dsec h4 .co{text-transform:none;letter-spacing:0;font-weight:400}
  .dtiles{display:flex;gap:8px;flex-wrap:wrap;margin:2px 0 6px}
  .dtile{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
    padding:6px 12px;text-align:center;min-width:64px}
  .dtile.zero{opacity:.5}
  .dtnum{font-family:var(--serif);font-weight:700;font-size:var(--t-lg);
    font-variant-numeric:tabular-nums}
  .dtlabel{font-family:var(--sans);font-size:var(--t-xs);color:var(--muted)}
  .dnote{font-family:var(--sans);font-size:var(--t-xs);color:var(--muted);margin-top:6px;
    line-height:1.5}
  .dchips{display:flex;gap:6px;flex-wrap:wrap}
  .srow{grid-template-columns:minmax(140px,1fr) 64px 84px 76px}
  @media (max-width:760px){#diag-list{grid-template-columns:1fr}}
  .drow{display:grid;grid-template-columns:minmax(120px,1fr) 52px 50px 36px 38px 68px;gap:4px;
    padding:4px 2px;font-family:var(--sans);font-size:var(--t-xs);align-items:center}
  .drow>span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .drow.dhead{color:var(--muted);text-transform:uppercase;font-size:var(--t-xs);letter-spacing:.5px}
  .drow.doff{opacity:.5}
  .qsec{background:var(--card);border:1px solid var(--line2);border-radius:var(--r-md);
    padding:10px 16px 8px;margin-bottom:12px}
  .qsec .actday{margin-top:0}
  .qrow{grid-template-columns:1fr auto;padding:8px 2px;align-items:center}
  .qrow .cchip{margin-right:10px;min-width:92px;text-align:center;height:auto;
    padding:4px 12px;line-height:1.5;display:inline-block;white-space:nowrap}
  .qrow+.qrow{border-top:1px solid var(--line)}
  .qcenter{display:flex;justify-content:center;padding:8px 0 2px}
  .qcenter .kbpager{margin-left:0}   /* kanban's auto-margin would shove it right */
  .doneck{color:var(--hot);font-weight:700;margin-right:6px}
  .qfinds{color:var(--hot)}
  .dwhy{color:var(--muted);font-style:italic;font-size:var(--t-xs);padding:0 2px 6px 16px}
  .ddot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px}
  .d-on{background:var(--hot)} .d-off{background:var(--warn)} .d-alt{background:hsl(var(--goldh),62%,50%)}
  .dfacts{display:flex;flex-direction:column;gap:4px;padding:2px;font-family:var(--sans);
    font-size:var(--t-sm)}
  .searchbox{background:transparent;border:0;box-shadow:none;
    width:min(560px,92vw);padding:0;text-align:center}
  .searchbox input{width:100%;font-size:var(--t-lg);padding:10px 16px}
  .qebox{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
    width:min(380px,92vw);padding:16px 16px}
  .qebox .mhead{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
  .qebox .mhead h2{margin:0;font-size:var(--t-md);font-family:var(--serif)}
  .qebox input,.qebox select{width:100%}
  .qedit{cursor:pointer;font:inherit;font-family:var(--sans);font-size:var(--t-xs)}
  @keyframes sheen{0%{background-position:180% 0}22%,100%{background-position:-80% 0}}
  .autobtn,.fab-scout,.tile.goal-hit{position:relative;overflow:hidden}
  .autobtn::after,.fab-scout::after,.tile.goal-hit::after{content:"";
    position:absolute;inset:0;pointer-events:none;border-radius:inherit;
    background:linear-gradient(115deg,transparent 42%,var(--a4) 50%,transparent 58%);
    background-size:240% 100%;background-repeat:no-repeat;
    animation:sheen 8s ease-in-out infinite}
  @media (prefers-reduced-motion:reduce){
    .autobtn::after,.fab-scout::after,.tile.goal-hit::after{animation:none}}
  .autobtn{background:linear-gradient(135deg,
      hsla(var(--indh,96),40%,48%,.18),
      hsla(calc(var(--indh,96) + 60),36%,48%,.14),
      hsla(calc(var(--indh,96) + 120),32%,48%,.18)),var(--card);
    color:hsl(var(--indh,96),45%,26%);
    border:1px solid hsla(var(--indh,96),35%,40%,.5);font-weight:600}
  :root:not([data-theme="light"]) .autobtn{color:hsl(var(--indh,96),45%,72%)}
  .autobtn:hover{filter:brightness(1.05);border-color:hsl(var(--indh,96),40%,40%)}
  .qedit:hover{border-color:var(--accent);color:var(--accent)}
  .dpre{white-space:pre-wrap;font-family:var(--sans);font-size:var(--t-sm);line-height:1.55;
    background:none;border:0;border-left:3px solid var(--line2);border-radius:0;
    padding:2px 0 2px 12px;margin:4px 0 0;max-height:320px;overflow-y:auto}
  .act-fwd{color:var(--hot)}
  .act-neg{color:var(--warn)}
  .acttime{color:var(--muted);font-variant-numeric:tabular-nums;font-size:var(--t-xs)}
  .bulkbar{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:var(--z-fab);
    display:flex;gap:10px;align-items:center;background:var(--hdr-bg);color:var(--hdr-ink);
    border:1px solid var(--line);border-radius:var(--r-pill);padding:8px 16px;
    box-shadow:var(--sh-overlay);animation:fadeup var(--dur-1) ease}
  .bulkbar .btn{background:transparent;color:var(--hdr-ink);border-color:var(--a4)}
  .pexp{margin-top:8px;border-top:1px solid var(--line);padding-top:8px;
    display:grid;grid-template-rows:1fr}
  .pexpin{overflow:hidden;min-height:0;padding-bottom:44px;
    display:flex;flex-direction:column;gap:4px;font-size:var(--t-sm)}
  .pexp .prow{display:flex;gap:8px;align-items:baseline}
  .pexp .plab{color:var(--muted);font-size:var(--t-xs);min-width:64px;flex:none;
    font-family:var(--serif);letter-spacing:.3px}
  .pexp a{color:var(--accent);word-break:break-all}
  /* Edit's left edge = the tag text's left edge (the card's text column) */
  .pexp .pexprow{position:absolute;left:50px;bottom:12px;display:flex;gap:8px}
  .pexp .pexprow .btn{position:static;margin:0}
  .pcard.open .triage{opacity:1}
  @keyframes growrow{from{grid-template-rows:0fr;opacity:0;margin-top:0;padding-top:0}
    to{grid-template-rows:1fr;opacity:1}}
  .pexp.anim{animation:growrow var(--dur-2) var(--ease-spring)}
  /* The grid-grow trick only exists WHILE animating — at rest the wrapper
     is a plain block, because inside the card's flex column a fr track
     resolved to 0 and swallowed the expansion. animationend strips .anim. */
  .expgrow{display:block;width:100%}
  .expgrow.anim{display:grid;grid-template-rows:1fr;
    animation:growrow var(--dur-2) var(--ease-spring)}
  .expgrow.anim>.expgrowin{overflow:hidden;min-height:0}
  .rel-founder{color:var(--founder-edge);font-weight:600}
  .rel-employee{color:var(--hot);font-weight:600}
  .rel-contact{color:var(--muted);font-weight:600}
  .o-warm{color:var(--warmlead-edge);font-weight:600}
  .o-cold{color:var(--nmh-edge);font-weight:600}
  .alum-emory{color:var(--emory-edge);font-weight:600}
  .alum-nmh{color:var(--nmh-edge);font-weight:600}
  .icn{display:inline-flex;align-items:center;justify-content:center;
    width:32px;height:32px;border-radius:50%;border:1px solid var(--line);
    color:var(--accent);text-decoration:none;font-size:var(--t-sm);font-weight:700;
    margin-right:4px;background:var(--card);font-family:var(--serif)}
  .icn .big{font-size:var(--t-md);line-height:1}
  .icn.off{color:var(--line);opacity:.55;cursor:default}
  .icn.addc{border-style:dashed;color:var(--muted);background:none;cursor:pointer}
  .icn.addc:hover{color:var(--accent);border-color:var(--accent)}
  .icn:hover{border-color:var(--accent)}

  /* modals */
  .modal{position:fixed;inset:0;background:var(--b4);display:none;
    backdrop-filter:blur(9px) saturate(1.15);
    -webkit-backdrop-filter:blur(9px) saturate(1.15);
    align-items:flex-start;justify-content:center;padding:40px;z-index:var(--z-modal)}
  .modal.show{display:flex}
  .modal pre{background:var(--bg);color:var(--ink);border:1px solid var(--line);
    border-radius:var(--r-sm);padding:14px;max-height:70vh;overflow:auto;
    font-size:var(--t-sm);line-height:1.6;white-space:pre;margin:0}
  .sheet{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
    padding:16px;max-width:460px;width:100%}
  .sheet h2{margin:0 0 10px;font-size:var(--t-lg);font-family:var(--serif)}
  .sheet .frow{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
  .sheet .frow.one{grid-template-columns:1fr}
  .sheet input,.sheet select{width:100%}
  .sheet .facts{display:flex;gap:8px;justify-content:flex-end;margin-top:10px}

  .toast{position:fixed;bottom:18px;right:18px;pointer-events:none;
    background:color-mix(in srgb, var(--accent) 84%, transparent);
    backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
    color:var(--on-solid);
    padding:10px 14px;border-radius:var(--r-md);font-size:var(--t-sm);opacity:0;
    transition:opacity var(--dur-2);z-index:var(--z-top)}
  .toast.show{opacity:1}
  footer{color:var(--muted);font-size:var(--t-xs);padding:0 20px 32px;text-align:center}
  code{background:var(--card);border:1px solid var(--line);padding:2px 6px;border-radius:var(--r-sm)}

  /* Motion: small, quick, and optional. */
  .kbcard,.pcard,.ccard,.card{transition:transform var(--dur-1) ease,box-shadow var(--dur-1) ease,
    border-color var(--dur-1) ease,background var(--dur-1) ease}
  :root:not([data-theme="light"]) .ccard:hover,
  :root:not([data-theme="light"]) .pcard:hover,
  :root:not([data-theme="light"]) .kbcard:hover{border-color:var(--line2);
    box-shadow:var(--sh-raised)}
  .kbcard:hover,.pcard:hover,.ccard:hover,.card:hover{
    box-shadow:var(--sh-raised)}
  .btn,.chip,.tab,.icn,.tri,.kbpg{transition:background var(--dur-1) ease,
    color var(--dur-1) ease,border-color var(--dur-1) ease,transform var(--dur-1) ease}
  .btn:active,.chip:active,.tri:active,.tab:active{transform:scale(.95)}
  @keyframes fadeup{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  .fadein{animation:panepop var(--dur-2) var(--ease-spring)}
  @keyframes panepop{from{opacity:0;transform:translateY(14px) scale(.988)}
    to{opacity:1;transform:none}}
  .modal>*{animation:fadeup var(--dur-1) ease}
  .card,.ccard{position:relative}
  .pcard{position:relative}
  .ccard.mini{padding:12px 14px 52px}
  /* In flow, pinned to the card floor by flex — the card grows with its
     description instead of clipping against a fixed footer. */
  /* row-gap 0 + per-chip margin-top: the balancer's zero-height break row
     would otherwise cost TWO row-gaps and double the space between lines */
  .cfoot{margin-top:auto;padding-top:4px;display:flex;
    column-gap:var(--gap-chip);row-gap:0;flex-wrap:wrap;align-items:center}
  .cfoot>*{margin-top:6px}
  .cchip{font-size:var(--t-xs);border:1px solid var(--line);border-radius:var(--r-pill);
    padding:2px 10px;white-space:nowrap;color:var(--muted);background:var(--chipfill);
    text-decoration:none;height:var(--chip-h);box-sizing:border-box;
    display:inline-flex;align-items:center}
  a.cchip{color:var(--accent)}
  /* Raise recency, three tiers (Eric 2026-08-09: recent = noticeable).
     <60d wears the hot fill — same urgency color as fresh roles; 60-120d a
     hot-toned outline; older fades to bare muted text. No transforms. */
  .raisedchip{font-size:var(--t-xs);border:1px solid var(--hot);border-radius:var(--r-pill);
    padding:2px 10px;white-space:nowrap;color:var(--hot);background:var(--card);
    text-decoration:none;font-weight:600;height:var(--chip-h);box-sizing:border-box;
    display:inline-flex;align-items:center;gap:4px}
  a.raisedchip:hover{text-decoration:underline}
  .raisedchip.hot{background:var(--hot);color:var(--card);font-weight:700}
  .raisedchip.old{border:none;background:none;padding:2px 4px;color:var(--muted);font-weight:400}
  /* Just-Raised notice board (Eric 2026-08-11): every watched company whose
     Form D is inside the 45d news window, one glance at the top of the
     tracker. Hover changes shadow only — transforms shimmer. */
  .rboard{display:flex;flex-wrap:wrap;gap:10px;margin:8px 0;justify-content:center;align-items:center}
  .rbcard{display:inline-flex;align-items:center;gap:10px;cursor:pointer;
    border:1px solid var(--hot);border-radius:var(--r-md);background:var(--card);
    padding:8px 14px;box-shadow:var(--sh-rest)}
  .rbcard:hover{box-shadow:var(--sh-raised)}
  .rbcard .rbname{font-weight:700}
  .rbcard.rbmore{border-style:dashed;color:var(--muted);cursor:pointer;
    font:inherit;font-family:var(--sans);font-size:var(--t-sm)}
  .inchip{font-family:var(--sans);font-weight:800;letter-spacing:0}
  /* centering, corrected: EXPANDED views and group headers center; the
     condensed grids stay left-aligned (Eric 2026-08-05, second pass) */
  .kbhead{justify-content:center;position:relative}
  .kbhead .kbpager{position:absolute;right:6px;top:0}
  .kbnone{text-align:center}
  #pane-tracker .connbar{text-align:center}
  a.cchip:hover{border-color:var(--accent)}
  .citychip{font-weight:700}
  .rolechip{color:var(--on-solid) !important}
  .ind-missing,.cchip.missing{color:var(--muted);background:var(--card);
    border:1px dashed var(--line);font-weight:400}
  :root:not([data-theme="light"]) .ind-missing,
  :root:not([data-theme="light"]) .cchip.missing{color:var(--miss-ink);border-color:color-mix(in srgb, var(--miss-ink) 60%, transparent)}
  .cchip.freshroles{background:var(--hot);border-color:var(--hot);color:var(--on-solid);font-weight:600}
  .freshpost{border-color:var(--hot) !important}
  .freshpost::before{content:"";position:absolute;inset:-2px;border-radius:inherit;
    pointer-events:none;box-shadow:0 0 0 4px color-mix(in srgb, var(--hot) 22%, transparent);opacity:0;
    will-change:opacity;animation:glowfade 7.5s ease-in-out infinite}
  .exp .cgrid .freshpost::before{inset:0;box-shadow:inset 0 0 0 4px color-mix(in srgb, var(--hot) 22%, transparent)}
  @keyframes glowfade{0%,30%,100%{opacity:0}15%{opacity:1}}
  @media (prefers-reduced-motion:reduce){.freshpost::before{animation:none}}
  @media (prefers-reduced-motion:reduce){.freshpost{animation:none}}
  .salchip{color:var(--hot);font-weight:600}
  /* Experience-requirement chip: green light at <=1yr, plain 2-3, warn 4+ */
  .yoechip.yoe-ok{color:var(--hot);border-color:var(--hot)}
  .yoechip.yoe-high{color:var(--warn);border-color:var(--warn)}
  /* Triage sits IN the footer row (2026-08-12): the old floating ✓/✕ needed
     a 48px bottom band plus an in-row spacer — double reservation, ~25% of
     every card's height measured dead. Open cards keep the corner float. */
  .card.open,.ccard.open{padding-bottom:14px}
  /* The expanded view's actions sit in one bottom-left row, level with ✓/✕ */
  .exprow{display:flex;gap:8px;flex-wrap:wrap;align-items:center;
    margin-top:14px;padding-right:84px;min-height:32px}
  .exprow .whybox{flex-basis:100%;order:9}
  .histbtn{margin-left:auto;position:relative}
  .histbtn summary{list-style:none;cursor:pointer;font:inherit;font-family:var(--sans);
    font-size:var(--t-sm);border:1px solid var(--line);border-radius:var(--r-pill);
    padding:6px 14px;background:var(--card)}
  .histbtn[open] summary{background:var(--accent);color:var(--on-solid);border-color:var(--accent)}
  .histbtn .histpop{position:absolute;right:0;top:calc(100% + 6px);z-index:5;
    background:var(--card);border:1px solid var(--line2);border-radius:var(--r-md);
    box-shadow:var(--sh-overlay);padding:10px 12px;min-width:320px;max-height:220px;overflow-y:auto}
  .triage{position:absolute;right:9px;bottom:8px;display:flex;gap:4px;
    opacity:0;transition:opacity var(--dur-1) ease}
  .cfoot>.triage{position:static;margin-left:auto;margin-top:4px}
  .pline2>.triage{position:static;margin-left:auto;flex:none}
  .scorefil{display:inline-flex;align-items:center;gap:6px;height:28px;
    padding:0 10px;border:1px solid var(--line);border-radius:var(--r-pill);
    background:var(--card);color:var(--muted);
    font-size:var(--t-sm);font-family:var(--sans)}
  .ddwrap{position:relative;display:inline-flex}
  #controls .chip{min-height:28px;padding:4px 10px}
  #controls select{min-height:28px}
  #controls input[type=search]{min-height:28px}
  .modebar .chip{min-height:26px}
  .ddpanel{position:absolute;top:calc(100% + 4px);left:0;z-index:var(--z-fab);
    background:var(--card);border:1px solid var(--line2);border-radius:var(--r-md);
    box-shadow:var(--sh-overlay);padding:8px;display:flex;flex-direction:column;
    gap:4px;min-width:150px}
  .ddrow{display:flex;align-items:center;gap:8px;font-family:var(--sans);
    font-size:var(--t-sm);color:var(--ink);cursor:pointer;padding:2px 4px;
    white-space:nowrap}
  .ddrow input[type=checkbox]{accent-color:var(--accent);margin:0}
  .chip.actv{background:var(--accent);border-color:var(--accent);color:var(--on-solid)}
  /* the GLOBAL input rule pills every input (border+card bg+32px) — on a
     range that drew a second capsule around the custom track (Eric's
     screenshot, 2026-08-18) */
  .scorefil input[type=range]{width:88px;margin:0;flex:none;height:16px;
    -webkit-appearance:none;appearance:none;background:transparent;
    border:0;padding:0;min-height:0}
  .scorefil input[type=range]::-webkit-slider-runnable-track{height:4px;
    border-radius:var(--r-pill);background:var(--line2)}
  .scorefil input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;
    width:12px;height:12px;border-radius:50%;background:var(--accent);
    border:0;margin-top:-4px}
  #minscore-lbl{min-width:34px;flex:none;text-align:center;font-variant-numeric:tabular-nums}
  .card:hover .triage,.ccard:hover .triage,.pcard:hover .triage,
  .triage:focus-within,.triage.decided{opacity:1}
  @media (hover:none){.triage{opacity:1}}
  /* The decision about the OPENED card is always on the table; nested cards
     keep their circles until hovered. */
  #cardhost>.ccard>.triage,#cardhost>.pcard>.triage{opacity:1}
  .tri{width:28px;height:28px;border-radius:50%;border:1px solid var(--line);
    background:var(--card);cursor:pointer;font:inherit;font-family:var(--sans);font-size:var(--t-sm);
    display:inline-flex;align-items:center;justify-content:center;color:var(--muted)}
  .tri.ok:hover,.tri.ok.on{background:var(--accent);border-color:var(--accent);color:var(--on-solid)}
  .tri.no:hover,.tri.no.on{background:var(--kill);border-color:var(--kill);color:var(--on-solid)}
  .preveal{opacity:0}
  .preveal.vis{opacity:1;transition:opacity var(--dur-3) ease}
  .kbcard[draggable]{cursor:grab}
  .kbcard.dragging{opacity:.45;transform:rotate(1.5deg) scale(.98);cursor:grabbing}
  .kbcol{transition:border-color var(--dur-1) ease,background var(--dur-1) ease}
  /* Dormant is where threads flicker out — dashed edge, faint signal glitch. */
  .kbcol[data-st="saved"] .kbcard{background:linear-gradient(hsla(var(--mossh),30%,48%,.08),hsla(var(--mossh),30%,48%,.08)),var(--card)}
  .kbcol[data-st="applied"] .kbcard,.kbcol[data-st="contacted"] .kbcard{background:linear-gradient(hsla(var(--goldh),55%,50%,.09),hsla(var(--goldh),55%,50%,.09)),var(--card)}
  .kbcol[data-st="interviewing"] .kbcard,.kbcol[data-st="conversation"] .kbcard{background:linear-gradient(hsla(var(--emberh),60%,50%,.10),hsla(var(--emberh),60%,50%,.10)),var(--card)}
  .kbcol[data-st="offer"] .kbcard,.kbcol[data-st="met"] .kbcard{background:linear-gradient(hsla(var(--minth),40%,42%,.12),hsla(var(--minth),40%,42%,.12)),var(--card)}
  .kbcol[data-st="rejected"] .kbcard{background:linear-gradient(hsla(var(--redh),45%,45%,.07),hsla(var(--redh),45%,45%,.07)),var(--card)}
  /* The board reads as a journey: moss → gold → amber → deep green. */
  .kbcol[data-st="saved"]{background:linear-gradient(hsla(var(--mossh),30%,48%,.10),hsla(var(--mossh),30%,48%,.10)),var(--bg);border-color:hsla(var(--mossh),30%,40%,.5)}
  .kbcol[data-st="applied"],.kbcol[data-st="contacted"]{background:linear-gradient(hsla(var(--goldh),55%,50%,.10),hsla(var(--goldh),55%,50%,.10)),var(--bg);border-color:hsla(var(--goldh),55%,42%,.5)}
  .kbcol[data-st="interviewing"],.kbcol[data-st="conversation"]{background:linear-gradient(hsla(var(--emberh),60%,50%,.12),hsla(var(--emberh),60%,50%,.12)),var(--bg);border-color:hsla(var(--emberh),60%,42%,.55)}
  .kbcol[data-st="offer"],.kbcol[data-st="met"]{background:linear-gradient(hsla(var(--minth),40%,42%,.14),hsla(var(--minth),40%,42%,.14)),var(--bg);border-color:var(--hot)}
  .kbcol[data-st="rejected"]{background:linear-gradient(hsla(var(--redh),45%,45%,.08),hsla(var(--redh),45%,45%,.08)),var(--bg);border-color:hsla(var(--redh),45%,45%,.45)}
  .kbcol[data-st="dormant"]{border-style:dashed;animation:glitch 5s steps(1) infinite}
  .kbcol[data-st="dormant"] .kbhead{text-shadow:1px 0 hsla(var(--goldh),54%,38%,.55),-1px 0 hsla(var(--skyh),47%,37%,.45)}
  .kbcol[data-st="dormant"] .kbcard{border-style:dashed;opacity:.82;
    filter:saturate(.55) contrast(.93);transform:rotate(-.5deg)}
  .kbcol[data-st="dormant"] .kbcard:nth-child(even){transform:rotate(.6deg) translateX(1px)}
  .kbcol[data-st="dormant"] .kbcard:hover{opacity:1;filter:none;transform:none}
  @keyframes glitch{
    0%,93%{transform:none;opacity:1}
    94%{transform:translateX(1px) skewX(.5deg);opacity:.82}
    96%{transform:translateX(-1px);opacity:.95}
    98%{transform:translateX(.5px) skewX(-.4deg);opacity:.85}
    100%{transform:none;opacity:1}
  }
  .kbcol.dragover{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{animation:none !important;transition:none !important}
  }

  /* ---- Tweets: the X sweep's ledger, read in context ---------------------
     The stored text is the blockquote's OWN content, so the card reads with
     no network: widgets.js swaps in the real embed and marks the blockquote
     .twitter-tweet-rendered, which is why only the UNrendered one carries
     the fallback frame — otherwise the loaded embed sits inside a ghost of
     its own fallback. */
  .twcard{cursor:default;padding-bottom:12px;gap:6px}
  .twcard .twitter-tweet:not(.twitter-tweet-rendered){
    margin:0;border:1px solid var(--line);border-left:3px solid var(--accent);
    border-radius:var(--r-sm);padding:10px 12px;
    background:hsla(var(--mossh),26%,50%,.07)}
  .twcard .twitter-tweet:not(.twitter-tweet-rendered) p{
    margin:0 0 8px;white-space:pre-wrap;font-family:var(--serif);
    font-size:var(--t-md);line-height:1.55;color:var(--ink)}
  .twcard .twby{font-family:var(--sans);font-size:var(--t-xs);color:var(--muted)}
  .twcard .twby a{color:var(--accent)}
  .twcard .twitter-tweet-rendered{margin:0 !important}
  /* X's embed follows the OS colour scheme, not the theme= param it is
     handed: on a dark-mode Mac the light page rendered dark embeds.
     Propagating color-scheme into the iframe is what actually flips it,
     and it lands because a theme flip re-renders the whole tab. */
  :root[data-theme="light"] .twcard{color-scheme:light}
  :root[data-theme="dark"] .twcard{color-scheme:dark}
  .twcard iframe{max-width:100% !important}
  .twcard>.why{margin:2px 0 0;opacity:1;line-height:1.55;
    font-size:var(--t-sm)}
  .twcard>.cfoot{padding-top:6px;min-height:44px}
  .twcard a.btn{text-decoration:none;display:inline-flex;align-items:center;
    padding:4px 12px;min-height:28px;border-color:var(--accent);color:var(--accent)}
  .tcount{margin-left:6px;font-size:var(--t-xs);font-weight:800;font-family:var(--sans);
    background:var(--hot);color:var(--hdr-bg);border-radius:var(--r-pill);
    padding:0 6px;min-width:18px;line-height:18px;display:inline-block;
    text-align:center;vertical-align:1px}
  .tab.on .tcount{background:var(--a6);color:var(--hdr-bg)}
  :root[data-theme="light"] .tcount{color:var(--hdr-ink)}
  /* ink-on-fill follows the fill: the light-theme override above painted the
     ACTIVE tab's badge hdr-ink on a near-white fill — invisible (Eric,
     2026-08-12). The .on badge re-asserts its own pairing after it. */
  :root[data-theme="light"] .tab.on .tcount{color:var(--hdr-bg)}
  /* Demoted from injection-moulded plastic to the quiet wash (Eric,
     2026-08-11): the base .cogroup — wash, hairline, one top highlight —
     IS the rolodex surface now. The divider-tab structure stays. */

  /* Tracker cards ARE the Postings / Rolodex cards, so the board inherits
     their look; only the drag affordance is board-specific. */
  .kbcol>.ccard,.kbcol>.pcard{margin-bottom:8px}
  /* board cards read a size quieter — the column header carries the weight */
  .kbcol .ptitle{font-size:var(--t-md)}
  .kbcol .ptitle .pcosm{font-size:var(--t-md)}
  .kbcol>.ccard:last-child,.kbcol>.pcard:last-child{margin-bottom:0}
  .kbdrag[draggable]{cursor:grab}
  .kbdrag.dragging{opacity:.45;transform:rotate(1.5deg) scale(.98);cursor:grabbing}
  .kbcol[data-st="dormant"]>.kbdrag{border-style:dashed;opacity:.84}
  /* an offer is the whole game — the card goes loud green (Eric, 2026-08-18) */
  .ccard.offercard{background:linear-gradient(hsla(var(--minth),52%,45%,.42),
    hsla(var(--minth),52%,45%,.42)),var(--card) !important;
    border-color:var(--hot) !important}
  /* visited cards read visited in the TRIAGE piles only — on the tracker
     every worked card has been opened, so muting there would gray the
     whole board (pipeline handoff, 2026-08-18) */
  #list .ccard.seen:not(.open):not(.sel),
  #companies .ccard.seen:not(.open):not(.sel){opacity:.58}
  #list .ccard.seen:not(.open):hover,
  #companies .ccard.seen:not(.open):hover{opacity:1}
  .kbcol[data-st="dormant"]>.kbdrag:hover{opacity:1}
  /* the people count: glyph + number, one look on cards and headers alike */
  .pplchip{display:inline-flex;align-items:center;gap:4px}
  .pplbare{display:inline-flex;align-items:center;gap:4px;color:var(--muted);
    font-family:var(--sans);font-size:var(--t-sm);font-weight:400}
  .pplsvg{width:11px;height:11px;fill:currentColor;flex:none;opacity:.9}
  .pplbare .pplsvg{width:12px;height:12px}
  .pplnum{font-variant-numeric:tabular-nums}
  /* one dashed pill for everything not on file yet — four borders became one */
  .misspill{display:inline-flex;align-items:center;gap:2px;height:var(--chip-h);
    box-sizing:border-box;
    border:1px dashed var(--line2);border-radius:var(--r-pill);padding:0 6px;
    font-family:var(--sans);font-size:var(--t-xs);color:var(--muted)}
  .cchip.agechip,.cchip.addedchip{display:inline-flex;align-items:center;gap:4px}
  .intel{font-family:var(--sans);font-size:var(--t-xs);color:var(--muted);
    margin-top:4px}
  .chip.kzero{display:none !important}
  .cchip.atspain{color:hsl(var(--clayh),55%,38%);background:hsla(var(--clayh),55%,45%,.14)}
  :root:not([data-theme="light"]) .cchip.atspain{color:hsl(var(--clayh),65%,68%)}
  .wrd{display:none}
  .open .wrd{display:inline}
  .open .gly{display:none}
  .agesvg{width:11px;height:11px;fill:currentColor;flex:none;opacity:.85}
  .agevia{opacity:.8;font-weight:400}
  .roletag{display:inline-flex;align-items:center;gap:4px}
  .jobsvg{width:11px;height:11px;fill:currentColor;flex:none;opacity:.9}
  /* the +N rides INSIDE the lit-up freshroles chip, so it must inherit that
     chip's ink — var(--hot) here was green-on-green and simply invisible.
     A hairline keeps "1 +1" from reading as eleven. */
  /* --hot flips to a LIGHT green in dark mode, so the #fff ink the freshroles
     chip has always carried was white-on-mint at about 1.7:1 — unreadable, and
     newly load-bearing now that the chip is an icon and two numbers. The ink
     follows the fill, not the theme's name. */
  :root:not([data-theme="light"]) .cchip.freshroles,
  :root[data-theme="dark"] .cchip.freshroles{color:var(--hdr-bg)}
  .rnew{font-weight:800;display:inline-flex;align-items:center;gap:4px}
  .rnew::before{content:"";width:1px;height:11px;background:currentColor;opacity:.45}
  /* DESIGN.md decision B (Eric, 2026-08-09): data chips are tinted fills —
     a border now means "control" (links, dashed missing-field pills, filter
     bar). Quick-edit chips reveal their edge on hover instead of wearing it. */
  .cchip:not(.missing),.ind:not(.ind-missing),.sig{border-color:transparent}
  /* a raise is a timing trigger, not a biography: condensed cards show it
     only while it is fresh (<60d); expanded cards keep the history */
  .ccard:not(.open) .raisedchip:not(.hot):not(.fresh){display:none}
  /* DESIGN.md: empty states share one grammar — dashed hairline, muted label.
     State color lives on populated columns; a stub is a parking spot. */
  .kbcol.kbempty,.kbcol.kbstub{background:var(--bg);border:1px dashed var(--line);animation:none}
  /* city tones were tuned for borders on light; as text-on-wash in the dark
     theme they sank into the card — lift them, one rule, both fill and ink */
  :root:not([data-theme="light"]) .cchip.citychip{filter:brightness(1.75)}
  /* 95% of rows wore the scrape-date pill — a chip everyone wears is
     decoration (design audit). It reads as quiet text now. */
  .pcard .cchip.addedchip{background:transparent;padding:0 2px}
  a.cchip{border-color:var(--line)}
  .cchip.qedit:hover,.ind.qedit:hover{border-color:var(--line2)}
</style></head><body>
<a class="skip" href="#main">Skip to content</a>
<header>
  <div class="hband">
  <div class="hrow">

    <div class="hlogo" aria-hidden="true"><span class="hword">SteinJobs</span></div>
    <nav class="tabs" role="tablist">
      <button type="button" class="tab on" data-tab="tracker" role="tab" aria-selected="true">Tracker</button>
      <button type="button" class="tab" data-tab="postings" role="tab" aria-selected="false">Postings<span class="tcount" id="pon" hidden></span></button>
      <button type="button" class="tab" data-tab="tweets" role="tab" aria-selected="false">Tweets<span class="tcount" id="twn" hidden></span></button>
      <button type="button" class="tab" data-tab="companies" role="tab" aria-selected="false">Companies</button>
      <button type="button" class="tab" data-tab="people" role="tab" aria-selected="false">Rolodex</button>
      <span id="tabslide" aria-hidden="true"></span>
    </nav>
    <div class="hspace"></div>
    <div class="hbtns">
      <button id="themebtn" class="btn" aria-label="Switch theme"
        title="theme: follows your system unless you pick one">◐</button>
      <button id="refresh" class="btn" hidden style="display:none">Find New Roles</button>
      <button id="about-btn" class="btn">About</button>
      <button id="queue-btn" class="btn">Queue</button>
      <button id="activity-btn" class="btn">Activity</button>
      <button id="diag-btn" class="btn">Insights</button>

    </div>
  </div>
  </div>
  <div class="controls" id="controls">
    <input aria-label="Search" type="search" id="q" placeholder="search…" hidden>
    <span class="modebar" id="ps-bar"><span class="modeslide" aria-hidden="true"></span><button type="button" class="chip on" id="ps-review" aria-pressed="true">Review</button>
    <button type="button" class="chip" id="ps-saved" aria-pressed="false">Saved</button>
    <button type="button" class="chip" id="ps-uninterested" aria-pressed="false">Uninterested</button></span>
    <span class="sep" aria-hidden="true" id="pssep"></span>
    <span class="scorefil" id="scorefil"><span id="minscore-lbl">Score</span>
      <input type="range" id="minscore" min="0" max="90" step="5" value="0"
        autocomplete="off" aria-label="Minimum score"></span>
    <select aria-label="Sort postings" id="sort">
      <option value="score">Sort: Score</option>
      <option value="posted">Sort: Newest</option>
      <option value="touch">Sort: Recently Touched</option>
      <option value="company">Sort: Company</option>
    </select>
    <span class="sep" aria-hidden="true" id="pssep2"></span>
    <select aria-label="Filter by date posted" id="posted">
      <option value="">Posted</option>
      <option value="7">Last 7 Days</option>
      <option value="14">Last 14 Days</option>
      <option value="30">Last 30 Days</option>
      <option value="60">Last 60 Days</option>
    </select>
    <span class="modebar" id="ct-bar"><span class="modeslide" aria-hidden="true"></span><button type="button" class="chip on" id="ct-review" aria-pressed="true">Review</button>
    <button type="button" class="chip" id="ct-saved" aria-pressed="false">Saved</button>
    <button type="button" class="chip" id="ct-uninterested" aria-pressed="false">Uninterested</button></span>
    <span class="sep" aria-hidden="true" id="csep1"></span>
    <select aria-label="Sort companies" id="csort">
      <option value="az">Sort: A–Z</option>
      <option value="recent">Sort: Recently Added</option>
    </select>
    <span class="sep" aria-hidden="true" id="csep2"></span>
    <span class="ddwrap" id="dd-ind"><button type="button" class="chip" id="dd-ind-btn" aria-haspopup="true" aria-expanded="false">Industries</button><div class="ddpanel" id="dd-ind-panel" hidden></div></span>
    <span class="ddwrap" id="dd-cat"><button type="button" class="chip" id="dd-cat-btn" aria-haspopup="true" aria-expanded="false">Job Types</button><div class="ddpanel" id="dd-cat-panel" hidden></div></span>
    <span class="ddwrap" id="dd-city"><button type="button" class="chip" id="dd-city-btn" aria-haspopup="true" aria-expanded="false">Cities</button><div class="ddpanel" id="dd-city-panel" hidden></div></span>
    <span class="ddwrap" id="dd-rnd"><button type="button" class="chip" id="dd-rnd-btn" aria-haspopup="true" aria-expanded="false">Rounds</button><div class="ddpanel" id="dd-rnd-panel" hidden></div></span>
    <span class="ddwrap" id="yqwrap">
      <button type="button" class="chip" id="yqbtn" aria-haspopup="true"
        aria-expanded="false">Experience</button>
      <div class="ddpanel" id="yqpanel" hidden>
        <label class="ddrow"><input type="checkbox" id="yq-le1"
          aria-label="≤1 year or none stated as required"><span id="yq-le1-lbl">≤1 yr</span></label>
        <label class="ddrow"><input type="checkbox" id="yq-mid"
          aria-label="2–3 years required"><span id="yq-mid-lbl">2–3 yrs</span></label>
        <label class="ddrow"><input type="checkbox" id="yq-hi"
          aria-label="4 or more years required"><span id="yq-hi-lbl">4+ yrs</span></label>
        <label class="ddrow"><input type="checkbox" id="yq-none"
          aria-label="No requirement stated"><span id="yq-none-lbl">No req</span></label>
      </div>
    </span>
    <span class="modebar" id="pt-bar"><span class="modeslide" aria-hidden="true"></span><button type="button" class="chip on" id="pt-review" aria-pressed="true">Review</button>
    <button type="button" class="chip" id="pt-saved" aria-pressed="false">Saved</button></span>
    <select aria-label="Filter saved people by talking stage" id="talkstage" hidden>
      <option value="">Talking Stage</option>
      <option value="saved">Saved</option>
      <option value="contacted">Contacted</option>
      <option value="conversation">Conversation</option>
      <option value="met">Met</option>
      <option value="dormant">Dormant</option>
    </select>
    <span class="sep" aria-hidden="true" id="psep"></span>
    <button type="button" class="chip on" id="pview-flat" aria-pressed="true">All Names</button>
    <button type="button" class="chip" id="pview-co" aria-pressed="false">By Company</button>
    <span class="sep" aria-hidden="true" id="psepv"></span>
    <select aria-label="Sort people" id="psort"
      title="Actionable = warm people first, then deepest talking stage, then most recently touched">
      <option value="act">Sort: Actionable</option>
      <option value="az">Sort: A–Z</option>
      <option value="recent">Sort: Recently Added</option>
    </select>
    <span class="sep" aria-hidden="true" id="psep2"></span>
    <span class="ddwrap" id="dd-src"><button type="button" class="chip" id="dd-src-btn" aria-haspopup="true" aria-expanded="false">Sources</button><div class="ddpanel" id="dd-src-panel" hidden></div></span>
    <span class="ddwrap" id="dd-pmiss"><button type="button" class="chip" id="dd-pmiss-btn" aria-haspopup="true" aria-expanded="false">Missing</button><div class="ddpanel" id="dd-pmiss-panel" hidden></div></span>
    <button type="button" class="chip on" id="tw-new" aria-pressed="true">New</button>
    <button type="button" class="chip" id="tw-saved" aria-pressed="false">Saved</button>
    <button type="button" class="chip" id="tw-dismissed" aria-pressed="false">Dismissed</button>
    <span class="sep" aria-hidden="true" id="twksep"></span>
    <button type="button" class="chip" id="twk-hiring" aria-pressed="false">Hiring</button>
    <button type="button" class="chip" id="twk-raise" aria-pressed="false">Raise</button>
    <button type="button" class="chip" id="twk-intro" aria-pressed="false">Intro</button>
    <button type="button" class="chip" id="twk-advice" aria-pressed="false">Advice</button>
    <button type="button" class="chip" id="twk-unset" aria-pressed="false"
      title="Collected before the sweep classified kinds — not yet categorized">Unsorted</button>
    <button type="button" class="chip" data-f="visa" aria-pressed="false">Sponsors Visa</button>
    <button type="button" class="chip" data-f="warm" aria-pressed="false">Warm</button>
    <button type="button" class="chip" data-f="family" aria-pressed="false">Family</button>
    <button type="button" class="chip" data-f="recruiter" aria-pressed="false">Recruiter</button>
    <button type="button" class="chip" data-f="alumni" aria-pressed="false">Alumni</button>
    <button type="button" class="chip swchip" data-f="swept" aria-pressed="false"
      title="Removed by the system, not by you: review that aged out, plus dead links the weekly sweep found (404s)">Auto-Swept</button>
    <button type="button" class="chip" data-f="hasroles" aria-pressed="false">Has Roles</button>
    <span class="ddwrap" id="dd-cmiss"><button type="button" class="chip" id="dd-cmiss-btn" aria-haspopup="true" aria-expanded="false">Missing</button><div class="ddpanel" id="dd-cmiss-panel" hidden></div></span>
    <button type="button" class="chip" data-f="funded" aria-pressed="false"
      title="Form D filed in the last 60 days">Recently Funded</button>
    <select aria-label="Filter by contact info on file" id="hascontact">
      <option value="">Contact: Any</option>
      <option value="linkedin">Has LinkedIn</option>
      <option value="email">Has Email</option>
      <option value="x">Has X</option>
      <option value="phone">Has Phone</option>
    </select>
    <button type="button" class="chip" id="clearfil"
      title="Reset every filter — the sort stays">Clear</button>
    <button id="triagebtn" class="btn addpill" hidden>⚡ Triage</button>
    <button id="addbtn" class="btn addpill" hidden>+ Add</button>
  </div>
  <div id="scoutbar" class="scoutbar" hidden>
    <div class="scoutmsg" id="scoutmsg">searching…</div>
    <div class="scouttrack"><div class="scoutfill" id="scoutfill"></div></div>
  </div>
</header>
<div id="splash" hidden aria-hidden="true"><span class="spllogo">❧</span></div>
<div class="fabs" id="fabs">
  <button type="button" class="fab" id="fab-search" aria-label="Search"><span class="fabicon fabmag">⌕</span><span class="fablbl">Search</span></button>
  <button type="button" class="fab fabhide" id="fab-triage" aria-label="Speed triage"><span class="fabicon">⚡</span><span class="fablbl">Triage</span></button>
  <button type="button" class="fab fab-add fabhide" id="fab-add" aria-label="Add"><span class="fabicon">+</span><span class="fablbl" id="fab-add-lbl">Add</span></button>
  <button type="button" class="fab fab-scout fabhide" id="fab-scout" aria-label="Find new roles"><span class="fabicon">☄</span><span class="fablbl">Find New Roles</span></button>
</div>
<button id="kbhint" class="kbhint" aria-label="Keyboard shortcuts (hold ⌥)"
  title="Keyboard shortcuts — hold ⌥ or click">Hold ⌥ for Shortcuts</button>
<div id="kbhelp" class="kbhelp" role="dialog" aria-label="Keyboard shortcuts">
  <div class="kbtitle2">Keyboard Shortcuts</div>
  <div class="kbgrid">
    <kbd>1</kbd><span>Tracker</span>
    <kbd>2</kbd><span>Postings</span>
    <kbd>3</kbd><span>Tweets</span>
    <kbd>4</kbd><span>Companies</span>
    <kbd>5</kbd><span>Rolodex</span>
    <kbd>/</kbd><span>Search</span>
    <kbd>a</kbd><span>Add Person / Company</span>
    <kbd>⌘·click</kbd><span>Multi-Select People</span>
    <kbd>f</kbd><span>Find New Roles</span>
    <kbd>t</kbd><span>Theme</span>
    <kbd>esc</kbd><span>Close Dialogs</span>
  </div>
  <div class="co">hold ⌥ to peek · ? to pin</div>
</div>
<div class="modal" id="cardmodal" role="dialog" aria-modal="true" aria-label="Card detail">
  <div class="cardbox">
    <div id="triagebar" hidden>
      <span class="tmode">⚡ TRIAGE</span>
      <span id="triageprog"></span>
      <span class="tkeys"><kbd>s</kbd> save · <kbd>x</kbd> pass · <kbd>space</kbd> skip</span>
      <button type="button" class="btn" onclick="endTriage()">✕ Exit</button>
    </div>
    <button type="button" class="btn mclose" onclick="closeModal()" aria-label="Close">✕</button>
    <div id="cardhost"></div>
  </div>
</div>
<div class="modal" id="actmodal" role="dialog" aria-modal="true" aria-label="Activity log">
  <div class="mbox">
    <div class="mhead"><h2>Activity</h2>
      <button type="button" class="btn" onclick="closeModal()" aria-label="Close">✕</button></div>
    <div id="act-list" class="actlist" tabindex="-1"></div>
  </div>
</div>
<div class="modal" id="searchmodal" role="dialog" aria-modal="true" aria-label="Search" style="align-items:center">
  <div class="searchbox">
    <input type="search" id="sq" placeholder="search postings, companies, people…" aria-label="Search everything">
  </div>
</div>
<div class="modal" id="histmodal" role="dialog" aria-modal="true" aria-label="History" style="align-items:center">
  <div class="mbox" style="max-width:520px">
    <div class="mhead"><h3 id="hist-title">History</h3>
      <button type="button" class="btn" onclick="closeOnly('histmodal')" aria-label="Close">✕</button></div>
    <div id="hist-body" style="padding:4px 2px 10px"></div>
  </div>
</div>
<div class="modal" id="addpostmodal" role="dialog" aria-modal="true" aria-label="Add posting" style="align-items:center">
  <div class="mbox qebox">
    <div class="mhead"><h2>Add Posting</h2>
      <button type="button" class="btn" onclick="closeOnly('addpostmodal')" aria-label="Close">✕</button></div>
    <input id="ap-url" type="url" aria-label="Posting URL" placeholder="posting URL — or leave empty for an off-market role" style="width:100%;margin-bottom:8px">
    <input id="ap-title" type="text" aria-label="Title" placeholder="title (required without a URL)" style="width:100%;margin-bottom:8px">
    <input id="ap-company" type="text" aria-label="Company" placeholder="company (required without a URL)" style="width:100%">
    <div class="exprow" style="padding-right:0"><button class="btn primary" id="ap-save">Add</button>
      <button class="btn" onclick="closeOnly('addpostmodal')">Cancel</button></div>
  </div>
</div>
<div class="modal" id="qeditmodal" role="dialog" aria-modal="true" aria-label="Quick edit" style="align-items:center">
  <div class="mbox qebox">
    <div class="mhead"><h2 id="qe-title">Edit</h2>
      <button type="button" class="btn" onclick="closeOnly('qeditmodal')" aria-label="Close">✕</button></div>
    <div id="qe-body"></div>
    <div class="exprow" style="padding-right:0"><button class="btn primary" id="qe-save">Save</button>
      <button class="btn" onclick="closeOnly('qeditmodal')">Cancel</button></div>
  </div>
</div>
<div class="modal" id="aboutmodal" role="dialog" aria-modal="true" aria-label="About SteinJobs">
  <div class="mbox">
    <div class="mhead"><h2>What SteinJobs does</h2>
      <button type="button" class="btn" onclick="closeModal()" aria-label="Close">✕</button></div>
    <div class="aboutbody">
      <p><b>The job:</b> find early-stage startup roles, the companies behind them, and the
      people to write to — then track every thread until it closes. It scores each posting
      0–100 against Eric's rubric (role shape 35% · stage 25% · industry 20% · location 10%
      · comp 10%), and it never writes resumes or emails: it puts the right person, the right
      timing, and the right proof point in front of you, and stops.</p>
      <h4>What gets found</h4>
      <ul>
        <li>Pre-seed → Series A (B only for explicit 0→1 builds); high-agency generalist
        shapes — founding/early, growth, BD, chief of staff who builds, PM, technical-GTM.</li>
        <li>Sweet spot: health &amp; human performance. Interest exceptions with real history:
        music, psychedelics/mental health, neuro tools, food systems tech, ed-tech, civic
        tech, fitness, outdoors.</li>
        <li>Hard-excluded at import: pure IC data science, big-company seats (and mega-corp
        careers hosts), consulting, senior titles, AE/AM/CS/designer execution seats,
        internships, postings asking for graduate degrees.</li>
        <li>NYC first, SF fine, remote for the right company; Europe only with sponsorship
        (Eric is a US citizen), judged against local comp.</li>
      </ul>
      <h4>How a posting flows</h4>
      <p>Scout (daily) → prefilter → score → <b>Review</b> pile → triage ✓/✕ → <b>Saved</b> →
      tracker board (applied → interviewing → offer) → sends &amp; follow-ups (nudges at day
      4 and 11 — two is the whole budget). Postings scoring ≤50 auto-pass; stale review ages
      out (<i>Auto-Swept</i>); pages that 404 become <i>expired</i> — both hide behind the
      Auto-Swept lens. Postings arriving without a description wait as
      <i>incomplete</i>, invisible until text and a score promote them.</p>
      <h4>The rules the data lives by</h4>
      <ul>
        <li>No blind scoring — no description, no score, ever.</li>
        <li>Never invent founders, emails, stages, descriptions. A blank beats a guess.</li>
        <li>Duplicates backfill blanks only; they never overwrite a recorded value.</li>
        <li>Acquired brands resolve to their acquirer.</li>
        <li>A person's warm signals come only from their own verified text — never from
        Eric-side context.</li>
        <li>LinkedIn and Wellfound are reached only through Eric's real Chrome, on request.</li>
      </ul>
      <h4>Reading the cards</h4>
      <ul>
        <li>Glyphs: 🔥 warm · 🎓 multi-school alum · 🦅 Emory · 🐗 NMH · 🌱 pre-seed ·
        🌳 seed · $A/$B+ later rounds · 💰 raised (fresh &lt;30d goes green) · 🗽 🌉 🍀 🌐
        cities, country flags abroad. Open any card and glyphs speak in words.</li>
        <li>Gray company card = no description or industry yet; dashed +N = fields missing;
        muted card = you already opened it; loud green posting = offer. The muted
        intel line on a posting ("raised 2mo ago · ~12 ppl · 3 roles in the feed")
        is the why-now at a glance; <i>slow apply</i> flags long-form ATS hosts.</li>
      </ul>
      <h4>It learns from you</h4>
      <p>Every pass-with-a-reason becomes a worked example the scorer reads next time.
      Insights reports where the scorer disagrees with your actual verdicts, which sources
      earn their keep, and which proof points open doors.</p>
    </div>
  </div>
</div>
<div class="modal" id="queuemodal" role="dialog" aria-modal="true" aria-label="Sitting queue">
  <div class="mbox">
    <div class="mhead"><h2>Queue</h2>
      <button type="button" class="btn" onclick="closeModal()" aria-label="Close">✕</button></div>
    <div id="queue-list" class="actlist" tabindex="-1"></div>
  </div>
</div>
<div class="modal" id="diagmodal" role="dialog" aria-modal="true" aria-label="Pipeline diagnostics">
  <div class="mbox">
    <div class="mhead"><h2>Diagnostics &amp; Insights</h2>
      <button type="button" class="btn" onclick="closeModal()" aria-label="Close">✕</button></div>
    <div id="diag-list" class="actlist" tabindex="-1"></div>
  </div>
</div>
<div id="bulkbar" class="bulkbar" hidden>
  <span id="bulkcount">0 selected</span>
  <button type="button" class="btn autobtn" id="bulk-autofill" onclick="bulkAutofill()" hidden>Autofill All</button>
  <button type="button" class="btn" onclick="bulkSet('saved')">Save All</button>
  <button type="button" class="btn" onclick="bulkSet('uninterested')">Uninterested All</button>
  <button type="button" class="btn" onclick="bulkClear()">Clear</button>
</div>
<div id="pagefade" class="pagefade" aria-hidden="true" hidden></div>
<main id="main">
  <div id="pane-tracker"><div id="tracker"></div></div>
  <div id="pane-postings" hidden><div id="list"></div></div>
  <div id="pane-companies" hidden><div id="companies" class="cgrid"></div></div>
  <div id="pane-people" hidden><div id="people"></div></div>
  <div id="pane-tweets" hidden><div id="tweets"></div></div>
</main>

<div class="modal" id="modal" role="dialog" aria-modal="true" aria-label="Insights">
  <div class="sheet" style="max-width:880px">
    <h2>Insights</h2>
    <pre id="modal-pre" tabindex="-1"></pre>
  </div></div>

<div class="modal" id="datemodal" role="dialog" aria-modal="true" aria-label="When did this happen">
  <div class="sheet" style="max-width:340px">
    <h2 id="dm-title">When did this happen?</h2>
    <div class="frow one"><input aria-label="Date it happened" type="date" id="dm-date"></div>
    <div class="frow" id="dm-methodrow" hidden>
      <select id="dm-method" aria-label="How did you reach out">
        <option value="email">Email</option>
        <option value="linkedin">LinkedIn</option>
        <option value="x">X</option>
        <option value="ig">Instagram</option>
        <option value="phone">Phone</option>
        <option value="irl">In person</option>
      </select>
      <input id="dm-detail" aria-label="Contact detail" placeholder="their email address…">
    </div>
    <div class="facts">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn" id="dm-today">Today</button>
      <button class="btn primary" id="dm-save">That day</button>
    </div>
  </div>
</div>

<div class="modal" id="formmodal" role="dialog" aria-modal="true" aria-label="Add entry" style="align-items:center">
  <div class="sheet">
    <div id="form-person" hidden>
      <h2 id="fp-title">Add a person</h2>
      <div class="frow">
        <input aria-label="Name" id="fp-name" placeholder="name *">
        <input aria-label="Company" id="fp-company" placeholder="company">
      </div>
      <div class="frow">
        <input aria-label="Role" id="fp-role" placeholder="role">
        <select aria-label="Founder or employee" id="fp-rel">
          <option value="contact">contact</option>
          <option value="employee">employee</option>
          <option value="founder">founder</option>
          <option value="recruiter">recruiter</option>
        </select>
      </div>
      <div class="frow">
        <select aria-label="Connection" id="fp-signal">
          <option value="">no connection</option>
          <option value="Emory">Emory</option>
          <option value="NMH">NMH</option>
          <option value="Sports">Sports</option>
          <option value="Family">Family</option>
        </select>
        <input aria-label="LinkedIn URL" id="fp-linkedin" placeholder="linkedin url">
      </div>
      <div class="frow">
        <input aria-label="Email" id="fp-email" placeholder="email">
        <input aria-label="X handle" id="fp-x" placeholder="x / twitter">
      </div>
      <div class="frow">
        <select aria-label="Warm or cold lead" id="fp-warm" onchange="warmVia()">
          <option value="">Cold</option>
          <option value="1">Warm</option>
        </select>
        <input aria-label="Warm via — who or how you know them" id="fp-wvia"
          placeholder="warm via — who / how you know them">
      </div>
      <div class="frow">
        <input aria-label="Phone" id="fp-phone" placeholder="phone">
        <input aria-label="Location (from their LinkedIn)" id="fp-loc"
          placeholder="location — from their LinkedIn">
      </div>
      <div class="frow one">
        <textarea aria-label="Notes" id="fp-notes" rows="3"
          placeholder="notes"></textarea>
      </div>
      <div class="facts">
        <button class="btn" onclick="closeModal()">Cancel</button>
        <button class="btn autobtn" id="fp-autofill">Autofill from LinkedIn</button>
        <button class="btn primary" id="fp-save">Add person</button>
      </div>
    </div>
    <div id="form-company" hidden>
      <h2 id="fc-title">Follow a Company</h2>
      <div class="frow">
        <input aria-label="Company name" id="fc-name" placeholder="company name *">
        <select aria-label="Industry" id="fc-ind"><option value="">Industry…</option></select>
      </div>
      <div class="frow one"><textarea aria-label="Description" id="fc-desc" rows="4"
        placeholder="description — what it does"
        oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"></textarea></div>
      <div class="frow">
        <select aria-label="Location" id="fc-loc"><option value="">Location…</option></select>
        <select aria-label="Round" id="fc-round">
          <option value="">Round…</option>
          <option value="pre_seed">Pre Seed</option><option value="seed">Seed</option>
          <option value="series_a">Series A</option><option value="growth">Growth</option>
          <option value="other">Other</option>
        </select>
      </div>
      <div class="frow">
        <input aria-label="Website" id="fc-site" placeholder="website">
        <input aria-label="LinkedIn page" id="fc-linkedin" placeholder="linkedin page">
      </div>
      <div class="frow one"><textarea aria-label="Note" id="fc-why" rows="3"
        placeholder="note"
        oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"></textarea></div>
      <div class="facts">
        <button class="btn autobtn" id="fc-autofill">Autofill</button>
        <button class="btn" id="fc-fetch" hidden>Fetch from site</button>
        <button class="btn" onclick="closeModal()">Cancel</button>
        <button class="btn primary" id="fc-save">Follow company</button>
      </div>
    </div>
  </div>
</div>


<script>
let DATA = __DATA__;
const YQ = new Set();          // experience buckets, multi-select
// One Set per enum filter, multi-select OR (Eric, 2026-08-18: "checkboxes
// for rounds, cities, job types, industries, sources"). city/ind/rnd are
// shared by the Postings and Companies tabs, as their selects were.
const MSEL = {city: new Set(), ind: new Set(), rnd: new Set(), src: new Set(),
              cat: new Set(), pmiss: new Set(), cmiss: new Set()};
const DD_TITLE = {city: "Cities", ind: "Industries", rnd: "Rounds",
                  src: "Sources", cat: "Job Types", pmiss: "Missing", cmiss: "Missing"};
function ddLabel(k){
  const b = el("dd-" + k + "-btn"); if(!b) return;
  const set = MSEL[k];
  if(!set.size){ b.textContent = DD_TITLE[k]; b.classList.remove("actv"); return; }
  const lbls = [...set].map(v => {
    const cb = document.querySelector(`.ddpanel input[data-dd="${k}"][value="${CSS.escape(v)}"]`);
    return cb ? cb.nextElementSibling.dataset.lbl : v; });
  b.textContent = lbls.length <= 2 ? lbls.join(" · ") : `${lbls[0]} +${lbls.length - 1}`;
  b.classList.add("actv");
}
function ddRows(key, rows){
  el("dd-" + key + "-panel").innerHTML = rows.map(([v, lbl]) =>
    `<label class="ddrow"><input type="checkbox" data-dd="${key}" value="${esc(v)}"
       aria-label="${esc(lbl)}"><span class="ddlbl" data-lbl="${esc(lbl)}">${esc(lbl)}</span></label>`).join("");
}
const yqBucket = r => (r.yoe === null || r.yoe === undefined) ? "none"
  : r.yoe <= 1 ? "le1" : r.yoe <= 3 ? "mid" : "hi";
const F = {unscored:false, visa:false, alum:false, warm:false, hasroles:false, followedco:false,
           family:false, recruiter:false, sports:false, music:false, research:false};
// People are "alums" only for the school signals, not every shared-background tag.
const ALUM_SIGNALS = ["Emory", "NMH"];
const SCHOOL_GLYPH = {Emory: "🦅", NMH: "🐗"};   // Swoop; the Hoggers
const schoolChip = x => `<span class="sig ${sigClass(x)}" title="${esc(x)} alum" role="img" aria-label="${esc(x)}">${SCHOOL_GLYPH[x] ? gw(SCHOOL_GLYPH[x], x) : esc(x)}</span>`;
// Sports is warmth without being alumni-ness — Eric did NFL data science for
// the Jets, so a sports background opens a door the way a shared school does.
// Kept OUT of ALUM_SIGNALS on purpose: that list drives the "Alumni" collapse
// and the kanban alum badge, both of which mean school.
const SPORTS_SIGNALS = ["Sports", "NY Jets", "NFL / football analytics", "Big Data Bowl"];
const MUSIC_SIGNALS = ["Music", "Trash Compactor"];
const RESEARCH_SIGNALS = ["Neuroscience", "Psychedelics", "UCSF Neuroscape"];
// Somewhere Eric actually was. Anyone who worked or studied at one of these
// is warm on that basis alone (Eric, 2026-08-07 — "anyone with a degree from
// UCSF, or who has worked at UCSF, warm for sure"). Before this, UCSF /
// Carter Center / Foxino / CIEE were DETECTED but rendered as plain grey
// chips and never passed the Warm filter.
const INSTITUTION_SIGNALS = ["UCSF", "UCSF Neuroscape", "Carter Center", "Foxino",
                             "CIEE Prague", "Trash Compactor", "NY Jets"];
// Interests, not shared rooms — Prague, Atlanta, powerlifting, the PCT, WWOOF,
// thru-hiking, personal training. Real chips, real search terms, but they
// don't make a stranger warm on their own.
const WARM_SIGNALS = [...new Set([...ALUM_SIGNALS, ...SPORTS_SIGNALS,
  ...MUSIC_SIGNALS, ...RESEARCH_SIGNALS, ...INSTITUTION_SIGNALS])];
// One place deciding a signal's chip colour. Two hardcoded Emory/NMH ternaries
// used to do this, and the person-side one had NO fallback branch — any third
// label silently wore NMH styling.
const SIG_CLASS = {Emory:"sig-emory", NMH:"sig-nmh", Family:"sig-family"};
INSTITUTION_SIGNALS.forEach(s => SIG_CLASS[s] = "sig-inst");
SPORTS_SIGNALS.forEach(s => SIG_CLASS[s] = "sig-sports");
MUSIC_SIGNALS.forEach(s => SIG_CLASS[s] = "sig-music");
RESEARCH_SIGNALS.forEach(s => SIG_CLASS[s] = "sig-neuro");
const sigClass = x => SIG_CLASS[x] || "";
// Which chip a signal collapses into. A person who is both "Neuroscience" and
// "UCSF Neuroscape" is one Research chip, not two; the specific labels survive
// in the chip's tooltip so the evidence is still one hover away. Institutions
// keep their own name — "UCSF" says more than "Institution" ever would.
const FAM_LABEL = {};
SPORTS_SIGNALS.forEach(s => FAM_LABEL[s] = "Sports");
MUSIC_SIGNALS.forEach(s => FAM_LABEL[s] = "Music");
RESEARCH_SIGNALS.forEach(s => FAM_LABEL[s] = "Research");
INSTITUTION_SIGNALS.forEach(s => { if(!FAM_LABEL[s]) FAM_LABEL[s] = s; });
// Non-school warm signals → one chip per family, in a stable order.
function famChips(tags, ev){
  const seen = new Map();
  tags.filter(x => !ALUM_SIGNALS.includes(x) && WARM_SIGNALS.includes(x))
      .forEach(x => {
        const key = FAM_LABEL[x] || x;
        if(!seen.has(key)) seen.set(key, []);
        seen.get(key).push(x);
      });
  return [...seen.entries()].map(([label, from]) => {
    // Prefer the words that actually matched. A signal can come from a cached
    // profile Eric never sees on the card, and "Sports" with nothing behind it
    // is one hover away from him claiming a shared past that isn't there.
    const why = from.map(x => (ev||{})[x]).filter(Boolean);
    const tip = why.length ? why.join(" · ") : from.join(" · ");
    return `<span class="sig ${sigClass(from[0])}" title="${esc(tip)}">${esc(label)}</span>`;
  }).join("");
}
const isSports = pv => (pv.signals||[]).some(x => SPORTS_SIGNALS.includes(x));
const isMusic = pv => (pv.signals||[]).some(x => MUSIC_SIGNALS.includes(x));
const isResearch = pv => (pv.signals||[]).some(x => RESEARCH_SIGNALS.includes(x));
const EXPANDED = new Set();          // postings rows kept open across re-renders
const COMPANY_EXPANDED = new Set();
// Rendering every row put 1,884 cards and 41k DOM nodes on the page — a 5MB
// document and a 240,000px scroll. Render a page at a time.
const PAGE = 60;
let shown = PAGE;
let COMPANY_MODE = "review";
let TAB = "tracker";

const el = id => document.getElementById(id);
// Death metal foley, synthesized on the spot — no audio files. A tiny band:
// tanh-distorted sawtooth guitar, sine-drop kick, noise snare, noise crash,
// and a pinch squeal. Riffs are ROLLED PER CLICK, so no two saves sound alike.
let _ac = null;
function _audio(){ _ac = _ac || new (window.AudioContext || window.webkitAudioContext)(); return _ac; }
function _distNode(ac){
  // high-gain clip into a cabinet-ish lowpass — bare tanh(7x) sounded like a
  // synth, not an amp. The 3.6k rolloff is most of the realism.
  const ws = ac.createWaveShaper(), c = new Float32Array(1024);
  for(let i = 0; i < 1024; i++){ const x = i/512 - 1; c[i] = Math.tanh(16*x) * .9; }
  ws.curve = c; ws.oversample = "4x";
  const cab = ac.createBiquadFilter(); cab.type = "lowpass";
  cab.frequency.value = 3600; cab.Q.value = .8;
  ws.connect(cab);
  ws._out = cab;   // callers connect FROM the cab
  return ws;
}
function _chug(ac, out, t, f, dur, vol){
  // palm-mute anatomy: two detuned "guitars" + sub-octave square, a pitch
  // scoop into the note, an attack chiff of filtered noise, and a lowpass
  // that slams shut — the "djun" is the filter closing, not the note ending.
  dur = dur || .12; vol = vol || .2;
  const g = ac.createGain(); 
  const lp = ac.createBiquadFilter(); lp.type = "lowpass"; lp.Q.value = 1.2;
  lp.frequency.setValueAtTime(2800, t);
  lp.frequency.exponentialRampToValueAtTime(420, t + dur);
  g.connect(lp); lp.connect(out);
  g.gain.setValueAtTime(vol, t);
  g.gain.setValueAtTime(vol*.9, t + dur*.55);
  g.gain.exponentialRampToValueAtTime(.001, t + dur);
  [[f*.997, "sawtooth"], [f*1.004, "sawtooth"], [f/2, "square"]].forEach(([fr, ty]) => {
    const o = ac.createOscillator(); o.type = ty;
    o.frequency.setValueAtTime(fr*1.07, t);                 // scoop in
    o.frequency.exponentialRampToValueAtTime(fr, t + .02);
    o.connect(g); o.start(t); o.stop(t + dur);
  });
  const chiff = _noise(ac, .02), cg = ac.createGain(); cg.gain.value = vol*.5;
  const chp = ac.createBiquadFilter(); chp.type = "highpass"; chp.frequency.value = 1200;
  chiff.connect(chp); chp.connect(cg); cg.connect(out); chiff.start(t);
}
function _slam(ac, out, t, f, dur){
  // the pitch bomb: a chug whose whole pitch falls a fourth and keeps going
  dur = dur || .38; f = f || 61.74;
  const g = ac.createGain();
  const lp = ac.createBiquadFilter(); lp.type = "lowpass"; lp.Q.value = 1.4;
  lp.frequency.setValueAtTime(2200, t);
  lp.frequency.exponentialRampToValueAtTime(220, t + dur);
  g.connect(lp); lp.connect(out);
  g.gain.setValueAtTime(.3, t);
  g.gain.exponentialRampToValueAtTime(.001, t + dur);
  [f*.996, f*1.005, f/2].forEach(fr => {
    const o = ac.createOscillator(); o.type = "sawtooth";
    o.frequency.setValueAtTime(fr, t);
    o.frequency.exponentialRampToValueAtTime(fr*.62, t + dur);
    o.connect(g); o.start(t); o.stop(t + dur);
  });
}
function _kick(ac, out, t){
  // slam kicks are clicky: a 6ms beater snap over the sine drop
  const o = ac.createOscillator(), g = ac.createGain();
  o.type = "sine";
  o.frequency.setValueAtTime(160, t);
  o.frequency.exponentialRampToValueAtTime(32, t + .07);
  g.gain.setValueAtTime(.55, t);
  g.gain.exponentialRampToValueAtTime(.001, t + .11);
  o.connect(g); g.connect(out); o.start(t); o.stop(t + .12);
  const click = _noise(ac, .008), cg = ac.createGain(); cg.gain.value = .35;
  const hp = ac.createBiquadFilter(); hp.type = "highpass"; hp.frequency.value = 2500;
  click.connect(hp); hp.connect(cg); cg.connect(out); click.start(t);
}
function _noise(ac, secs){
  const len = Math.floor(ac.sampleRate * secs), buf = ac.createBuffer(1, len, ac.sampleRate);
  const d = buf.getChannelData(0);
  for(let i = 0; i < len; i++) d[i] = (Math.random()*2 - 1) * Math.pow(1 - i/len, 2);
  const src = ac.createBufferSource(); src.buffer = buf; return src;
}
function _snare(ac, out, t){
  // gunshot snare: 190Hz shell thump under a hard noise crack
  const src = _noise(ac, .1);
  const bp = ac.createBiquadFilter(); bp.type = "bandpass"; bp.frequency.value = 2400; bp.Q.value = .6;
  const g = ac.createGain(); g.gain.setValueAtTime(.34, t);
  g.gain.exponentialRampToValueAtTime(.001, t + .1);
  src.connect(bp); bp.connect(g); g.connect(out); src.start(t);
  const o = ac.createOscillator(), og = ac.createGain();
  o.type = "triangle"; o.frequency.setValueAtTime(190, t);
  o.frequency.exponentialRampToValueAtTime(120, t + .05);
  og.gain.setValueAtTime(.3, t); og.gain.exponentialRampToValueAtTime(.001, t + .07);
  o.connect(og); og.connect(out); o.start(t); o.stop(t + .08);
}
function _crash(ac, out, t){
  // two noise bands (wash + sizzle) so it shimmers instead of hissing
  const src = _noise(ac, .8);
  const hp = ac.createBiquadFilter(); hp.type = "highpass"; hp.frequency.value = 4200;
  const g = ac.createGain(); g.gain.setValueAtTime(.2, t);
  g.gain.exponentialRampToValueAtTime(.001, t + .8);
  src.connect(hp); hp.connect(g); g.connect(out); src.start(t);
  const s2 = _noise(ac, .35);
  const bp = ac.createBiquadFilter(); bp.type = "bandpass"; bp.frequency.value = 9000; bp.Q.value = 1.5;
  const g2 = ac.createGain(); g2.gain.setValueAtTime(.12, t);
  g2.gain.exponentialRampToValueAtTime(.001, t + .35);
  s2.connect(bp); bp.connect(g2); g2.connect(out); s2.start(t);
}
function _squeal(ac, out, t){
  const o = ac.createOscillator(), g = ac.createGain();
  o.type = "square";
  o.frequency.setValueAtTime(620, t);
  o.frequency.exponentialRampToValueAtTime(1500 + Math.random()*600, t + .16);
  g.gain.setValueAtTime(.05, t);
  g.gain.exponentialRampToValueAtTime(.001, t + .2);
  o.connect(g); g.connect(out); o.start(t); o.stop(t + .2);
}
// Real samples (Karplus-Strong guitar rendered server-side at /sfx/) with
// the oscillator synth as fallback until they load / if they can't.
const SFXB = {};
function _loadSfx(){
  if(SFXB.ok || SFXB.loading || SFXB.fail) return;
  SFXB.loading = true;
  const ac = _audio();
  Promise.all(["chug","kick","snare","crash"].map(n =>
    fetch("/sfx/" + n + ".wav").then(r => r.arrayBuffer())
      .then(b => ac.decodeAudioData(b)).then(buf => { SFXB[n] = buf; })))
    .then(() => { SFXB.ok = true; }, () => { SFXB.fail = true; });
}
function _play(out, name, t, rate, gain, rateEnd){
  const ac = _audio(), b = SFXB[name];
  const src = ac.createBufferSource(); src.buffer = b;
  src.playbackRate.setValueAtTime(rate || 1, t);
  if(rateEnd) src.playbackRate.exponentialRampToValueAtTime(rateEnd, t + b.duration);
  const g = ac.createGain(); g.gain.value = gain || 1;
  src.connect(g); g.connect(out); src.start(t);
}
function metal(kind){
  _loadSfx();
  try{
    const ac = _audio(), t0 = ac.currentTime;
    const comp = ac.createDynamicsCompressor();
    comp.threshold.value = -18; comp.ratio.value = 6;
    comp.attack.value = .002; comp.release.value = .12;
    comp.connect(ac.destination);
    const master = ac.createGain(); master.gain.value = 1; master.connect(comp);
    const dist = _distNode(ac); dist._out.connect(master);
    const E = 82.41, B = 61.74, G = 98.0, A = 110.0, C = 130.81;
    if(SFXB.ok){
      // sample land: the chug was rendered at 55Hz — rate = f/55 repitches it
      const ch = (t, f, gain) => _play(master, "chug", t, (f||E)/55, gain||.9);
      const slam = (t, f) => _play(master, "chug", t, (f||B)/55, 1, (f||B)/55*.58);
      const kck = t => _play(master, "kick", t, .96 + Math.random()*.08, 1);
      const snr = t => _play(master, "snare", t, .96 + Math.random()*.08, .9);
      const crs = (t, g) => _play(master, "crash", t, 1, g||.7);
      if(kind === "save"){
        const step = .09, notes = [E, E, G, A, B, C];
        const n = 3 + Math.floor(Math.random()*2);
        for(let i = 0; i < n; i++) ch(t0 + i*step, notes[Math.floor(Math.random()*notes.length)]);
        kck(t0); if(Math.random() < .5) kck(t0 + 2*step);
        if(Math.random() < .6) crs(t0 + n*step, .5);
      } else if(kind === "kill"){
        slam(t0, B); kck(t0); kck(t0 + .17);
      } else if(kind === "drop"){
        ch(t0, E, .6); ch(t0 + .1, E, .5); kck(t0);
      } else if(kind === "offer"){
        crs(t0, .8); ch(t0, E); slam(t0 + .3, E);
        kck(t0); kck(t0 + .14); kck(t0 + .38); crs(t0 + .45, .6);
      } else if(kind === "blast"){
        for(let i = 0; i < 4; i++){ kck(t0 + i*.085); snr(t0 + i*.085 + .042); }
        crs(t0 + .34, .8);
      } else {
        const r = Math.random();
        if(r < .4) snr(t0);
        else if(r < .8) ch(t0, [E, G, A][Math.floor(Math.random()*3)], .6);
        else slam(t0, G);
      }
      return;
    }
    if(kind === "save"){
      // 3–4 random 16ths off the E-minor low end, kick under, maybe a crash out
      const step = .09, notes = [E, E, G, A, B, C];
      const n = 3 + Math.floor(Math.random()*2);
      for(let i = 0; i < n; i++)
        _chug(ac, dist, t0 + i*step, notes[Math.floor(Math.random()*notes.length)]);
      _kick(ac, master, t0);
      if(Math.random() < .5) _kick(ac, master, t0 + 2*step);
      if(Math.random() < .6) _crash(ac, master, t0 + n*step);
    } else if(kind === "kill"){
      _slam(ac, dist, t0, B, .4);
      _kick(ac, master, t0); _kick(ac, master, t0 + .17);
    } else if(kind === "drop"){
      _chug(ac, dist, t0, E, .12); _chug(ac, dist, t0 + .1, E, .09);
      _kick(ac, master, t0);
    } else if(kind === "offer"){
      // the big one: crash + sustained low chord + double kick
      _crash(ac, master, t0);
      _chug(ac, dist, t0, E, .7, .2); _slam(ac, dist, t0 + .3, E, .5);
      _kick(ac, master, t0); _kick(ac, master, t0 + .14); _kick(ac, master, t0 + .38);
      _crash(ac, master, t0 + .45);
    } else if(kind === "blast"){
      // a bar of blast beat for the scout button
      for(let i = 0; i < 4; i++){
        _kick(ac, master, t0 + i*.085);
        _snare(ac, master, t0 + i*.085 + .042);
      }
      _crash(ac, master, t0 + .34);
    } else {
      const r = Math.random();
      if(r < .33) _snare(ac, master, t0);
      else if(r < .66) _chug(ac, dist, t0, [E, G, A][Math.floor(Math.random()*3)], .1);
      else _squeal(ac, dist, t0);
    }
  }catch(_){}
}
let POSTING_MODE = "review";   // triage pile first — that's the work
window.setPostingMode = (m) => {
  POSTING_MODE = m;
  for(const [id, mm] of [["ps-review","review"],["ps-saved","saved"],["ps-uninterested","uninterested"]]){
    el(id).classList.toggle("on", m === mm);
    el(id).setAttribute("aria-pressed", String(m === mm));
  }
  moveModeSlides();
  shown = PAGE; render();
};
// Everything past review that isn't a pass lives in the Saved bucket; the
// tracker board is where the fine-grained stages live.
const bucketOf = st => st === "review" ? "review"
  : st === "uninterested" ? "uninterested"
  : st === "expired" ? "expired"
  // no-description intake rows; they promote themselves when text + a score
  // arrive (pipeline intake gate, 2026-08-19) — nobody's pile until then
  : st === "incomplete" ? "incomplete" : "saved";
const esc = s => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const jsq = s => (s||"").replace(/\\/g,"\\\\").replace(/'/g,"\\'");
const cap = s => s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
const tc = s => (s||"").replace(/\w\S*/g, w => w.charAt(0).toUpperCase() + w.slice(1));
// Board slugs get their real names on screen. Declared HERE, above the
// top-level dropdown-population block that calls it — a later declaration
// is a TDZ crash that takes the whole script down (see the `cap` incident).
const SRC_NAMES = {workatastartup:"Work at a Startup", southparkcommons:"South Park Commons",
  generalist:"Generalist", rockhealth:"Rock Health", a16z:"a16z", greylock:"Greylock",
  techstars:"Techstars", atomico:"Atomico", accel:"Accel", speedinvest:"Speedinvest",
  hvcapital:"HV Capital", builtin:"Built In", builtinnyc:"Built In NYC", edgar:"SEC EDGAR",
  "landing.jobs":"Landing.jobs", landingjobs:"Landing.jobs",
  watchlist:"watchlist", shortlist:"shortlist", manual:"by hand", wellfound:"Wellfound",
  substack:"Substack newsletters", climatebase:"Climatebase", teamworkonline:"TeamWork Online",
  x:"X (via Grok)"};
const srcDisp = k => SRC_NAMES[k] || k;

function short(s, n){ s=(s||"").trim(); return s.length<=n ? s : s.slice(0,n).replace(/\s+\S*$/,"")+"…"; }
function ago(iso){
  const d = daysAgo(iso);
  if(d === null) return iso;
  if(d <= 0) return "today";
  if(d < 14) return `${d}d ago`;
  if(d < 61) return `${Math.round(d/7)}w ago`;
  return `${Math.round(d/30.4)}mo ago`;
}
function daysAgo(iso){ if(!iso) return null;
  const d=(new Date(DATA.built)-new Date(iso))/86400000; return isNaN(d)?null:Math.floor(d); }
function postedAge(r){ return daysAgo(r.p || r.fs); }
// The scorer emphasises decision-relevant phrases with **markdown bold**;
// escape first, then honor only that marker.
function emph(t){ return esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"); }
// Real-div masonry. CSS multicol fragmented the cards and misrendered them
// three different ways (clipped glows, transform shimmer, vanishing on
// hover); plain flex columns can't. Greedy shortest-column placement, with
// html length as the height proxy.
function mason(items, hostWidth, maxCols = 3){
  // Three columns max for postings/companies (Eric, 2026-08-18) — four made
  // those cards strips. Rolodex cards are half the height and run 4.
  const n = Math.max(1, Math.min(items.length || 1, maxCols, Math.floor((hostWidth + 12) / 392)));
  const cols = Array.from({length: n}, () => ({h: 0, parts: []}));
  for(const html of items){
    const c = cols.reduce((a, b) => b.h < a.h ? b : a);
    c.parts.push(html); c.h += 90 + html.length / 6;
  }
  return cols.map(c => `<div class="mcol">${c.parts.join("")}</div>`).join("");
}
function setFacetCounts(key, counts, noneCount){
  document.querySelectorAll(`.ddpanel input[data-dd="${key}"]`).forEach(cb => {
    const n = cb.value === "__none" ? (noneCount || 0) : (counts[cb.value] || 0);
    const s = cb.nextElementSibling;
    s.textContent = `${s.dataset.lbl} (${n})`;
    // zero rows hide — unless checked, so they can still be unchecked
    cb.closest(".ddrow").style.display = (!n && !cb.checked) ? "none" : "";
  });
  ddLabel(key);
}
function hostW(id){
  const w = el(id) && el(id).clientWidth;
  return w && w > 60 ? w : Math.max(360, innerWidth - 48);
}
function cls(n){ if(n===null||n===undefined) return "s-none";
  return n>=90?"s-90":n>=80?"s-80":n>=70?"s-70":n>=45?"s-mid":"s-lo"; }
function ageChip(age){
  if(age === null) return "";
  const label = age === 0 ? "today" : age + "d ago";
  const c = age <= 14 ? "" : age <= 30 ? "age-warm" : "age-hot";
  return `<span class="${c}">${label}</span>`;
}

// One fixed hue per industry, faint wash over the whole card.
// Series B+ and growth read the same to Eric ("past our stage"); PE, ICO and
// acquisitions aren't rounds he'd chase — one Other bucket.
const STAGE_SHORT = {"Pre Seed": "🌱", "Seed": "🌳", "Series A": "$A",
  "Growth": "$B+"};
const stageShort = s => STAGE_SHORT[s] || s;
// Dual-voice chips: glyph in the condensed grid, the word inside any open
// card (Eric, 2026-08-18: "in expanded view none of that matters") — same
// markup, CSS flips which span shows under an .open ancestor.
const gw = (glyph, word) => glyph === word ? esc(glyph)
  : `<span class="gly">${esc(glyph)}</span><span class="wrd">${esc(word)}</span>`;
const salShort = s => /[\u2013\u2014-]/.test(s||"")
  ? (s.split(/[\u2013\u2014-]/)[0].trim() + "+") : s;
function roundGroup(st){
  const x = (st||"").toLowerCase();
  if(!x) return "";
  if(/^series_[b-z]/.test(x) || x === "growth" || x === "late") return "growth";
  // "series_unknown" and "undisclosed" are the same fact — a raise whose
  // round nobody named. One Undisclosed bucket (Eric, 2026-08-12).
  if(x === "series_unknown") return "undisclosed";
  if(["private_equity","ico","acquisition"].includes(x)) return "other";
  return x;
}
const CITY_TONES = {
  "New York": "hsl(140,48%,30%)",      // moss
  "SF Bay Area": "hsl(14,58%,40%)",    // terracotta
  "Remote": "hsl(205,48%,38%)",        // slate
  "Boston": "hsl(302,26%,38%)",        // plum
  "Los Angeles": "hsl(40,60%,34%)",    // gold
  "Seattle": "hsl(172,44%,27%)",       // pine
  "International": "hsl(28,62%,36%)",  // amber
};
function cityTone(city){ return CITY_TONES[city] || earthTone(city); }
// DESIGN.md decision B: hued data chips wear their tone as a wash, not a border
function toneWash(tone){ return tone.replace("hsl(","hsla(").replace(")",",0.15)"); }
const DEFAULT_HUE = 96;   // muted olive — the color of "unclassified"
const IND_HUES = [4, 28, 52, 88, 122, 152, 178, 202, 228, 258, 288, 318];
const IND_COLORS = {
  "digital health": 172, "health": 172, "bio & pharma": 288,
  "fitness & wellness": 96, "dev & data tools": 215, "fintech": 130,
  "ed-tech": 45, "food & ag": 70, "agriculture": 70, "food-systems": 70,
  "commerce & cpg": 25, "robotics & hardware": 240, "legal & compliance": 340,
  "music & creative": 318, "climate": 152, "climate & energy": 152,
  "civic tech": 205, "other": 200, "sports": 8, "enterprise software": 262,
};
function indHue(name){
  if(!name) return DEFAULT_HUE;
  const k = name.toLowerCase().trim();
  if(k in IND_COLORS) return IND_COLORS[k];
  let h = 0; for(const ch of k) h = ((h*31 + ch.charCodeAt(0)) >>> 0) % 3600;
  return IND_HUES[h % IND_HUES.length];
}
// Role types get their own, deliberately different color wheel — the card
// gradient blends industry (top) into role (bottom), so the two must be
// tellable apart at a glance.
const ROLE_COLORS = {
  "Growth": 355, "Sales & BD": 28, "Founding & Generalist": 48,
  "Marketing": 322, "Product & Design": 270, "Technical GTM": 245,
  "Data": 222, "Engineering": 205, "CoS & Strategy": 185, "Operations": 160,
  "People & Talent": 130, "Finance & Legal": 95, "Other": 75,
};
function roleHue(cat){ return ROLE_COLORS[cat] ?? 75; }
function roleChip(cat){
  const h = roleHue(cat);
  return `background:hsl(${h},42%,38%)`;
}
function indChip(name){
  const h = indHue(name);
  return `background:hsla(${h},46%,50%,.34)`;
}
// Postings only (Eric kept this on 2026-08-09 when the sweep proposed one
// wash for every kind): the industry hue pours from the top, the ROLE hue
// rises from the bottom — the gradient is what says "this is a posting,
// not a company" at a glance.
function indGrad(name, cat){
  const h = indHue(name), h2 = cat ? roleHue(cat) : (h + 84) % 360;
  return `background:linear-gradient(146deg,hsla(${h},52%,52%,.42),hsla(${h},46%,50%,.30) 32%,hsla(${h2},46%,50%,.30) 62%,hsla(${h2},48%,50%,.38)),var(--card)`;
}
function indShade(name){
  // Unclassified companies still get one shared default wash, so every card
  // belonging to that company wears the same color.
  const h = indHue(name);
  return `background:linear-gradient(hsla(${h},42%,50%,.22),hsla(${h},42%,50%,.22)),var(--card)`;
}
// Every derived map rebuilds from DATA — softReload() swaps DATA in place
// and calls this again, so actions update the page without a full reload.
if((location.search||"").includes("wrapper=1"))
  document.documentElement.classList.add("wrapper");
const ROW_BY_URL = {}, CO_PPL_N = {}, CO_KEY = {}, CO_REC = {}, CO_IND = {},
      CO_SITE = {}, CO_STAGE = {}, CO_ALUM = {};
const CO_WARM = new Set(), CO_SPORTS = new Set(), CO_MUSIC = new Set(),
      CO_RESEARCH = new Set(), FOLLOWED = new Set();
// A verified alum IS a warm lead — recorded warmth and school signals feed
// the same warmth everywhere (filters, company chips, origin rows).
const isWarm = pv => !!pv.warm || (pv.signals||[]).some(x => WARM_SIGNALS.includes(x));
function initDerived(){
  for(const o of [ROW_BY_URL, CO_PPL_N, CO_KEY, CO_REC, CO_IND, CO_SITE, CO_STAGE, CO_ALUM])
    for(const k in o) delete o[k];
  CO_WARM.clear(); CO_SPORTS.clear(); CO_MUSIC.clear(); CO_RESEARCH.clear();
  FOLLOWED.clear();
  DATA.rows.forEach(r => { ROW_BY_URL[r.u] = r; });
  (DATA.people||[]).forEach(pv => {
    if(isWarm(pv) && pv.company) CO_WARM.add(pv.company.toLowerCase().trim());
    // Same derivation rule as the alumni chips: a company "has sports inside"
    // only because a recorded person there does. Delete the person, lose the flag.
    const _co = (pv.company||"").toLowerCase().trim();
    if(_co && isSports(pv)) CO_SPORTS.add(_co);
    if(_co && isMusic(pv)) CO_MUSIC.add(_co);
    if(_co && isResearch(pv)) CO_RESEARCH.add(_co);
    const k = (pv.company||"").toLowerCase().trim();
    if(k) CO_PPL_N[k] = (CO_PPL_N[k]||0) + 1;
  });
  (DATA.companies||[]).forEach(c => {
    const k = c.name.toLowerCase().trim();
    CO_KEY[k] = c.key;
    CO_REC[k] = c;
    if(c.industry) CO_IND[k] = c.industry;
    if(c.site) CO_SITE[k] = c.site;
    if(c.stage) CO_STAGE[k] = c.stage;
    if((c.alumni||[]).length) CO_ALUM[c.name] = c.alumni;
    // The FILTER also accepts a company that is itself in sports, not just one
    // with a sports person recorded. The CHIP stays person-derived (famTags
    // reads c.alumni) — "someone here worked in sports" is a claim about a
    // person and needs a person behind it; "this is a sports company" isn't.
    const _cs = c.signals || [];
    if(_cs.some(x => SPORTS_SIGNALS.includes(x))) CO_SPORTS.add(k);
    if(_cs.some(x => MUSIC_SIGNALS.includes(x))) CO_MUSIC.add(k);
    if(_cs.some(x => RESEARCH_SIGNALS.includes(x))) CO_RESEARCH.add(k);
    if(c.tracked && !c.ni) FOLLOWED.add(c.name.toLowerCase());
  });
}
initDerived();
// The soft refresh: fetch fresh data, re-render in place — no page flash,
// no lost scroll, no stutter. Falls back to a hard reload if anything fails.
// ---- render scheduling: only the visible tab renders eagerly ------------
// Measured 2026-08-12: every keystroke re-rendered all four tabs (~100ms of
// blocking each), and hidden-tab rebuilds fed the observer leak. Off-screen
// tabs go dirty instead and render once on arrival.
const DIRTY = new Set();
function renderTab(name){
  DIRTY.delete(name);
  if(name === "postings") render();
  else if(name === "companies") renderCompanies();
  else if(name === "people") renderPeople();
  else if(name === "tracker") renderTracker();
  else if(name === "tweets") renderTweets();
}
function markDirty(...tabs){
  tabs.forEach(x => DIRTY.add(x));
  if(DIRTY.has(TAB)) renderTab(TAB);
}
async function softReload(){
  try {
    const twKey = (DATA.tweets || []).map(x => x.url).join("|");
    DATA = await api("/api/data");
    initDerived();
    markDirty("postings", "companies", "people", "tracker");
    // tweets re-render only when the ledger itself changed — the pane renders
    // once so live embeds never reload; triage patches cards in place
    if((DATA.tweets || []).map(x => x.url).join("|") !== twKey) markDirty("tweets");
    else updateTweetCounts();
    if(MODAL_CARD) renderModalCard();
  } catch(_){ location.reload(); }
}
const CITY_ORDER = ["New York","SF Bay Area","Remote","Boston","Los Angeles","Seattle",
                    "Other US","International","Other"];
const US_CITIES = new Set(["New York","SF Bay Area","Remote","Boston","Los Angeles","Seattle","Other US","Other"]);
function nonUsSelected(){ return [...MSEL.city].some(c => c !== "__none" && !US_CITIES.has(c)); }

// dropdown options from the data itself
(function(){

  // Every dropdown option carries its count — "Climate & energy (15)",
  // "Last 7 days (45)" — so a filter says what it will cost before it's used.
  const present = new Set(DATA.rows.map(r=>r.city));
  (DATA.companies||[]).forEach(c=>(c.cities||[]).forEach(x=>present.add(x)));
  const CITY_BUCKET_OPTS = ["Other US","International","Other"];
  ddRows("city", [
    ...[...CITY_ORDER.filter(c=>present.has(c)&&!CITY_BUCKET_OPTS.includes(c)).sort(),
        ...CITY_BUCKET_OPTS.filter(c=>present.has(c))].map(c => [c, c]),
    ["__none", "Unknown Location"]]);
  const srcs = {}; DATA.rows.forEach(r=>srcs[r.src]=(srcs[r.src]||0)+1);
  ddRows("src", Object.keys(srcs)
    .sort((a,b)=>srcDisp(a).localeCompare(srcDisp(b), undefined, {sensitivity:"base"}))
    .map(k => [k, srcDisp(k)]));
  const indN = {}; (DATA.companies||[]).forEach(c=>{ if(c.industry) indN[c.industry]=(indN[c.industry]||0)+1; });
  Object.keys(indN).sort().forEach(i => el("fc-ind").add(new Option(tc(i), i)));
  if(!("other" in indN)) el("fc-ind").add(new Option("Other", "other"));
  [...CITY_ORDER].forEach(cty => el("fc-loc").add(new Option(cty, cty)));
  window.IND_OPTS = Object.keys(indN).sort();
  ddRows("ind", [...IND_OPTS.map(i => [i, tc(i)]), ["__none", "Unknown Industry"]]);
  const rndN = {}; (DATA.companies||[]).forEach(c=>{ const g = roundGroup(c.stage); if(g) rndN[g]=(rndN[g]||0)+1; });
  const RND_ORDER = ["pre_seed","seed","series_a","growth","other"];
  ddRows("rnd", [...RND_ORDER.filter(r=>rndN[r]),
                 ...Object.keys(rndN).filter(r=>!RND_ORDER.includes(r)).sort()]
    .map(r => [r, tc(r.replace("_"," "))]));
  const catN = {}; DATA.rows.forEach(r=>{ if(r.cat) catN[r.cat]=(catN[r.cat]||0)+1; });
  ddRows("cat", (DATA.role_order||[]).filter(k=>catN[k]).map(k => [k, k]));
  ddRows("pmiss", [["unscored","Unscored"], ["location","No location"],
    ["industry","No industry"], ["stage","No round"], ["role","No role type"]]);
  ddRows("cmiss", [["industry","No industry"], ["location","No location"],
    ["stage","No round"], ["site","No website"], ["desc","No description"]]);
  document.querySelectorAll(".ddpanel input[data-dd]").forEach(cb =>
    cb.addEventListener("change", () => {
      const set = MSEL[cb.dataset.dd];
      if(cb.checked) set.add(cb.value); else set.delete(cb.value);
      ddLabel(cb.dataset.dd);
      shown = PAGE; CO_SHOWN = 120;
      markDirty("postings", "companies", "people", "tweets");
    }));
  document.querySelectorAll(".ddwrap > .chip").forEach(b =>
    b.addEventListener("click", () => {
      const p = b.parentElement.querySelector(".ddpanel");
      document.querySelectorAll(".ddpanel").forEach(x => { if(x !== p){ x.hidden = true;
        x.parentElement.querySelector(".chip").setAttribute("aria-expanded", "false"); } });
      p.hidden = !p.hidden;
      b.setAttribute("aria-expanded", String(!p.hidden));
    }));
  const ages = DATA.rows.map(r=>postedAge(r));
  [...el("posted").options].forEach(o => { if(o.value)
    o.text = `Last ${o.value} Days (${ages.filter(a=>a!=null&&a<=Number(o.value)).length})`; });
  const stN = {}; (DATA.people||[]).forEach(pv=>{ const b=bucketOf(pv.st||"review"); stN[b]=(stN[b]||0)+1; });
  el("pt-review").textContent = `Review (${stN.review||0})`;
  el("pt-saved").textContent = `Saved (${stN.saved||0})`;
  const hcN = {linkedin:0, email:0, x:0, phone:0};
  (DATA.people||[]).forEach(pv => { for(const k in hcN) if(pv[k]) hcN[k]++; });
  [...el("hascontact").options].forEach(o => {
    if(o.value) o.text = o.text.replace(/( \(\d+\))?$/, ` (${hcN[o.value]})`); });
  const tkN = {}; (DATA.people||[]).forEach(pv => { tkN[pv.st] = (tkN[pv.st]||0)+1; });
  [...el("talkstage").options].forEach(o => {
    if(o.value) o.text = `${cap(o.value)} (${tkN[o.value]||0})`; });
})();

// ---- triage: one green check to save, one X to pass -------------------------
// The tracker's drag-and-drop owns the fine-grained stages; here the only
// decisions are keep / not for me.
function triageBtns(kind, key, st){
  if(!LIVE) return "";
  const decided = st === "saved" || st === "uninterested";
  // Clicking the state a card is already in un-decides it — back to review.
  const saveTo = st === "saved" ? "review" : "saved";
  const killTo = st === "uninterested" ? "review" : "uninterested";
  return `<span class="triage${decided ? " decided" : ""}">
    <button type="button" class="tri ok ${st==="saved"?"on":""}"
      aria-label="${st==="saved"?"Move back to review":"Save"}"
      title="${st==="saved"?"Back to review":"Save"}"
      onclick="event.stopPropagation();triageSet('${jsq(kind)}','${jsq(key)}','${saveTo}')">✓</button>
    <button type="button" class="tri no ${st==="uninterested"?"on":""}"
      aria-label="${st==="uninterested"?"Move back to review":"Uninterested"}"
      title="${st==="uninterested"?"Back to review":"Not interested"}"
      onclick="event.stopPropagation();triageSet('${jsq(kind)}','${jsq(key)}','${killTo}')">✕</button>
  </span>`;
}
window.triageSet = async (kind, key, to) => {
  metal(to === "saved" ? "save" : to === "review" ? "accent" : "kill");
  try {
    if(kind === "post"){
      let note = "";
      if(to === "uninterested")
        note = prompt("Why pass? (teaches the scorer — 10 words is plenty)") || "";
      await api("/api/mark", {url: key, status: to, note, date: ""});
    } else if(kind === "person"){
      const [name, company] = key.split("|");
      await api("/api/person-status", {name, company, status: to, date: "", create: true});
    } else if(kind === "tweet"){
      await api("/api/tweet-status", {url: key,
        status: to === "saved" ? "saved" : to === "review" ? "new" : "dismissed"});
      // the card leaves its pile in place — the pane renders once, so a
      // rebuild (which reloads every live embed) is never the answer
      document.querySelectorAll(".twcard").forEach(c => {
        if(c.dataset.tw === key) c.remove(); });
    } else {
      const d = await api("/api/company-mode", {company: key, mode: to});
      const ppl = (d.company || {}).people_cascaded || 0;
      const bits = [];
      if(d.dropped) bits.push(`${d.dropped} saved posting${d.dropped === 1 ? "" : "s"}`);
      if(ppl) bits.push(`${ppl} unworked ${ppl === 1 ? "person" : "people"}`);
      toast((to === "saved" ? "Saved" : to === "review" ? "Back to review"
        : "Uninterested — noted") + (bits.length ? ` — also dropped ${bits.join(" and ")}` : ""));
      setTimeout(softReload, 350);
      return;
    }
    toast(to === "saved" ? "Saved" : to === "review" ? "Back to review" : "Uninterested — noted");
    setTimeout(softReload, 350);
  } catch(e){ toast(String(e.message || e)); }
};
async function _applyPostingStage({url, status, note, date}){
  // Wins should sound like wins — play while the click's audio unlock is
  // still warm; after the reload the context would be muted again.
  if(status === "offer") metal("offer");
  else if(status === "applied" &&
          (DATA.game?.today_applied||0) + 1 === DATA.game?.goal_apply) metal("blast");
  try { await api("/api/mark", {url, status, note: note || "", date: date || ""});
        toast("→ " + status + (date && date !== DATA.built ? " on " + date : ""));
        setTimeout(softReload, 350); }
  catch(e){ toast(String(e.message || e)); }
}

// ---- shared row pieces -------------------------------------------------------
function rowButtons(r){
  if(!LIVE) return "";
  const u = jsq(r.u), c = jsq(r.c);
  return `<div class="rowbtns">
    <button class="btn" onclick="event.stopPropagation();doNote('${u}')">Note</button>
    <button class="btn" onclick="event.stopPropagation();doUndo('${c}')">Undo</button>
  </div>`;
}
window.doNote = async url => {
  const note = prompt("Add a note — what happened, or what to remember:");
  if(!note) return;
  try { await api("/api/note", {url, note}); toast("note saved"); setTimeout(softReload, 350); }
  catch(e){ toast(String(e.message || e)); }
};
window.doPass = async url => {
  const note = prompt("Why pass? (this teaches the scorer — 10 words is plenty)");
  if(note === null) return;
  try { await api("/api/mark", {url, status: "uninterested", note}); toast("uninterested — the scorer learned"); setTimeout(softReload, 350); }
  catch(e){ toast(String(e.message || e)); }
};
window.doMark = async (url, status) => {
  let note = "";
  if(status === "interviewing") note = prompt("What did they say?") || "";
  try { await api("/api/mark", {url, status, note}); toast("→ " + status); setTimeout(softReload, 350); }
  catch(e){ toast(String(e.message || e)); }
};
window.doUndo = async company => {
  try { const d = await api("/api/undo", {company}); toast(d.msg || "undone"); setTimeout(softReload, 350); }
  catch(e){ toast(String(e.message || e)); }
};
window.queueFounder = async (company) => {
  try { const d = await api("/api/queue-founder", {company});
    toast(d.msg || "queued for founder lookup");
  } catch(e){ toast(String(e.message || e)); }
};

const WHYOPEN = new Set();
window.toggleWhy = (id) => {
  if(WHYOPEN.has(id)) WHYOPEN.delete(id); else WHYOPEN.add(id);
  render(); renderCompanies(); renderModalCard();
};
window.sendFeedback = async (url, company, judgment, boxId) => {
  const note = (el(boxId)?.value || "").trim();
  try {
    await api("/api/feedback", {url, company, judgment, note});
    toast("thanks — the scorer will weigh that next run");
    WHYOPEN.clear(); render(); renderCompanies(); renderModalCard();
  } catch(e){ toast(String(e.message || e)); }
};
function whyBox(kind, id, score, why, url, company){
  const open = WHYOPEN.has(id);
  const btn = `<button class="btn" onclick="event.stopPropagation();toggleWhy('${jsq(id)}')">${open?"hide":"Why This Score?"}</button>`;
  if(!open) return btn;
  const fb = LIVE ? `
    <div style="margin-top:8px">
      <b style="font-size:var(--t-xs)">Disagree? Teach it:</b>
      <textarea aria-label="Why the score is off" id="fb-${esc(id)}" placeholder="optional — why the score is off" onclick="event.stopPropagation()"></textarea>
      <div class="rowbtns">
        <button class="btn" onclick="event.stopPropagation();sendFeedback('${jsq(url)}','${jsq(company)}','too_high','fb-${jsq(id)}')">Score too high</button>
        <button class="btn" onclick="event.stopPropagation();sendFeedback('${jsq(url)}','${jsq(company)}','too_low','fb-${jsq(id)}')">Score too low</button>
        <button class="btn" onclick="event.stopPropagation();sendFeedback('${jsq(url)}','${jsq(company)}','right','fb-${jsq(id)}')">About right</button>
      </div>
    </div>` : `<div class="why" style="margin-top:6px">start <code>make app</code> to give feedback the scorer learns from</div>`;
  return btn + `<div class="whybox" onclick="event.stopPropagation()">
    <b>${score ?? "—"}</b> — scored against the CLAUDE.md rubric
    (role shape 35% · stage 25% · industry 20% · location 10% · comp 10%).
    <div class="why" style="margin-top:4px">${esc(why || "no reasoning recorded — likely unscored or imported")}</div>
    ${fb}</div>`;
}

const DESC = {};
function expandedHtml(r){
  const ck = CO_KEY[(r.c||"").toLowerCase().trim()];
  // People at this company, straight from the People section — same cards.
  const ppl = (DATA.people||[]).filter(pv => (pv.company||"").toLowerCase() === r.c.toLowerCase());
  const people = ppl.length
    ? `<div class="pgrid">${ppl.slice(0,6).map(pv => personCard(pv, true)).join("")}</div>`
    : `<div class="descr"><span class="co">no people on file</span></div>`;
  const co = (DATA.companies||[]).find(c=>c.name===r.c) || {};
  // Provenance lives once, in the head's "Posted 2w ago · via …" tag. Only
  // what the head can't say renders down here: extra boards carrying the same
  // role (signal — it's being marketed hard), and built artifacts.
  // Two panels, side by side: the role on the left, the company on the right.
  // The company panel prefers the stored LONG description; the condensed blurb
  // is the concise card's job. When the role text merely repeats the company
  // text (single-source boards), one panel carries both.
  const roleTxt = DESC[r.u] || "";
  const coTxt = co.descfull || co.blurb || "";
  const dupBlurb = coTxt && roleTxt &&
    roleTxt.replace(/\s+/g," ").toLowerCase().includes(coTxt.replace(/\s+/g," ").toLowerCase().slice(0, 120));
  const coPanel = dupBlurb ? "" : `<div><h4>The company</h4>
    <div class="descr">${coTxt ? esc(short(coTxt, 1400)) : '<span class="co">nothing on file — Repopulate Company Details fetches it</span>'}`+
      `${co.funding?` · ${esc(co.funding)}`:""}</div></div>`;
  const rolePanel = `<div><h4>The role</h4><div class="descr">${roleTxt ? esc(short(roleTxt, 1400))
    : '<span class="co">no role text on file — Repopulate Job pulls it from the posting page</span>'}</div></div>`;
  const fitPanel = `<div class="fitbox"><h4>The fit</h4><div class="descr">${r.w ? emph(r.w)
    : '<span class="co">unscored — the fit line arrives with the score</span>'}</div></div>`;
  return `<div class="expgrow${JUST_ROW === r.u ? " anim" : ""}"><div class="expgrowin">
    <div class="exp" onclick="event.stopPropagation()">
    ${r.sn?`<div class="why" style="color:var(--warn);margin-bottom:8px">note: ${esc(r.sn)}</div>`:""}
    ${rolePanel||coPanel||fitPanel?`<div class="pstack">${rolePanel}${coPanel}${fitPanel}</div>`:""}
    <div class="pplbox"><h4>People</h4>${people}</div>
    <div class="exprow">
      ${whyBox("row", "r:"+r.u, r.s, r.w, r.u, r.c)}
      ${LIVE?`<button class="btn" onclick="event.stopPropagation();doNote('${jsq(r.u)}')">Note</button>
      <button class="btn autobtn" onclick="event.stopPropagation();jobAutofill('${jsq(r.u)}')">Repopulate Job</button>`:""}
      ${(r.hist||[]).length?`<button class="btn histbtn" onclick="event.stopPropagation();openHistory('${jsq(r.u)}')">History (${r.hist.length})</button>`:""}
      ${LIVE && r.st === "interviewing" ? `<select aria-label="Interview stage" class="ivsel"
        onclick="event.stopPropagation()" onchange="setInterviewStage('${jsq(r.u)}', this.value)">
        <option value="">Interview stage…</option>
        ${["screen","take-home","technical","onsite","final"].map(v =>
          `<option value="${v}" ${r.ivs===v?"selected":""}>${tc(v.replace("-"," "))}</option>`).join("")}
      </select>` : ""}
    </div>
  </div></div></div>`;
}
window.showMore = () => { shown += PAGE; render(); };
window.clearFilters = () => {
  // The old "status" select is gone; guard every id so a renamed control can
  // never take the whole click path down with a null TypeError again.
  ["q","source","city","posted","industry","round","rtype","yoereq","hascontact","talkstage"].forEach(id => {
    const n = el(id); if(n) n.value = "";
  });
  POSTING_MODE = "review";
  ["ps-review","ps-saved","ps-uninterested"].forEach((id,i) => {
    const n = el(id); if(n){ n.classList.toggle("on", i===0);
      n.setAttribute("aria-pressed", String(i===0)); } });
  if(el("pt-review")) setPeopleMode("review");
  Object.keys(F).forEach(k => F[k] = false);
  document.querySelectorAll(".chip[data-f]").forEach(c => { c.classList.remove("on"); c.setAttribute("aria-pressed","false"); });
  shown = PAGE; markDirty("postings", "companies", "people", "tweets");
};
window.rowKey = (ev, u) => {
  if(ev.key !== "Enter" && ev.key !== " ") return;
  if(ev.target.closest("a,button,input,textarea,select")) return;
  ev.preventDefault();
  toggleRow(u);
};
let JUST_ROW = null;
// One card at a time, in a centered popup over a dimmed page — cards in the
// grids stay concise, the modal holds the open version.
let MODAL_CARD = null;
function renderModalCard(){
  if(!MODAL_CARD) return;
  const {kind, key} = MODAL_CARD;
  let html = "";
  if(kind === "post"){
    const r = ROW_BY_URL[key];
    html = r ? postingCard(r, nonUsSelected() || F.visa, false, true) : "";
  } else if(kind === "co"){
    const c = (DATA.companies||[]).find(x => x.key === key);
    html = c ? companyCard(c, false, true) : "";
  } else {
    const [nm, co] = key.split("|");
    const pv = (DATA.people||[]).find(x => x.name === nm && x.company === co);
    html = pv ? personCard(pv, false, true) : "";
  }
  el("cardhost").innerHTML = html;
}
let MODAL_PAGE = {post: 0, ppl: 0};
window.modalPage = (k, d) => {
  MODAL_PAGE[k] = Math.max(0, MODAL_PAGE[k] + d);
  renderModalCard();
};
function sectionPager(kind, page, pages){
  if(pages <= 1) return "";
  return `<span class="kbpager">
    <button type="button" class="kbpg" aria-label="Previous page" ${page===0?"disabled":""}
      onclick="event.stopPropagation();modalPage('${kind}',-1)">‹</button>
    <span class="co">${page+1}/${pages}</span>
    <button type="button" class="kbpg" aria-label="Next page" ${page===pages-1?"disabled":""}
      onclick="event.stopPropagation();modalPage('${kind}',1)">›</button></span>`;
}
const SEEN = new Set((() => {
  try { return JSON.parse(localStorage.getItem("seen-cards") || "[]"); }
  catch(_){ return []; } })());
function markSeen(kind, key){
  const k = kind + "|" + key;
  if(SEEN.has(k)) return;
  SEEN.add(k);
  try { localStorage.setItem("seen-cards", JSON.stringify([...SEEN].slice(-2000))); } catch(_){}
  const sel = kind === "post" ? `.ccard[data-u="${CSS.escape(key)}"]`
                              : `.ccard[data-k="${CSS.escape(key)}"]`;
  document.querySelectorAll(sel).forEach(c => c.classList.add("seen"));
}
window.openCard = async (kind, key) => {
  if(kind === "post" || kind === "company") markSeen(kind, key);
  MODAL_CARD = {kind, key};
  MODAL_PAGE = {post: 0, ppl: 0};
  renderModalCard();
  modalOpener = document.activeElement;
  el("cardmodal").classList.add("show");
  if(kind === "post" && LIVE && DESC[key] === undefined){
    try {
      const d = await api("/api/row?url=" + encodeURIComponent(key));
      DESC[key] = d.desc || "";
    } catch(_){ DESC[key] = ""; }
    if(MODAL_CARD && MODAL_CARD.key === key) renderModalCard();
  }
};
window.toggleRow = (u) => openCard("post", u);

// ---- speed triage: clear the review pile without ever touching the mouse.
// Decisions hit /api/mark immediately but the page reloads ONCE, on exit —
// a reload per verdict would make a 100-card session unbearable.
let TRIAGE = null;
let LAST_ROWS = [];
window.startTriage = () => {
  if(POSTING_MODE !== "swept") setPostingMode("review");
  render();
  const urls = LAST_ROWS.map(r => r.u);
  if(!urls.length){ toast("nothing to triage under these filters"); return; }
  TRIAGE = {urls, i: 0, decided: 0};
  el("triagebar").hidden = false;
  triageShow();
};
function triageShow(){
  const t = TRIAGE;
  if(!t || t.i >= t.urls.length){ endTriage(); return; }
  el("triageprog").textContent = `${t.i + 1} of ${t.urls.length} — ${t.decided} decided`;
  openCard("post", t.urls[t.i]);
}
function endTriage(){
  const decided = TRIAGE ? TRIAGE.decided : 0;
  TRIAGE = null;
  el("triagebar").hidden = true;
  closeModal();
  if(decided){ toast(`${decided} decided — rebuilding`); setTimeout(softReload, 300); }
}
async function triageDecide(status){
  const t = TRIAGE; if(!t) return;
  const u = t.urls[t.i];
  let note = "";
  if(status === "uninterested"){
    note = prompt("Why pass? (teaches the scorer — 10 words is plenty)") || "";
    if(note === "" && !confirm("Pass without a reason? A reason teaches the scorer.")) return;
  }
  try {
    await api("/api/mark", {url: u, status, note});
    metal(status === "saved" ? "save" : "kill");
    const r = ROW_BY_URL[u]; if(r) r.st = status;
    t.decided++; t.i++;
    triageShow();
  } catch(e){ toast(String(e.message || e)); }
}
document.addEventListener("keydown", ev => {
  if(!TRIAGE) return;
  if(ev.target.closest?.("input,textarea,select")) return;
  if(ev.key === "s"){ ev.preventDefault(); triageDecide("saved"); }
  else if(ev.key === "x" || ev.key === "u"){ ev.preventDefault(); triageDecide("uninterested"); }
  else if(ev.key === " " || ev.key === "ArrowRight"){ ev.preventDefault(); TRIAGE.i++; triageShow(); }
  else if(ev.key === "Escape"){ ev.preventDefault(); endTriage(); }
});

// Display-only: all-lowercase company names from the boards title-case on
// screen ("town" → "Town"); mixed-case names (InstaLily.AI, 14.ai) pass through.
function coDisp(name){
  return (name||"").split(/\s+/).map(w =>
    /^[a-z]/.test(w) && w === w.toLowerCase() ? w.charAt(0).toUpperCase()+w.slice(1) : w).join(" ");
}
function famTags(name, alumni){
  const tags = [...new Set((alumni||[]).map(x => x.split(":")[0].trim()))].filter(Boolean);
  // Sports rides alongside the schools rather than inside them: two SCHOOLS
  // collapse to one "Alumni" chip, and a sports chip must not be counted in
  // that collapse or it would read as a third school.
  const schools = tags.filter(x => ALUM_SIGNALS.includes(x));
  const fams = famChips(tags);
  const warm = CO_WARM.has((name||"").toLowerCase().trim());
  const sch = schools.length >= 2 ? `<span class="sig sig-alumni" title="Alumni — multiple schools" role="img" aria-label="Alumni">${gw("🎓", "Alumni")}</span>`
    : schools.map(schoolChip).join("");
  return sch + fams + (!schools.length && !fams && warm
    ? `<span class="warmtag sig" title="Warm lead" role="img" aria-label="Warm">${gw("🔥", "Warm")}</span>` : "");
}
// Funding-watch entries carry their why as a title ("No posting yet — …");
// display them as what they are: an outreach play at the company.
const isPlay = r => /^no posting yet/i.test(r.t||"");
const dispTitle = r => isPlay(r) ? `Outreach — ${coDisp(r.c)}` : r.t;
// One posting card, shared by the Postings tab and expanded company views.
function postingCard(r, showVisa, nested, forceOpen, kb){
  const open = !!forceOpen;                       // grids stay concise
  const age = postedAge(r);
  // Fresh find inside an expanded company: review-pile posting first seen
  // in the last week gets the pulsing edge instead of a count chip.
  const fresh = !forceOpen && r.st === "review" && daysAgo(r.fs) !== null && daysAgo(r.fs) <= 7;
  const act = forceOpen ? "" : nested ? `gotoPosting('${jsq(r.u)}')` : `rowTap(event,'${jsq(r.u)}')`;
  // Same language as companies: the card wears its company's industry wash,
  // the modal frames itself in the darkened cut of that hue.
  const ind = CO_IND[(r.c||"").toLowerCase().trim()] || "";
  const edge = open ? `;border-color:hsl(${indHue(ind)},36%,33%)` : "";

  const fam = famTags(r.c, CO_ALUM[r.c]);
  const miss = [];
  const need = (sh, full, act) => open ? missBtn(full, act) : (miss.push([sh, full, act]), "");
  const cityChip = r.city && r.city!=="Other"?`<button type="button" class="cchip citychip qedit"
    style="color:${cityTone(r.city)};background:${toneWash(cityTone(r.city))}"
    onclick="event.stopPropagation();postingEdit('${jsq(r.u)}','location')"
    title="${esc(r.loc || r.city)}">${gw(cityAbbr(r.city), r.city)}</button>`
    : need("Loc", "Location", `postingEdit('${jsq(r.u)}','location')`);
  const ck = CO_KEY[(r.c||"").toLowerCase().trim()];
  const ck2 = (r.c||"").toLowerCase().trim();
  const sg = r.s!=null && r.s>=60 ? Math.round(45 + (Math.min(r.s,100)-60)*2.25) : null;
  const ring = `<span class="cscore ${sg!==null?"sgrad":cls(r.s)}"
    style="${sg!==null?`--sh:${sg};`:""}background:conic-gradient(currentColor ${(r.s||0)*3.6}deg, var(--line) 0)">
    <span class="cscorein">${r.s ?? "—"}</span></span>`;
  const indChipHtml = ind?`<button type="button" class="ind qedit" style="${indChip(ind)}"
    title="${esc(tc(ind))}"
    onclick="event.stopPropagation();quickEdit('${jsq(r.c)}','industry')">${esc(shortLabel(tc(ind)))}</button>`
    : need("Ind", "Industry", `quickEdit('${jsq(r.c)}','industry')`);
  const tags = fam;
  const roleChipHtml = r.cat && r.cat!=="Other"
    ? `<button type="button" class="ind rolechip qedit" style="${roleChip(r.cat)}"
       title="${esc(r.cat)}"
       onclick="event.stopPropagation();postingEdit('${jsq(r.u)}','role_type')">${esc(shortLabel(r.cat))}</button>`
    : need("Role", "Role Type", `postingEdit('${jsq(r.u)}','role_type')`);
  const placeTags = `${indChipHtml}${roleChipHtml}${cityChip}`;
  // hoisted out of the template so the pill sees every miss before it renders
  const stageChip = CO_STAGE[ck2]
    ? `<button type="button" class="cchip qedit" title="${esc(tc(roundGroup(CO_STAGE[ck2]).replace("_"," ")))}"
       onclick="event.stopPropagation();quickEdit('${jsq(r.c)}','stage')">${gw(stageShort(tc(roundGroup(CO_STAGE[ck2]).replace("_"," "))), tc(roundGroup(CO_STAGE[ck2]).replace("_"," ")))}</button>`
    : need("Stage", "Stage", `quickEdit('${jsq(r.c)}','stage')`);
  const siteChip = CO_SITE[ck2]
    ? `<a class="cchip linkchip" href="${esc(CO_SITE[ck2])}" target="_blank" rel="noopener" title="${esc(CO_SITE[ck2])}" onclick="event.stopPropagation()">🔗</a>`
    : need("Site", "Website", `quickEdit('${jsq(r.c)}','site')`);
  return `
    <div class="ccard ${open?"open":""} ${!nested&&POSTSEL.has(r.u)?"sel":""} ${nested&&r.st==="uninterested"?"ghost":""} ${fresh?"freshpost":""} ${r.st==="offer"?"offercard":""} ${kb?"kbdrag":""} ${SEEN.has("post|"+r.u)?"seen":""}" data-u="${esc(r.u)}" role="button" tabindex="0" aria-expanded="${open}"
         ${kb?`draggable="true" ondragstart="kbDrag(event,'post','${jsq(r.u)}')" ondragend="kbDragEnd(event)"`:""}
         style="${indGrad(ind, r.cat)}${edge};--indh:${indHue(ind)}"
         onclick="${act}"
         onkeydown="${forceOpen?"":nested?`if(event.key==='Enter'){${act}}`:`rowKey(event,'${jsq(r.u)}')`}">
      <div class="chead">
        ${kb&&r.st!=="saved"?"":ring}
        <span class="ptitle">${open?(r.u||"").startsWith("manual://")?esc(short(dispTitle(r),52)):`<a href="${esc(r.u)}" target="_blank" rel="noopener"
          onclick="event.stopPropagation()">${esc(short(dispTitle(r),52))}</a>`
          :`${esc(short(dispTitle(r),52))}${isPlay(r)?"":` <span class="tsep">·</span> <button type="button" class="colink pcobig pcosm"
             onclick="event.stopPropagation();${ck?`gotoCompany('${jsq(ck)}')`:""}">${esc(coDisp(r.c))}</button>`}`}</span>
        ${open?`<span class="tsep">·</span>`:""}
        ${open?`<button type="button" class="colink pcobig"
          onclick="event.stopPropagation();${ck?`gotoCompany('${jsq(ck)}')`:""}">${esc(coDisp(r.c))}</button>`:""}
        ${r.eh?'<span class="eh">⚑</span>':""}
        ${open?"":`<span class="sigs">${tags}</span>`}
      </div>
      ${r.w&&!open&&!kb?`<div class="why">${emph(short(r.w, 520))}</div>`:""}
      ${kb?"":intelStrip(r)}
      <div class="cfoot">
        ${age!==null&&!kb?(()=>{ const hue = Math.max(4, 135 - age*5.5);
          const ago2 = age===0?"today":age+" days ago";
          return `<span class="cchip agechip" style="--ageh:${hue}"
          title="${r.p?`Posted ${ago2} — date published by the board`
                     :`First seen ${ago2} — no posting date published, this is when the scout first saw it`}">${
          r.p?CAL_SVG:EYE_SVG}<span class="pplnum">${age===0?"today":age+"d"}</span>${
          open?`<span class="agevia">· via ${esc(srcDisp(r.src||""))}</span>`:""}</span>`; })():""}
        ${placeTags}
        ${open?tags:""}
        ${stageChip}
        ${kb?"":siteChip}
        ${r.sal&&!kb?`<span class="cchip salchip" title="${esc(r.sal)}">${esc(open?r.sal:salShort(r.sal))}</span>`:""}
        ${kb?"":atsChip(r)}
        ${yoeChip(r)}
        ${r.ap&&!kb?`<span class="cchip freshroles">Applied ${esc(ago(r.ap))}</span>`:""}
        ${showVisa ? (r.visa===true?`<span class="cchip visa-yes">Sponsors Visa</span>`:r.visa===false?`<span class="cchip visa-no">No Sponsorship</span>`:"") : ""}
        ${kb?`${r.days!=null?`<span class="cchip${stalled(r.st,r.days)?" stallchip":""}">${r.days}d</span>`:""}${r.ivs?`<span class="cchip">${esc(r.ivs)}</span>`:""}`:""}
        ${kb?"":missPill(miss)}
        ${open||(kb&&r.st!=="saved")?"":triageBtns("post", r.u, r.st)}
      </div>
      ${open?triageBtns("post", r.u, r.st):""}
      ${open ? expandedHtml(r) : ""}
    </div>`;
}

// ---- postings list -----------------------------------------------------------
function render(){
  const msEl0 = el("minscore");
  // active only above the floor — the floor itself means "no score filter"
  const MINSC = msEl0 && +msEl0.value > +msEl0.min ? +msEl0.value : 0;
  const q = el("q").value.trim().toLowerCase();
  const sort = el("sort").value, posted = el("posted").value;
  // Visa only matters abroad — the chip appears with a non-US city.
  const vchip = document.querySelector('[data-f="visa"]');
  if(vchip && TAB === "postings") vchip.style.display = nonUsSelected() ? "" : "none";
  const showVisa = nonUsSelected() || F.visa;
  // One predicate, one escape hatch: `skip` lifts a single dimension so
  // each dropdown can count what the OTHER filters leave (Eric, 2026-08-13:
  // "warm + city should show the actual entries").
  const pass = (r, skip) => {
    if(skip !== "source" && MSEL.src.size && !MSEL.src.has(r.src)) return false;
    if(skip !== "city" && MSEL.city.size
       && !(MSEL.city.has(r.city) || (MSEL.city.has("__none") && !r.loc))) return false;
    if(skip !== "posted" && posted){
      const age = postedAge(r);
      if(age===null || age > +posted) return false;
    }
    if(skip !== "score" && MINSC && (r.s || 0) < MINSC) return false;
    const rInd = CO_IND[(r.c||"").toLowerCase().trim()] || "";
    if(skip !== "industry" && MSEL.ind.size
       && !(MSEL.ind.has(rInd) || (MSEL.ind.has("__none") && !rInd))) return false;
    if(skip !== "round" && MSEL.rnd.size && !MSEL.rnd.has(roundGroup(r.stg))) return false;
    if(skip !== "yoereq" && YQ.size){
      // disjoint buckets, multi-select OR; a stated value is required for
      // the numbered ones — same honesty rule the old dropdown had
      if(!YQ.has(yqBucket(r))) return false;
    }
    if(F.warm && !CO_WARM.has((r.c||"").toLowerCase().trim())) return false;
    if(F.sports && !CO_SPORTS.has((r.c||"").toLowerCase().trim())) return false;
    if(F.music && !CO_MUSIC.has((r.c||"").toLowerCase().trim())) return false;
    if(F.research && !CO_RESEARCH.has((r.c||"").toLowerCase().trim())) return false;
    if(skip !== "rtype" && MSEL.cat.size && !MSEL.cat.has(r.cat)) return false;
    if(skip !== "pmiss" && MSEL.pmiss.size){
      const gaps = {unscored: r.s === null, location: !r.loc, industry: !rInd,
                    stage: !roundGroup(r.stg), role: !r.cat || r.cat === "Other"};
      if(![...MSEL.pmiss].some(k => gaps[k])) return false;
    }
    if(isPlay(r)) return false;   // funding plays live on the Companies tab
    if(F.visa && r.visa!==true) return false;
    if(F.followedco && !FOLLOWED.has(r.c.toLowerCase())) return false;
    if(q){
      const hay = `${r.t} ${r.c} ${r.w} ${r.loc} ${(r.fs_all||[]).map(f=>f.n).join(" ")}`.toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;
  };
  let rows = DATA.rows.filter(r => pass(r));
  const bN = {review:0, saved:0, uninterested:0, expired:0, incomplete:0};
  rows.forEach(r => bN[bucketOf(r.st)]++);
  el("ps-review").textContent = `Review (${bN.review})`;
  el("ps-saved").textContent = `Saved (${bN.saved})`;
  el("ps-uninterested").textContent = `Uninterested (${bN.uninterested})`;
  requestAnimationFrame(moveModeSlides);   // count text changes the chip widths
  // Tab badge, same idea as the tweets one: how big is the untriaged pile.
  // Computed off the whole feed, NOT off `rows` — a filter chip must not make
  // the badge shrink, or it stops meaning "how much is left".
  const pbadge = el("pon");
  if(pbadge){
    const nrev = DATA.rows.filter(r => bucketOf(r.st) === "review").length;
    pbadge.textContent = nrev > 999 ? (nrev/1000).toFixed(1) + "k" : String(nrev);
    pbadge.hidden = !nrev;
  }
  const qn = ((DATA.diag||{}).alumni_q||0) + ((DATA.diag||{}).person_fill_q||0);
  el("queue-btn").textContent = qn ? `Queue (${qn})` : "Queue";
  el("queue-btn").classList.toggle("hasq", qn > 0);
  for(const m of ["review","saved","uninterested"]){
    el("ps-"+m).classList.toggle("empty0", !bN[m]);
    el("ps-"+m).disabled = !bN[m] && POSTING_MODE !== m;
  }
  // Auto-Swept is the "system removed it, not Eric" lens: aged-out review
  // (r.sw) and dead links (expired) in one pile (Eric, 2026-08-18 —
  // "combine expired and auto swept"). Off, both stay invisible.
  rows = rows.filter(r => F.swept ? (r.sw || bucketOf(r.st) === "expired")
                                  : bucketOf(r.st) === POSTING_MODE);
  // Dropdown counts answer "under the OTHER active filters + this pile",
  // with each dropdown's own choice lifted so switching stays possible.
  const inPile = r => F.swept ? (r.sw || bucketOf(r.st) === "expired")
                              : bucketOf(r.st) === POSTING_MODE;
  const fc = skip => DATA.rows.filter(r => inPile(r) && pass(r, skip));
  const tally = (arr, key) => { const n = {};
    arr.forEach(r => { const k = key(r); if(k) n[k] = (n[k]||0)+1; }); return n; };
  const rIndOf = r => CO_IND[(r.c||"").toLowerCase().trim()] || "";
  const cityRows = fc("city"), indRows = fc("industry"), postedRows = fc("posted");
  setFacetCounts("city", tally(cityRows, r => r.city), cityRows.filter(r => !r.loc).length);
  setFacetCounts("ind", tally(indRows, rIndOf), indRows.filter(r => !rIndOf(r)).length);
  setFacetCounts("rnd", tally(fc("round"), r => roundGroup(r.stg)));
  setFacetCounts("src", tally(fc("source"), r => r.src));
  setFacetCounts("cat", tally(fc("rtype"), r => r.cat));
  const pmRows = fc("pmiss"), pmC = {unscored:0, location:0, industry:0, stage:0, role:0};
  pmRows.forEach(r => { if(r.s === null) pmC.unscored++; if(!r.loc) pmC.location++;
    if(!rIndOf(r)) pmC.industry++; if(!roundGroup(r.stg)) pmC.stage++;
    if(!r.cat || r.cat === "Other") pmC.role++; });
  setFacetCounts("pmiss", pmC);
  updateClearBtn();
  // the slider spans the data: floor = lowest score in this pile with the
  // score filter lifted — a track starting at 0 wasted its length (Eric)
  const loScores = fc("score").map(r => r.s).filter(s => s !== null);
  const lo = loScores.length ? Math.floor(Math.min(...loScores) / 5) * 5 : 0;
  if(msEl0 && +msEl0.min !== lo){
    const wasFloor = +msEl0.value <= +msEl0.min;
    msEl0.min = lo;
    if(wasFloor || +msEl0.value < lo){ msEl0.value = lo;
      el("minscore-lbl").textContent = "Score"; }
  }
  const yqC = tally(fc("yoereq"), yqBucket);
  [["le1","≤1 yr"],["mid","2–3 yrs"],["hi","4+ yrs"],["none","No req"]].forEach(([k,lbl]) => {
    const s = el("yq-" + k + "-lbl"); if(!s) return;
    s.textContent = `${lbl} (${yqC[k] || 0})`;
    s.classList.toggle("empty0", !yqC[k]); });
  yqButtonLabel();
  [...el("posted").options].forEach(o => { if(o.value)
    o.text = o.text.replace(/\s*\(\d+\)$/, "") + ` (${postedRows.filter(r => {
      const a = postedAge(r); return a != null && a <= +o.value; }).length})`; });
  LAST_ROWS = rows;
  rows.sort((a,b)=>{
    if(sort==="company") return (a.c||"").localeCompare(b.c||"");
    if(sort==="touch") return (b.touch||"").localeCompare(a.touch||"");
    if(sort==="posted") return (b.p||b.fs||"").localeCompare(a.p||a.fs||"");
    return (b.s===null?-1:b.s) - (a.s===null?-1:a.s);
  });

  const total = rows.length;
  if(shown > total) shown = Math.max(PAGE, Math.ceil(shown/PAGE)*PAGE);
  const page = rows.slice(0, shown);

  ROW_KEYS = total ? page.map(r => r.u) : [];
  el("list").innerHTML = `<div class="pmason">` + (total ? mason(page.map(r => postingCard(r, showVisa)), hostW("list"))
    : `<div class="empty" style="grid-column:1/-1">No postings match these filters.<br><button class="btn" style="margin-top:10px" onclick="clearFilters()">Clear filters</button></div>`)
    + `</div>`
    + (total > shown
        ? `<div style="text-align:center;padding:14px"><button class="btn" onclick="showMore()">`
         + `Show ${Math.min(PAGE, total-shown)} More <span class="co">· ${shown} of ${total} shown</span>`
         + `</button></div>`
        : total > PAGE ? `<div class="why" style="text-align:center;padding:10px">all ${total} shown</div>` : "");
  observeCards("list");
  updateBulk();
  // rows.slice(0, shown) drives the page; grid + expand share the ccard pattern

  document.title = `SteinJobs — ${total} postings`;
}

// ---- tracker -------------------------------------------------------------------
// The Today sections came and went inside a week — all that survives is the
// weekly connections nag, one slim dashed bar nobody can mistake for work.
function todayPanel(){
  return "";  // demo fork: no connections export to nag about
  const d = DATA.diag || {}; const cd = d.conn_days;
  let bars = "";
  if(cd === null || cd === undefined || cd > 7)
    bars += `<button type="button" class="connbar"
      onclick="window.open('https://www.linkedin.com/mypreferences/d/download-my-data','_blank')">
      Re-import LinkedIn connections — ${cd == null
        ? "no export on file; request one, then drop it at data/connections.csv"
        : cd + "d old; request a fresh export, then replace data/connections.csv"}</button>`;
  // The scoring banner retired entirely (Eric, 2026-08-18): the API is
  // parked by design and the in-session pass is a conversation away — a
  // permanent nag about a deliberate state is noise. unscored_review still
  // rides the diag payload for whoever asks.
  return bars;
}
function statTiles(){
  // One row: compact daily goals on the left, a divider, then the lifetime
  // scoreboard as matching tiles. Same box language, different jobs.
  const g = DATA.game || {};
  const pct = (a,b) => b ? Math.round(100*a/b) + "%" : "";
  // A tile that flips green PULSES once — compared against what it looked
  // like last render (localStorage, keyed by day, so tomorrow resets).
  const hits = {a: g.today_applied>=g.goal_apply, r: g.today_reached>=g.goal_reach,
                w: (g.week_hits||0)>=5, s: g.streak>0};
  let was = {};
  try { was = JSON.parse(localStorage.getItem("goalstate:" + DATA.built) || "{}"); } catch(_){}
  try { localStorage.setItem("goalstate:" + DATA.built, JSON.stringify(hits)); } catch(_){}
  const fresh = k => hits[k] && !was[k] ? " pulse" : "";
  const tile = (num, label, hit, extra, frac) => {
    // A goal tile in progress fills from the floor; a hit one goes solid.
    const fill = !hit && frac > 0 ? ` style="background:linear-gradient(to top,` +
      `rgba(79,107,56,.30) ${Math.min(100,Math.round(frac*100))}%,var(--card) 0)"` : "";
    return `<div class="tile ${hit?"goal-hit":""}${extra||""}"${fill}><div class="tnum">${num}</div>
      <div class="tlabel">${esc(label)}</div></div>`;
  };
  const flame = (g.streak||0) >= 5 ? " 🔥" : "";
  const goals =
    tile(`${g.today_applied||0}<span class="co">/${g.goal_apply}</span>`, "Applied Today", hits.a, fresh("a"), (g.today_applied||0)/(g.goal_apply||1))
    + tile(`${g.today_reached||0}<span class="co">/${g.goal_reach}</span>`, "Reached Out Today", hits.r, fresh("r"), (g.today_reached||0)/(g.goal_reach||1))
    + tile(`${g.week_hits||0}<span class="co">/5</span>`, "Goal Days / Wk", hits.w, fresh("w"), (g.week_hits||0)/5)
    + tile(`${g.streak||0}${flame}`, "Day Streak", hits.s, fresh("s"), 0);
  const rate = (a,b) => b ? `<span class="co"> · ${pct(a,b)}</span>` : "";
  // Zero tiles dim so the row reads at a glance as "what's actually moving".
  const mk = rows => rows.map(([v, n, l]) => tile(n, l, false, v ? "" : " zero")).join("");
  // LIVE state, not history (Eric, 2026-08-18): the old lifetime counts came
  // from append-only history, so taking a card OFF a board never moved them.
  // These are the same numbers the board columns show.
  const stC = s => DATA.rows.filter(r => r.st === s).length;
  const ppC = s => (DATA.people||[]).filter(p => p.st === s).length;
  const appliedFam = stC("applied") + stC("interviewing") + stC("offer") + stC("rejected");
  const talkFam = ppC("contacted") + ppC("conversation") + ppC("met");
  const lifePost = mk([
    [stC("saved"), `${stC("saved")}`, "Saved"],
    [stC("applied"), `${stC("applied")}`, "Applied"],
    [stC("interviewing"), `${stC("interviewing")}${rate(stC("interviewing"),appliedFam)}`, "Interviewing"],
    [stC("offer"), `${stC("offer")}`, "Offers"],
    [stC("rejected"), `${stC("rejected")}`, "Rejected"],
  ]);
  const lifePpl = mk([
    [ppC("saved"), `${ppC("saved")}`, "Saved"],
    [ppC("contacted"), `${ppC("contacted")}`, "Contacted"],
    [ppC("conversation"), `${ppC("conversation")}${rate(ppC("conversation")+ppC("met"),talkFam)}`, "Conversations"],
    [ppC("met"), `${ppC("met")}`, "Met"],
  ]);
  return `<div class="tilerow">
    <div class="tilegroup"><div class="tglabel">Streaks</div><div class="tiles goals">${goals}</div></div>
    <span class="tilesep" aria-hidden="true"></span>
    <div class="tilegroup"><div class="tglabel">Postings</div><div class="tiles">${lifePost}</div></div>
    <span class="tilesep" aria-hidden="true"></span>
    <div class="tilegroup"><div class="tglabel">Rolodex</div><div class="tiles">${lifePpl}</div></div>
  </div>`;
}

// Every tracker card click-through lands on the real entry, expanded, on its
// own tab — the tracker is the index, the tabs are the detail.
window.gotoPosting = (u) => {
  switchTab("postings"); openCard("post", u);
};
window.gotoCompany = (key) => { switchTab("companies"); openCard("co", key); };
window.gotoPerson = (pid) => {
  closeModal();
  switchTab("people");
  const [nm, co] = pid.split("|");
  const pv = (DATA.people||[]).find(x => x.name === nm && x.company === co);
  setPeopleMode(bucketOf(pv ? pv.st : "review"));
  PEOPLE_EXPANDED.clear(); PEOPLE_EXPANDED.add(pid);
  renderPeople();
  // center once on the next frame and again after the reveal animations
  // settle — a single rAF lost the scroll to the late reflow (Eric,
  // 2026-08-18: "expanded card should be centered or at least very visible")
  const center = () =>
    document.querySelector(`#pane-people .pcard[data-pid="${CSS.escape(pid)}"]`)
      ?.scrollIntoView({block: "center"});
  requestAnimationFrame(() => { center(); setTimeout(center, 400); });
};

let _pendingStage = null;
async function _applyPersonStage({name, company, status, date, method, detail}){
  if(status === "contacted" &&
     (DATA.game?.today_reached||0) + 1 === DATA.game?.goal_reach) metal("blast");
  try { await api("/api/person-status", {name, company, status, date, method, detail});
        toast(name.split(" ")[0] + " → " + status + (method ? " via " + method : "")
              + (date && date !== DATA.built ? " on " + date : ""));
        setTimeout(softReload, 350); }
  catch(e){ toast(String(e.message || e)); }
}
window.personStageDate = async (name, company, status) => {
  const date = prompt("When did this actually happen? (YYYY-MM-DD)");
  if(!date) return;
  try { await api("/api/person-status", {name, company, status, date});
        toast(`${status} recorded ${date}`); setTimeout(softReload, 350); }
  catch(e){ toast(String(e.message || e)); }
};

const KBPAGE = {};
const KB_PAGE_SIZE = 6;
window.kbPage = (key, d) => { KBPAGE[key] = Math.max(0, (KBPAGE[key] || 0) + d); renderTracker(); };
document.addEventListener("animationend", ev => {
  if(ev.animationName === "growrow") ev.target.classList.remove("anim");
});
// Reduced-motion kills the animation, so animationend never fires — a timer
// strips the class either way and the rest state is always plain block.
function stripAnimSoon(){
  setTimeout(() => document.querySelectorAll(".expgrow.anim")
    .forEach(x => x.classList.remove("anim")), 380);
}
function kanban(board, groups, cardFn){
  // Populated columns share the width; empty ones collapse to slim stubs with
  // a vertical label (horizontal labels clipped at stub width — the reason an
  // earlier stub attempt was reverted). Stubs stay live drop targets.
  // Long columns paginate; the sort puts the freshest moves on page one.
  const wide = innerWidth >= 1280;
  // Wider than the old stub board: these are full posting/rolodex cards now.
  const cols = groups.map(([,items]) => items.length ? "minmax(300px,1fr)"
    : wide ? "minmax(110px,.45fr)" : "52px").join(" ");
  const none = groups.every(([,items]) => !items.length);
  if(none)
    return `<div class="kbnone">Nothing here yet — ✓ a ${board==="post"?"posting":"person"} to start the board</div>`;
  return `<div class="kb" style="grid-template-columns:${cols};grid-auto-flow:row">`
    + groups.map(([label, items]) => {
      if(!items.length)
        // Empty columns still take drops — dragging Saved → Applied is the
        // single most common move and Applied usually starts empty.
        return wide
          ? `<div class="kbcol kbempty" data-board="${esc(board)}" data-st="${esc(label)}"
            title="Drag a card here to move it to ${esc(cap(label))}"
            ondragover="kbOver(event,this,'${jsq(board)}')" ondragleave="kbLeave(this)"
            ondrop="kbDrop(event,'${jsq(board)}','${jsq(label)}')">
          <div class="kbhead">${esc(cap(label))} <span class="co">0</span></div></div>`
          : `<div class="kbcol kbempty kbstub" data-board="${esc(board)}" data-st="${esc(label)}"
            title="Drag a card here to move it to ${esc(cap(label))}"
            ondragover="kbOver(event,this,'${jsq(board)}')" ondragleave="kbLeave(this)"
            ondrop="kbDrop(event,'${jsq(board)}','${jsq(label)}')">
          <div class="kbvlabel">${esc(cap(label))} <span class="co">0</span></div></div>`;
      const key = board + ":" + label;
      const pages = Math.ceil(items.length / KB_PAGE_SIZE);
      const page = Math.min(KBPAGE[key] || 0, pages - 1);
      const slice = items.slice(page * KB_PAGE_SIZE, (page + 1) * KB_PAGE_SIZE);
      const pager = pages > 1 ? `<span class="kbpager">
        <button type="button" class="kbpg" aria-label="Previous page of ${esc(label)}"
          ${page===0?"disabled":""} onclick="kbPage('${jsq(key)}',-1)">‹</button>
        <span class="co">${page+1}/${pages}</span>
        <button type="button" class="kbpg" aria-label="Next page of ${esc(label)}"
          ${page===pages-1?"disabled":""} onclick="kbPage('${jsq(key)}',1)">›</button></span>` : "";
      return `<div class="kbcol" data-board="${esc(board)}" data-st="${esc(label)}"
          ondragover="kbOver(event,this,'${jsq(board)}')" ondragleave="kbLeave(this)"
          ondrop="kbDrop(event,'${jsq(board)}','${jsq(label)}')">
        <div class="kbhead">${esc(cap(label))} <span class="co">${items.length}</span>${pager}</div>`
        + slice.map(cardFn).join("") + `</div>`;
    }).join("") + `</div>`;
}
// Drag a card onto another column to move it — the primary way stages change
// on this screen. Real-world stages still get the date popup on drop.
let DRAG = null;
window.kbDrag = (ev, board, key) => {
  DRAG = {board, key};
  ev.dataTransfer.effectAllowed = "move";
  ev.target.classList.add("dragging");
};
window.kbDragEnd = (ev) => { ev.target.classList.remove("dragging"); DRAG = null; };
window.kbOver = (ev, col, board) => {
  if(!DRAG || DRAG.board !== board) return;
  ev.preventDefault(); ev.dataTransfer.dropEffect = "move";
  col.classList.add("dragover");
};
window.kbLeave = (col) => col.classList.remove("dragover");
window.kbDrop = (ev, board, stage) => {
  ev.preventDefault();
  document.querySelectorAll(".dragover").forEach(c => c.classList.remove("dragover"));
  if(!DRAG || DRAG.board !== board) return;
  // the card lands NOW; the rebuild confirms it a beat later
  const cardEl = document.querySelector(".dragging");
  const colEl = ev.currentTarget;
  if(cardEl && colEl && !colEl.contains(cardEl)){
    cardEl.classList.remove("dragging");
    colEl.appendChild(cardEl);
  }
  metal("drop");
  const key = DRAG.key; DRAG = null;
  if(board === "post"){
    if(["applied","interviewing","offer"].includes(stage)){
      _pendingStage = {kind: "posting", url: key, status: stage};
      openDateModal(stage); return;
    }
    let note = "";
    if(stage === "uninterested")
      note = prompt("Why pass? (teaches the scorer — 10 words is plenty)") || "";
    _applyPostingStage({url: key, status: stage, note, date: ""});
  } else {
    const [name, company] = key.split("|");
    if(["contacted","conversation","met"].includes(stage)){
      _pendingStage = {kind: "person", name, company, status: stage};
      openDateModal(stage); return;
    }
    _applyPersonStage({name, company, status: stage, date: ""});
  }
};
const DM_HINTS = {email: "their email address…", linkedin: "uses their profile on file",
  x: "their @handle…", ig: "their @handle…", phone: "their number…", irl: "where you met…"};
// Re-dates the CURRENT stage: same status through /api/person-status with
// the picked date, so "met" can be recorded on the day it actually happened
// (Eric, 2026-08-18). History appends; nothing is rewritten.
window.editStageDate = (name, company, status) => {
  _pendingStage = {kind: "person", name, company, status};
  openDateModal(status);
};
window.recordNudge = async (name, company) => {
  try {
    await api("/api/nudge", {name, company});
    toast("nudge logged — day-4/day-11 is the whole budget");
    setTimeout(softReload, 350);
  } catch(e){ toast(String(e.message || e)); }
};
function openDateModal(stage){
  el("dm-title").textContent = `${cap(stage)} — when?`;
  el("dm-date").value = DATA.built;
  const wantsMethod = _pendingStage && _pendingStage.kind === "person" && stage === "contacted";
  el("dm-methodrow").hidden = !wantsMethod;
  if(wantsMethod){ el("dm-method").value = "email"; el("dm-detail").value = "";
    el("dm-detail").placeholder = DM_HINTS.email; }
  modalOpener = document.activeElement;
  el("datemodal").classList.add("show");
  el("dm-date").focus();
}
// Mirrors STALE_AFTER in status.py — the board flags what `make today` flags.
const STALE_AFTER = {saved: 7, applied: 10, interviewing: 4, contacted: 7, conversation: 7};
const stalled = (st, days) => days != null && STALE_AFTER[st] != null && days >= STALE_AFTER[st];
window.gotoFunded = () => {
  if(!F.funded){
    F.funded = true;
    const c = document.querySelector('.chip[data-f="funded"]');
    if(c){ c.classList.add("on"); c.setAttribute("aria-pressed", "true"); }
    markDirty("companies");
  }
  switchTab("companies");
};
function raisedBoard(){
  // Notice board of fresh raises (Eric 2026-08-11; decluttered 2026-08-18):
  // a headline, not an inventory — 30-day window (the 30-45d tail is past
  // the best-send window anyway), five freshest, the rest one click away
  // behind the Recently Funded lens. Daily EDGAR data, not real time: a
  // Form D can trail the money by 15 days, and SAFEs/foreign rounds never
  // file, so absence from this board proves nothing about a company.
  const rows = (DATA.companies||[]).filter(c => {
    if(c.ni || !c.raised || !c.raised.filed) return false;
    const days = (Date.now() - new Date(c.raised.filed + "T12:00:00")) / 864e5;
    return days >= 0 && days <= 30;
  }).sort((a,b) => b.raised.filed.localeCompare(a.raised.filed));
  if(!rows.length) return "";
  const shownRows = rows.slice(0, 5), extra = rows.length - shownRows.length;
  const card = c => {
    const f = (c.founders||[])[0] || {};
    return `<div class="rbcard" role="button" tabindex="0"
        onclick="gotoCompany('${jsq(c.key)}')"
        onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();gotoCompany('${jsq(c.key)}')}">
      <span class="rbname">${esc(coDisp(c.name))}</span>
      ${raisedChip(c)}
    </div>`;
  };
  return `<h2 class="tsec"><span>Just Raised <span class="co">· ${rows.length}</span></span></h2>
    <div class="rboard">${shownRows.map(card).join("")}${extra > 0
      ? `<button type="button" class="rbcard rbmore" onclick="gotoFunded()"
           title="All recent raises — Companies with the Recently Funded filter on">+${extra} more</button>` : ""}</div>`;
}
function renderTracker(){
  const pri = DATA.sendpri || {};
  // Freshest move first — "what did I just touch" beats fit ranking here.
  const stamp = r => r.ap || r.touch || "";
  const bySt = st => DATA.rows.filter(r => r.st === st)
      .sort((a,b) => stamp(b).localeCompare(stamp(a)) || (pri[b.u]||0)-(pri[a.u]||0) || (b.s||0)-(a.s||0));
  // One card definition, two surfaces (Eric, 2026-08-07): the board shows the
  // SAME posting card the Postings tab shows, minus the fit line — the board
  // answers "where is it", not "how good is it". It used to be a bespoke stub
  // that drifted from the real card every time either one changed.
  const kbPosting = r => postingCard(r, false, true, false, true);
  const postingGroups = ["saved","applied","interviewing","offer","rejected","dormant"]
    .map(st => [st, bySt(st)]);
  // Same deal for people: the board card IS the rolodex card in its
  // All-Names shape, so a rolodex tweak lands on the tracker unedited.
  const kbPerson = pv => personCard(pv, true, false, true);
  const peopleGroups = ["saved","contacted","conversation","met","dormant"].map(st => {
    const ppl = (DATA.people||[]).filter(pv => (pv.st||"review") === st);
    ppl.sort((a,b) => (b.std||"").localeCompare(a.std||""));
    return [st, ppl];
  });
  // Companies saved: the SAME cards as the Companies tab, newest save first.
  // Condensed on the board (Eric, 2026-08-07): name + chips, no blurb. The
  // strip is a glance at what you're following, not a place to read about them.
  // The saved-companies strip retired 2026-08-18 (Eric): it duplicated the
  // Companies tab's Saved mode card-for-card, and the tracker's job is the
  // two boards. Saved companies live one tab over.
  el("tracker").innerHTML =
    todayPanel()
    + statTiles()
    + raisedBoard()
    + `<h2 class="tsec"><span>Postings</span></h2>` + kanban("post", postingGroups, kbPosting)
    + `<h2 class="tsec"><span>Rolodex</span></h2>` + kanban("ppl", peopleGroups, kbPerson);
}

// ---- companies grid -------------------------------------------------------------
let JUST_CO = null;
window.toggleCompany = (key) => openCard("co", key);
window.doInterest = async (name, restore) => {
  try { await api("/api/company-interest", {company: name, interested: restore});
        toast(restore ? "restored " + name : "hidden — see the Dismissed chip");
        setTimeout(softReload, 350); }
  catch(e){ toast(String(e.message || e)); }
};
window.companyMode = async (sel, name) => {
  try { await api("/api/company-mode", {company: name, mode: sel.value});
        toast(name + " → " + sel.value);
        setTimeout(softReload, 350); }
  catch(e){ toast(String(e.message || e)); }
};
window.doFollow = async (name, on) => {
  try {
    if(on){ await api("/api/track", {company: name, why: ""}); toast("now following " + name); }
    else  { await api("/api/company-interest", {company: name, interested: true}); toast("unfollowed " + name); }
    setTimeout(softReload, 350);
  } catch(e){ toast(String(e.message || e)); }
};
function bestWhy(c){
  const r = DATA.rows.find(x=>x.u===c.best_url);
  return r ? r.w : "";
}
function companyExpanded(c){
  const grow = JUST_CO === c.key ? " anim" : "";
  const id = "c:"+c.key;
  // Real posting cards, same component as the Postings tab; the plain link
  // list survives only for postings that never made it into the page rows.
  const rowsHere = (c.postings || []).map(pp => ROW_BY_URL[pp.url]).filter(Boolean);
  const PPOST = 4;
  const postPages = Math.ceil(rowsHere.length / PPOST);
  const postPage = Math.min(MODAL_PAGE.post, Math.max(0, postPages - 1));
  const posts = rowsHere.length
    ? `<div class="cgrid">${mason(rowsHere.slice(postPage*PPOST, (postPage+1)*PPOST).map(r => postingCard(r, nonUsSelected() || F.visa, true)), Math.min(1000, innerWidth*0.96) - 44)}</div>`
    : (c.postings || []).slice(0, 8).map(pp =>
    `<li><a href="${esc(pp.url)}" target="_blank" rel="noopener">${esc(pp.title)}</a>` +
    `${pp.score !== null ? ` — ${pp.score}` : ""} <span class="co">[${esc(pp.status)}]</span></li>`).join("");
  // Same cards as the People tab — expand, triage, ⌘-select, all of it.
  const ppl = (DATA.people||[]).filter(pv => (pv.company||"").toLowerCase() === c.name.toLowerCase());
  const PPPL = 6;
  const pplPages = Math.ceil(ppl.length / PPPL);
  const pplPage = Math.min(MODAL_PAGE.ppl, Math.max(0, pplPages - 1));
  const founders = ppl.length
    ? `<div class="pgrid">${ppl.slice(pplPage*PPPL, (pplPage+1)*PPPL).map(pv => personCard(pv, true)).join("")}</div>`
    : (c.founders || []).length
    ? (c.founders || []).map(f =>
    `<div class="founder">${esc(f.name)}${f.title?` <span class="co">(${esc(f.title)})</span>`:""}${f.linkedin?` · <a href="${esc(f.linkedin)}" target="_blank" rel="noopener">LinkedIn</a>`:""}</div>`
      ).join("")
    : `<div class="descr"><span class="co">no people on file — Autofill Founders below finds them</span></div>`;
  return `<div class="expgrow${grow}"><div class="expgrowin">
    <div class="exp" onclick="event.stopPropagation()">
    ${c.why?`<div class="why" style="color:var(--warn)">note: ${esc(c.why)}${c.why_date?` <span class="co">(${esc(ago(c.why_date))})</span>`:""}</div>`:""}
    <div class="pplbox"><h4>Company</h4><div class="descr">${(c.descfull||c.blurb)
      ? esc(short(c.descfull||c.blurb, 1400))
      : '<span class="co">nothing on file — Repopulate Company Details fetches it</span>'}</div></div>
    <div class="pplbox"><h4>People ${sectionPager("ppl", pplPage, pplPages)}</h4>${founders}</div>
    ${posts?`<div class="pplbox"><h4>${(c.postings||[]).length} Posting${(c.postings||[]).length===1?"":"s"} ${sectionPager("post", postPage, postPages)}</h4>${rowsHere.length?posts:`<ul class="postlist">${posts}</ul>`}</div>`:""}
    <div class="exprow">
      ${LIVE?`<button class="btn editbtn" style="background:${c.industry?`hsl(${indHue(c.industry)},36%,33%)`:"var(--accent)"};border-color:transparent"
        onclick="event.stopPropagation();editCompany('${jsq(c.key)}')">Edit</button>`:""}
      ${LIVE?`<button class="btn autobtn" onclick="event.stopPropagation();autoComplete('${jsq(c.name)}','company')">Repopulate Company Details</button>`:""}
      ${sweepBtn(c)}
    </div>
  </div></div></div>`;
}
// Cities travel abbreviated (Eric, 2026-08-07): a location chip rides beside
// five other tags, and "SF Bay Area" spelled out is wider than the industry.
// Bucket labels first — that is what postings and companies carry — then a
// light pass for free-text person locations. Remote is a mode, not a city, so
// it keeps its word; the colour still keys off the FULL name, never this.
const CITY_ABBR = {"New York": "🗽", "SF Bay Area": "🌉", "Boston": "🍀",
  "Los Angeles": "LA", "Seattle": "SEA", "International": "INTL",
  "Other US": "US", "Remote": "🌐", "Other": "Other"};
const LOC_ABBR = [
  [/new york|nyc|brooklyn|manhattan/i, "🗽"],
  [/san francisco|bay area|palo alto|mountain view|menlo park|oakland|berkeley/i, "🌉"],
  [/seattle|bellevue|redmond/i, "SEA"],
  [/los angeles|santa monica|culver city/i, "LA"],
  [/boston|cambridge, ?ma/i, "🍀"],
  [/washington, ?d|, ?dc\b/i, "DC"], [/atlanta/i, "ATL"], [/austin/i, "ATX"],
  [/chicago/i, "CHI"], [/denver/i, "DEN"], [/miami/i, "MIA"],
  [/london/i, "🇬🇧 LDN"], [/dublin/i, "🇮🇪 DUB"], [/amsterdam|netherlands/i, "🇳🇱 AMS"],
  [/prague|praha|czech/i, "🇨🇿 PRG"], [/berlin/i, "🇩🇪 BER"], [/munich|münchen/i, "🇩🇪 MUC"],
  [/paris/i, "🇫🇷 PAR"], [/milan|rome|italy/i, "🇮🇹 ITA"],
  [/copenhagen/i, "🇩🇰 CPH"], [/stockholm/i, "🇸🇪 STO"],
  [/remote|anywhere|distributed/i, "🌐"],
];
function cityAbbr(city){
  if(!city) return "";
  if(CITY_ABBR[city]) return CITY_ABBR[city];
  for(const [re, ab] of LOC_ABBR) if(re.test(city)) return ab;
  return short(city, 14);
}
// Everything not on file yet collects into ONE dashed pill on collapsed cards
// (Eric, 2026-08-07). "Website? Industry? Location? Role Type?" was the widest
// run on the page and none of it is data — four borders became one, and the
// per-field click target survives. Expanded cards keep the full words: that is
// where you actually fill them in, and there is room for them.
function missBtn(full, act){
  return `<button type="button" class="cchip missing qedit"
    onclick="event.stopPropagation();${act}">${full}?</button>`;
}
function missPill(items){
  if(!items.length) return "";
  const what = items.map(i=>i[1]).join(", ");
  return `<span class="misspill" role="note" aria-label="Missing: ${what}"
    title="not on file: ${what} — open the card to fill">+${items.length}</span>`;
}
// The five labels wide enough to set a card's width on their own. The full
// name stays in the title — these are read for meaning, not counted.
const LABEL_SHORT = {
  "Technical GTM": "GTM", "Operations": "Ops", "Digital Health": "Health",
  "Sales & BD": "Sales", "People & Talent": "Ppl", "Finance & Legal": "Fin/Legal",
  "Legal & Compliance": "Legal", "Robotics & Hardware": "Robotics",
  "Dev & Data Tools": "Dev/Data", "Founding & Generalist": "Founding",
  "Commerce & CPG": "CPG", "Fitness & Wellness": "Fitness",
  "Bio & Pharma": "Bio", "Food & Ag": "Food/Ag", "Climate & Energy": "Climate",
  "Chief of Staff": "CoS", "Marketing & Growth": "Growth",
  "CoS & Strategy": "CoS", "Product & Design": "Product",
  "Engineering": "Eng", "Marketing": "Mktg"};
const shortLabel = s => LABEL_SHORT[s] || s;
// Posted vs first-seen is a real distinction — a board that publishes a date
// and a scout that guessed from when it showed up are not the same claim — so
// the glyph carries it and the words move into the title.
const CAL_SVG = `<svg class="agesvg" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M4.4 1.1c.45 0 .8.35.8.8v.7h5.6v-.7c0-.45.35-.8.8-.8s.8.35.8.8v.7h.6c1 0 1.8.8 1.8 1.8v8.2c0 1-.8 1.8-1.8 1.8H1.9c-1 0-1.8-.8-1.8-1.8V4.4c0-1 .8-1.8 1.8-1.8h.6v-.7c0-.45.35-.8.8-.8zM1.6 6.5v6.1c0 .17.13.3.3.3h12.1c.17 0 .3-.13.3-.3V6.5z"/></svg>`;
const EYE_SVG = `<svg class="agesvg" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M8 3.1c-3.6 0-6.4 2.6-7.4 4.5a.9.9 0 0 0 0 .8c1 1.9 3.8 4.5 7.4 4.5s6.4-2.6 7.4-4.5a.9.9 0 0 0 0-.8C14.4 5.7 11.6 3.1 8 3.1zm0 7.7a2.8 2.8 0 1 1 0-5.6 2.8 2.8 0 0 1 0 5.6z"/></svg>`;
// The open-roles count, same treatment as the people count: a briefcase and a
// number. "4 Roles | 1 New" was the widest chip on a company card.
const JOB_SVG = `<svg class="jobsvg" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M6.3 1.9h3.4c1 0 1.8.8 1.8 1.8v.9H9.9v-.9a.4.4 0 0 0-.4-.4h-3a.4.4 0 0 0-.4.4v.9H4.5v-.9c0-1 .8-1.8 1.8-1.8z"/><path d="M2.5 5.3h11c1 0 1.7.8 1.7 1.7v5.4c0 1-.8 1.7-1.7 1.7h-11c-1 0-1.7-.8-1.7-1.7V7c0-1 .8-1.7 1.7-1.7z"/></svg>`;
function roleTag(n, fresh){
  if(!n) return "";
  const lbl = `${n} open role${n===1?"":"s"}${fresh?`, ${fresh} new this week`:""}`;
  return `<span class="cchip roletag${fresh?" freshroles":""}" title="${lbl}"
    role="img" aria-label="${lbl}">${JOB_SVG}<span class="pplnum">${n}</span>${
    fresh?`<span class="rnew">+${fresh}</span>`:""}</span>`;
}
// One people count everywhere (Eric, 2026-08-07): a head-and-shoulders glyph
// and the number, never the words. "3 people" spelled out ate a whole chip
// slot on cards already carrying six, and the icon says what the number counts
// in a third of the width. `bare` drops the chip frame for header captions.
const PPL_SVG = `<svg class="pplsvg" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="8" cy="5" r="2.9"/><path d="M2.4 14.4c0-3.2 2.5-5.1 5.6-5.1s5.6 1.9 5.6 5.1z"/></svg>`;
function pplTag(n, bare){
  if(!n) return "";
  const lbl = `${n} ${n===1?"person":"people"}`;
  return `<span class="${bare?"pplbare":"cchip pplchip"}" title="${lbl}"
    role="img" aria-label="${lbl}">${PPL_SVG}<span class="pplnum">${n}</span></span>`;
}
// Stated experience requirement (2026-08-11). Three tones for a 0-1yr
// candidate: <=1 reads as a green light, 2-3 neutral, 4+ a warning. Absent
// when the posting states nothing — no guessing.
function yoeChip(r){
  const n = r.yoe;
  if(n === null || n === undefined) return "";
  const cls = n <= 1 ? " yoe-ok" : n >= 4 ? " yoe-high" : "";
  const label = n === 0 ? "no exp req" : `${n}+ yrs`;
  return `<span class="cchip yoechip${cls}" title="Posting asks for ${
    n === 0 ? "no prior experience" : `~${n}+ years' experience`}">${label}</span>`;
}
// lennysjobs.com steals (Eric approved, 2026-08-18): why-now facts render
// ON the posting card as one muted line — raise recency is the send-timing
// trigger, headcount is stage truth, our open-role count is a hiring-
// velocity proxy. Only facts already on file; absent stays absent.
function intelStrip(r){
  const c = CO_REC[(r.c||"").toLowerCase().trim()];
  if(!c) return "";
  const bits = [];
  if(c.raised && c.raised.filed){
    const d = daysAgo(c.raised.filed);
    if(d !== null && d >= 0){
      const m = c.raised.amount / 1e6;
      const amt = c.raised.amount ? (m >= 1 ? "$" + m.toFixed(1) + "M"
        : "$" + Math.round(c.raised.amount/1e3) + "k") : "";
      bits.push(`raised ${d < 60 ? d + "d" : d < 365 ? Math.round(d/30) + "mo" : Math.floor(d/365) + "y"} ago${amt ? ` (${amt})` : ""}`);
    }
  }
  if(c.headcount) bits.push(`~${c.headcount} ppl`);
  const n = (c.postings||[]).length;
  if(n > 1) bits.push(`${n} roles in the feed`);
  return bits.length
    ? `<div class="intel">${bits.map(esc).join(" · ")}</div>` : "";
}
// Long-form ATS hosts mean 20+ minutes of forms; the quick ones are two.
// Classified ONLY from what the posting URL itself shows — a board page
// that redirects to an ATS makes no claim either way.
const ATS_PAIN = /myworkdayjobs\.com|taleo\.net|icims\.com|successfactors\.com|brassring\.com/i;
function atsChip(r){
  return ATS_PAIN.test(r.u || "")
    ? `<span class="cchip atspain" title="Long-form ATS — budget 20+ minutes to apply">slow apply</span>` : "";
}
function raisedChip(c, open){
  const r = c.raised;
  if(!r || !r.filed) return "";   // no Form D ≠ never raised — render nothing
  const days = Math.floor((Date.now() - new Date(r.filed + "T12:00:00")) / 864e5);
  if(days < 0 || isNaN(days)) return "";
  const m = r.amount / 1e6;
  const amt = r.amount ? (m >= 1 ? "$" + m.toFixed(1) + "M" : "$" + Math.round(r.amount/1e3) + "k") : "";
  const when = days < 60 ? days + "d" : days < 365 ? Math.round(days/30) + "mo" : Math.floor(days/365) + "y";
  const label = "💰 " + (open && amt ? amt + " · " : "") + when;
  const cls = "raisedchip" + (days < 30 ? " hot" : days < 60 ? " fresh" : days <= 120 ? "" : " old");
  const title = "raised" + (amt ? " " + amt : "") + " — SEC Form D filed " + esc(r.filed);
  return r.url
    ? `<a class="${cls}" href="${esc(r.url)}" target="_blank" rel="noopener" title="${title}" onclick="event.stopPropagation()">${esc(label)}</a>`
    : `<span class="${cls}" title="${title}">${esc(label)}</span>`;
}
function companyCard(c, mini, forceOpen, kb){
  const open = !!forceOpen;
  const edge = forceOpen ? `;border-color:hsl(${indHue(c.industry)},36%,33%)` : "";
  const f0 = (c.founders || [])[0];
  const click = forceOpen ? "" : mini ? `gotoCompany('${jsq(c.key)}')` : `coTap(event,'${jsq(c.name)}')`;
  const miss = [];
  const need = (sh, full, act) => open ? missBtn(full, act) : (miss.push([sh, full, act]), "");
  // "Other" is not a place. The posting card has always suppressed it; the
  // company card used to print it 44 times a screen.
  const city0 = ((c.cities||[])[0] === "Other" ? "" : (c.cities||[])[0]) || "";
  const indChipHtml = c.industry
    ? `<button type="button" class="ind qedit" style="${indChip(c.industry)}"
        title="${esc(tc(c.industry))}"
        onclick="event.stopPropagation();quickEdit('${jsq(c.name)}','industry')">${esc(shortLabel(tc(c.industry)))}</button>`
    : need("Ind", "Industry", `quickEdit('${jsq(c.name)}','industry')`);
  const cityChip = city0
    ? `<button type="button" class="cchip citychip qedit"
        style="color:${cityTone(city0)};background:${toneWash(cityTone(city0))}"
        onclick="event.stopPropagation();quickEdit('${jsq(c.name)}','location')"
        title="${esc(city0)}">${gw(cityAbbr(city0), city0)}</button>`
    : need("Loc", "Location", `quickEdit('${jsq(c.name)}','location')`);
  const stageChip = c.stage
    ? `<button type="button" class="cchip qedit" title="${esc(tc(roundGroup(c.stage).replace("_"," ")))}"
       onclick="event.stopPropagation();quickEdit('${jsq(c.name)}','stage')">${gw(stageShort(tc(roundGroup(c.stage).replace("_"," "))), tc(roundGroup(c.stage).replace("_"," ")))}</button>`
    : need("Stage", "Stage", `quickEdit('${jsq(c.name)}','stage')`);
  const siteChip = c.site
    ? `<a class="cchip linkchip" href="${esc(c.site)}" target="_blank" rel="noopener" title="${esc(c.site)}" onclick="event.stopPropagation()">🔗</a>`
    : need("Site", "Website", `quickEdit('${jsq(c.name)}','site')`);
  const dull = !c.industry || !c.blurb;
  return `<div class="ccard ${mini?"mini":""} ${open?"open":""} ${dull?"dull":""} ${SEEN.has("company|"+c.key)?"seen":""} ${!mini&&COSEL.has(c.name)?"sel":""}" data-k="${esc(c.key)}" role="button" tabindex="0"
      style="${dull?"background:var(--card)":indShade(c.industry)}${edge};--indh:${indHue(c.industry)}"
      aria-expanded="${open}"
      onclick="${click}"
      onkeydown="${forceOpen?"":`if(event.key==='Enter'||event.key===' '){event.preventDefault();${mini?click:`toggleCompany('${jsq(c.key)}')`}}`}">
    <div class="chead">
      <span class="cname">${esc(coDisp(c.name))}</span>

      ${open?"":`<span class="sigs">
        ${indChipHtml}
        ${cityChip}
        ${famTags(c.name, c.alumni)}
        ${(c.conn||[]).length?`<span class="sig sig-conn" title="1st-degree LinkedIn connection${c.conn.length>1?"s":""}: ${esc(c.conn.join(", "))}">1st° ${esc(c.conn[0].split(" ")[0])}${c.conn.length>1?` +${c.conn.length-1}`:""}</span>`:""}
      </span>`}
    </div>
    ${open||kb?"":c.blurb?`<div class="why">${esc(short(c.blurb, mini?170:240))}</div>`
      :`<div class="why"><span class="co">no description on file — Repopulate Company Details fetches it</span></div>`}
    <div class="cfoot">
      ${open?`${indChipHtml}${cityChip}`:""}
      ${!open?roleTag((c.postings||[]).length, c.new_posts):""}
      ${stageChip}
      ${raisedChip(c, open)}
      ${siteChip}
      ${c.linkedin?`<a class="cchip linkchip inchip" href="${esc(c.linkedin)}/people/" target="_blank" rel="noopener" title="${esc(c.linkedin)}" onclick="event.stopPropagation()">in</a>`
        :(open?`<button type="button" class="cchip missing qedit" onclick="event.stopPropagation();quickEdit('${jsq(c.name)}','linkedin')">LinkedIn?</button>`:"")}
      ${!open?pplTag(CO_PPL_N[c.name.toLowerCase().trim()]):""}
      ${c.headcount?`<span class="cchip" title="Headcount (from postings/overlay)">${esc(short(String(c.headcount),9))} ppl</span>`:""}
      ${!open&&c.added?`<span class="cchip addedchip" title="Added ${esc(c.added)}">${CAL_SVG}<span class="pplnum">${esc(ago(c.added).replace(" ago",""))}</span></span>`:""}
      ${missPill(miss)}
      ${open?`${famTags(c.name, c.alumni)}
      ${(c.conn||[]).length?`<span class="sig sig-conn">1st° ${esc(c.conn.map(n=>n.split(" ")[0]).join(", "))}</span>`:""}
      <span class="cchip">Added ${c.added?esc(ago(c.added)):"—"}</span>
      <span class="cchip">via ${esc(srcDisp(c.via||""))}</span>
      ${c.edited?`<span class="cchip">Edited ${esc(ago(c.edited))}</span>`:""}
      ${sweepStatus(c)}`:""}
      ${open||mini?"":triageBtns("co", c.name, c.ni?"uninterested":c.tracked?"saved":"review")}
    </div>
    ${!mini&&open?triageBtns("co", c.name, c.ni?"uninterested":c.tracked?"saved":"review"):""}
    ${open ? companyExpanded(c) : ""}
  </div>`;
}
function setCompanyMode(mode){
  COMPANY_MODE = mode;
  for(const [id, m] of [["ct-review","review"],["ct-saved","saved"],["ct-uninterested","uninterested"]]){
    el(id).classList.toggle("on", COMPANY_MODE===m);
    el(id).setAttribute("aria-pressed", String(COMPANY_MODE===m));
  }
  moveModeSlides();
  renderCompanies();
}
for(const [id, m] of [["ct-review","review"],["ct-saved","saved"],["ct-uninterested","uninterested"]])
  el(id).addEventListener("click", ()=>setCompanyMode(m));
function renderCompanies(){
  const q = (el("q").value || "").toLowerCase();
  const coPass = (c, skip) => {
    if(q && !`${c.name} ${c.why} ${c.blurb} ${(c.signals||[]).join(" ")}`.toLowerCase().includes(q)) return false;
    if(skip !== "city" && MSEL.city.size
       && !((c.cities||[]).some(x => MSEL.city.has(x))
            || (MSEL.city.has("__none") && !(c.cities||[]).length))) return false;
    if(skip !== "industry" && MSEL.ind.size
       && !(MSEL.ind.has(c.industry) || (MSEL.ind.has("__none") && !c.industry))) return false;
    if(skip !== "round" && MSEL.rnd.size && !MSEL.rnd.has(roundGroup(c.stage))) return false;
    if(F.warm && !CO_WARM.has(c.name.toLowerCase().trim())) return false;
    if(F.hasroles && !(c.postings||[]).length) return false;
    if(skip !== "cmiss" && MSEL.cmiss.size){
      const city0 = (c.cities||[])[0] === "Other" ? "" : (c.cities||[])[0];
      const gaps = {industry: !c.industry, location: !city0, stage: !c.stage,
                    site: !c.site, desc: !c.blurb};
      if(![...MSEL.cmiss].some(k => gaps[k])) return false;
    }
    if(F.funded){
      const d = daysAgo((c.raised || {}).filed);
      if(d === null || d > 60) return false;
    }
    return true;
  };
  let list = (DATA.companies || []).filter(c => coPass(c));
  // Chip counts answer "under THESE filters", not the whole dataset.
  const base = list;
  const cN = {review: base.filter(c=>!c.ni&&!c.tracked).length,
              saved: base.filter(c=>c.tracked&&!c.ni).length,
              uninterested: base.filter(c=>c.ni).length};
  el("ct-review").textContent = `Review (${cN.review})`;
  el("ct-saved").textContent = `Saved (${cN.saved})`;
  el("ct-uninterested").textContent = `Uninterested (${cN.uninterested})`;
  requestAnimationFrame(moveModeSlides);
  for(const m of ["review","saved","uninterested"]){
    el("ct-"+m).classList.toggle("empty0", !cN[m]);
    el("ct-"+m).disabled = !cN[m] && COMPANY_MODE !== m;
  }
  list = COMPANY_MODE==="uninterested" ? base.filter(c => c.ni)
    : COMPANY_MODE==="saved" ? base.filter(c => c.tracked && !c.ni)
    : base.filter(c => !c.ni && !c.tracked);
  // faceted dropdown counts, company-shaped (a company can span cities)
  const modeOf = c => c.ni ? "uninterested" : c.tracked ? "saved" : "review";
  const cfc = skip => (DATA.companies||[]).filter(c =>
    modeOf(c) === COMPANY_MODE && coPass(c, skip));
  const ctally = (arr, key) => { const n = {};
    arr.forEach(c => (key(c)||[]).forEach(k => { if(k) n[k] = (n[k]||0)+1; })); return n; };
  const ccRows = cfc("city"), ciRows = cfc("industry");
  setFacetCounts("city", ctally(ccRows, c => c.cities), ccRows.filter(c => !(c.cities||[]).length).length);
  setFacetCounts("ind", ctally(ciRows, c => [c.industry]), ciRows.filter(c => !c.industry).length);
  setFacetCounts("rnd", ctally(cfc("round"), c => [roundGroup(c.stage)]));
  const cmRows = cfc("cmiss"), cmC = {industry:0, location:0, stage:0, site:0, desc:0};
  cmRows.forEach(c => { const city0 = (c.cities||[])[0] === "Other" ? "" : (c.cities||[])[0];
    if(!c.industry) cmC.industry++; if(!city0) cmC.location++; if(!c.stage) cmC.stage++;
    if(!c.site) cmC.site++; if(!c.blurb) cmC.desc++; });
  setFacetCounts("cmiss", cmC);
  updateClearBtn();
  const cnewest = c => [c.saved_at||"", ...(c.postings||[]).map(p=>p.first_seen||"")]
    .reduce((m,x) => x > m ? x : m, "");
  const csort = el("csort").value || "az";
  const filedOn = c => (c.raised && c.raised.filed) || "";
  // with the funded filter on, freshest money leads regardless of sort —
  // that IS the question being asked
  list = [...list].sort((a,b) =>
    F.funded ? filedOn(b).localeCompare(filedOn(a)) || a.name.localeCompare(b.name)
    : csort === "recent" ? cnewest(b).localeCompare(cnewest(a)) || a.name.localeCompare(b.name)
    : a.name.localeCompare(b.name));
  CO_RENDER = list.slice(0, CO_SHOWN).map(c => c.name);
  el("companies").classList.toggle("noflow", !list.length);
  el("companies").innerHTML =
    (list.length ? mason(list.slice(0, CO_SHOWN).map(c => companyCard(c, false)), hostW("companies"))
      + (list.length > CO_SHOWN ? `<div class="showmore"><button type="button" class="btn"
          onclick="CO_SHOWN += 120; renderCompanies()">Show More (${list.length - CO_SHOWN} left)</button></div>` : "") : "") ||
    `<div class="empty" style="grid-column:1/-1">${COMPANY_MODE==="saved"
      ? "No companies saved yet. Set a card to Saved, or + Add."
      : COMPANY_MODE==="uninterested" ? "Nothing marked uninterested." : "No companies match."}</div>`;
  observeCards("companies");
  updateBulk();
}
let CO_EDITING = false;
window.editCompany = (key) => {
  const c = (DATA.companies||[]).find(x => x.key === key);
  if(!c) return;
  openForm("company");
  CO_EDITING = true;
  el("fc-title").textContent = "Edit " + c.name;
  el("fc-save").textContent = "Save";
  el("fc-name").value = c.name; el("fc-name").readOnly = true;
  el("fc-desc").value = c.blurb || "";
  el("fc-why").value = c.why || "";
  el("fc-site").value = c.site || ""; el("fc-linkedin").value = c.linkedin || "";
  el("fc-ind").value = c.industry || "";
  el("fc-round").value = roundGroup(c.stage) || "";
  el("fc-loc").value = (c.cities||[])[0] || "";   // bucket, matching the options
};
window.trackCo = async name => {
  const why = prompt(`Note for ${name}:`) || "";
  try { await api("/api/track", {company: name, why}); toast("now following " + name);
        setTimeout(softReload, 350); }
  catch(e){ toast(String(e.message || e)); }
};

// ---- people cards -----------------------------------------------------------------
// Identity colors stay inside the app's palette: clay, ochre, sand, olive,
// moss, pine, slate, terracotta — hashed per name, never neon.
const EARTH = ["hsl(24,42%,42%)","hsl(36,40%,40%)","hsl(48,36%,38%)","hsl(84,26%,38%)",
               "hsl(140,24%,36%)","hsl(168,26%,34%)","hsl(204,24%,40%)","hsl(12,38%,42%)"];
function earthTone(name){
  let h = 0; for(const ch of name) h = (h*31 + ch.charCodeAt(0)) >>> 0;
  return EARTH[h % EARTH.length];
}
function personTone(pv){
  // A person wears their company's industry color; unclassified companies
  // share the default olive so teammates always match.
  const ind = CO_IND[(pv.company||"").toLowerCase().trim()];
  return `hsl(${indHue(ind)},36%,38%)`;
}
function avatar(pv){
  const initials = pv.name.split(/\s+/).map(w=>w[0]).filter(Boolean).slice(0,2).join("").toUpperCase();
  return `<span class="pband" style="background:${personTone(pv)}"><span class="pbandin">${esc(initials)}</span></span>`;
}
function contactIcons(pv){
  // Only channels actually on file render — four grey placeholders were dead
  // weight on most cards. One dashed + stands in for everything missing and
  // opens the edit form. Live icons wear the card's own identity tone.
  const tone = personTone(pv);
  const pid = pv.name + "|" + pv.company;
  const icn = (href, label, glyph, external) => href
    ? `<a class="icn" style="color:${tone};border-color:${tone}" href="${esc(href)}"
         ${external?'target="_blank" rel="noopener"':''}
         aria-label="${esc(label)}" title="${esc(label)}"
         onclick="event.stopPropagation()">${glyph}</a>`
    : "";
  const live = [
    icn(pv.linkedin, "LinkedIn", "in", true),
    icn(pv.email ? "mailto:" + pv.email : "", "Email " + (pv.email||""), "@", false),
    icn(pv.x ? (pv.x.startsWith("http") ? pv.x : "https://x.com/" + pv.x.replace(/^@/,"")) : "", "X", "𝕏", true),
    icn(pv.phone ? "tel:" + pv.phone.replace(/[^+\d]/g,"") : "", "Call " + (pv.phone||""), '<span class="big">☏</span>', false),
  ].filter(Boolean);
  if(LIVE && live.length < 4)
    live.push(`<button type="button" class="icn addc" title="Add contact info" aria-label="Add contact info"
      onclick="event.stopPropagation();editPerson('${jsq(pid)}')">+</button>`);
  return live.join("");
}

const PEOPLE_EXPANDED = new Set();
window.togglePerson = (pid) => {
  // People kept their in-place downward expansion — the modal treatment
  // stays for postings and companies only. One person open at a time.
  const opening = !PEOPLE_EXPANDED.has(pid);
  const prev = [...PEOPLE_EXPANDED][0];
  PEOPLE_EXPANDED.clear();
  if(opening) PEOPLE_EXPANDED.add(pid);
  JUST_OPENED = opening ? pid : null;
  // Re-dealing the masonry on every expand made the whole grid shuffle
  // (Eric's screenshots, 2026-08-18): in the flat view the touched cards
  // swap in place and every other card stays put. Grouped view re-renders
  // (group boxes reflow anyway).
  const patch = id => {
    const node = document.querySelector(`#pane-people .pcard[data-pid="${CSS.escape(id)}"]`);
    const pv = (DATA.people||[]).find(x => (x.name + "|" + x.company) === id);
    if(!node || !pv) return false;
    const tmp = document.createElement("div");
    tmp.innerHTML = personCard(pv);
    node.replaceWith(tmp.firstElementChild);
    return true;
  };
  if(PVIEW !== "flat" || !((prev && prev !== pid ? patch(prev) : true) & patch(pid)))
    renderPeople();
  JUST_OPENED = null;
};
function relLabel(pv){
  return pv.rel === "founder" ? "Founder" : pv.rel === "recruiter" ? "Recruiter"
    : pv.rel === "employee" ? "Employee" : "Contact";
}
function personExpanded(pv, pid){
  // Rows render only when there's something to say; six dashes in a column
  // buried the two facts that mattered. One "Contact: none" line covers the
  // empty channels, and Origin/Added always render.
  const prow = (label, v) =>
    `<div class="prow"><span class="plab">${label}</span><span>${v || '<span class="co">—</span>'}</span></div>`;
  const lnk = (href, text) =>
    `<a href="${esc(href)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(text)}</a>`;
  const alums = (pv.signals||[]).filter(x=>ALUM_SIGNALS.includes(x));
  return `<div class="pexp${JUST_OPENED === pid ? " anim" : ""}"><div class="pexpin">
    ${prow("Origin", pv.warm
      ? `<span class="o-warm">Warm${pv.wvia ? " — via " + esc(pv.wvia) : ""}</span>`
      : isWarm(pv) ? `<span class="o-warm">Warm — verified alum</span>`
      : `<span class="o-cold">Cold</span>`)}
    ${alums.length ? prow("Alumni",
      alums.map(x=>`<span class="${x==="Emory"?"alum-emory":"alum-nmh"}">${esc(x)}</span>`).join(", ")) : ""}
    ${pv.linkedin ? prow("LinkedIn", lnk(pv.linkedin, pv.linkedin.replace(/^https?:\/\/(www\.)?/,""))) : ""}
    ${pv.email ? prow("Email", lnk("mailto:" + pv.email, pv.email)) : ""}
    ${pv.x ? prow("X", lnk(pv.x.startsWith("http") ? pv.x : "https://x.com/" + pv.x.replace(/^@/,""), pv.x)) : ""}
    ${pv.phone ? prow("Phone", esc(pv.phone)) : ""}
    ${pv.linkedin||pv.email||pv.x||pv.phone ? "" :
      prow("Contact", `<span class="co">none on file${LIVE?" — Edit to add":""}</span>`)}
    ${pv.notes ? prow("Notes", esc(short(pv.notes, 260))) : ""}
    ${pv.st && pv.st !== "review"
      ? prow("Stage", (pv.std ? `${esc(cap(pv.st))} since ${esc(pv.std)}` : esc(cap(pv.st)))
        + (pv.cm ? ` · <span class="co">via ${esc(pv.cm)}</span>` : "")
        + (LIVE ? ` <button type="button" class="linklike stdedit" title="Fix when this actually happened"
            onclick="event.stopPropagation();editStageDate('${jsq(pv.name)}','${jsq(pv.company||"")}','${jsq(pv.st)}')">✎ date</button>` : "")) : ""}
    ${pv.loc ? prow("Location", esc(pv.loc)) : ""}
    ${prow("Added", pv.added
      ? `${esc(ago(pv.added))} · <span class="co">${pv.src === "feed" ? "auto (feed)" : "manual"}</span>` : "")}
    ${pv.nudges ? prow("Nudged", `${pv.nudges}× · last ${esc(ago(pv.nudged))}` +
      `<span class="co"> — two is the whole budget</span>`) : ""}
    ${LIVE?`<div class="pexprow">
      <button type="button" class="btn editbtn" style="background:${personTone(pv)};border-color:transparent"
        onclick="event.stopPropagation();editPerson('${jsq(pid)}')">Edit</button>
      ${pv.linkedin && (!pv.role || !pv.loc)?`<button type="button" class="btn autobtn"
        onclick="event.stopPropagation();personAutofill('${jsq(pv.linkedin)}','${jsq(pv.name)}')">Autofill Person</button>`:""}
      ${["contacted","conversation","met"].includes(pv.st)?`<button type="button" class="btn"
        title="Log that a follow-up went out today"
        onclick="event.stopPropagation();recordNudge('${jsq(pv.name)}','${jsq(pv.company||"")}')">Nudged</button>`:""}
    </div>`:""}
  </div></div>`;
}
const PSEL = new Set();
let RENDER_PIDS = [];       // cards in rendered order, for shift-ranges
let LAST_SEL = null;
// Postings and companies select the same way people do: ⌘-click marks,
// shift-click ranges, one selection kind at a time.
const POSTSEL = new Set(); let LAST_POST = null; let ROW_KEYS = [];
const COSEL = new Set();   let LAST_CO = null;   let CO_RENDER = [];
function clearOtherSel(keep){
  if(keep !== "person") PSEL.clear();
  if(keep !== "post") POSTSEL.clear();
  if(keep !== "co") COSEL.clear();
}
window.rowTap = (ev, u) => {
  if(ev.shiftKey && POSTSEL.size && LAST_POST){
    ev.preventDefault();
    const a = ROW_KEYS.indexOf(LAST_POST), b = ROW_KEYS.indexOf(u);
    if(a >= 0 && b >= 0)
      ROW_KEYS.slice(Math.min(a,b), Math.max(a,b)+1).forEach(x => POSTSEL.add(x));
    LAST_POST = u; render(); return;
  }
  if(ev.metaKey || ev.ctrlKey){
    clearOtherSel("post");
    if(POSTSEL.has(u)) POSTSEL.delete(u); else POSTSEL.add(u);
    LAST_POST = u; render(); return;
  }
  toggleRow(u);
};
window.coTap = (ev, name) => {
  if(ev.shiftKey && COSEL.size && LAST_CO){
    ev.preventDefault();
    const a = CO_RENDER.indexOf(LAST_CO), b = CO_RENDER.indexOf(name);
    if(a >= 0 && b >= 0)
      CO_RENDER.slice(Math.min(a,b), Math.max(a,b)+1).forEach(x => COSEL.add(x));
    LAST_CO = name; renderCompanies(); return;
  }
  if(ev.metaKey || ev.ctrlKey){
    clearOtherSel("co");
    if(COSEL.has(name)) COSEL.delete(name); else COSEL.add(name);
    LAST_CO = name; renderCompanies(); return;
  }
  toggleCompany(CO_KEY[name.toLowerCase().trim()] || name);
};
let PEOPLE_MODE = "review";
window.setPeopleMode = (m) => {
  PEOPLE_MODE = m;
  if(m === "uninterested") m = "review";
  PEOPLE_MODE = m;
  for(const [id, mm] of [["pt-review","review"],["pt-saved","saved"]]){
    el(id).classList.toggle("on", m === mm);
    el(id).setAttribute("aria-pressed", String(m === mm));
  }
  moveModeSlides();
  renderPeople();
};
// Cards fade up as they scroll into view.
const IO = ("IntersectionObserver" in window) ? new IntersectionObserver(es => {
  es.forEach(e => { if(e.isIntersecting){ e.target.classList.add("vis"); IO.unobserve(e.target); } });
}, {threshold: .05}) : null;
// The chip balancer (evened a 5+1 wrap into 3+3) retired 2026-08-12: it
// moved chips OFF a first row that still had room, which read worse than
// the orphan it fixed. Rows pack naturally now.
function observeCards(rootId){
  if(!IO) return;
  // Disconnect first: the observer pinned every replaced card subtree in
  // memory — 11 search keystrokes doubled the page's node count (2026-08-12).
  // Survivors still waiting to reveal are re-subscribed.
  IO.disconnect();
  document.querySelectorAll(".preveal").forEach(c => IO.observe(c));
  document.querySelectorAll(`#${rootId} .ccard, #${rootId} .pcard`).forEach(c => {
    if(c.classList.contains("vis")) return;
    // Only below-the-fold cards animate in — hiding on-screen cards for a
    // frame made every re-render flash the whole list.
    if(c.getBoundingClientRect().top > innerHeight){
      c.classList.add("preveal"); IO.observe(c);
    }
  });
}
function updateBulk(){
  const n = PSEL.size || POSTSEL.size || COSEL.size;
  const bb = el("bulkbar");
  if(bb){ bb.hidden = !n; el("bulkcount").textContent = `${n} selected`;
    el("bulk-autofill").hidden = !COSEL.size; }
}
window.bulkAutofill = async () => {
  const names = [...COSEL]; let done = 0;
  for(const name of names){
    try {
      const co = (DATA.companies||[]).find(x => x.name === name) || {};
      await api("/api/company-auto", {company: name, lane: "both", site: co.site || ""});
      done++;
      toast(`autofilling ${done}/${names.length}…`);
    } catch(_){}
  }
  COSEL.clear(); updateBulk();
  toast(`${done} of ${names.length} companies autofilled/queued`);
  setTimeout(softReload, 400);
};
// ⌘-click (ctrl off-mac) marks a card for bulk triage; with a selection
// going, shift-click takes everything between; plain click expands.
let JUST_OPENED = null;
let PVIEW = "co";
let PVIEW_SEL = "flat";     // which of the two view buttons is pressed
window.pcardTap = (ev, pid) => {
  if(ev.shiftKey && PSEL.size && LAST_SEL){
    ev.preventDefault();
    const a = RENDER_PIDS.indexOf(LAST_SEL), b = RENDER_PIDS.indexOf(pid);
    if(a >= 0 && b >= 0)
      RENDER_PIDS.slice(Math.min(a,b), Math.max(a,b)+1).forEach(x => PSEL.add(x));
    LAST_SEL = pid; renderPeople(); return;
  }
  if(ev.metaKey || ev.ctrlKey){
    clearOtherSel("person");
    if(PSEL.has(pid)) PSEL.delete(pid); else PSEL.add(pid);
    LAST_SEL = pid; renderPeople(); renderCompanies(); render(); return;
  }
  togglePerson(pid);
};
window.bulkClear = () => {
  clearOtherSel("none"); render(); renderCompanies(); renderPeople();
};
window.bulkSet = async (to) => {
  metal(to === "saved" ? "save" : "kill");
  try {
    if(PSEL.size){
      for(const pid of [...PSEL]){
        const [name, company] = pid.split("|");
        await api("/api/person-status", {name, company, status: to, date: "", create: true});
      }
    } else if(POSTSEL.size){
      let note = "";
      if(to === "uninterested")
        note = prompt("Why pass on these? (one reason for the batch — teaches the scorer)") || "";
      for(const u of [...POSTSEL])
        await api("/api/mark", {url: u, status: to, note, date: ""});
    } else {
      for(const name of [...COSEL])
        await api("/api/company-mode", {company: name, mode: to});
    }
    const n = PSEL.size || POSTSEL.size || COSEL.size;
    toast(`${n} ${to === "saved" ? "saved" : "marked uninterested"}`);
    setTimeout(softReload, 350);
  } catch(e){ toast(String(e.message || e)); }
};
// Display-only cleanup of role titles: source casing ("cofounder", "co-founder
// and CEO") and Form D officer legalese normalize; the data stays raw.
function roleDisp(t){
  if(!t) return "";
  t = t.trim().replace(/\bcofounder\b/gi, "Co-founder");
  if(/^executive officer\b/i.test(t)) return "Executive Officer";
  return t.charAt(0).toUpperCase() + t.slice(1);
}
// Same chip rule as companies: a school chip absorbs Warm, both schools
// collapse to one Alumni chip. Non-school signals (Jets, Prague, …) still
// render, capped, after the school family.
function personFam(pv){
  const sig = pv.signals || [];
  const schools = sig.filter(x => ALUM_SIGNALS.includes(x));
  const fams = famChips(sig, pv.ev);
  // Anything left is an interest, not a shared room — Prague, powerlifting,
  // the PCT. Real chips, capped, but they never make a stranger warm.
  const others = sig.filter(x => !ALUM_SIGNALS.includes(x) && !WARM_SIGNALS.includes(x));
  const sch = schools.length >= 2 ? `<span class="sig sig-alumni" title="Alumni — multiple schools" role="img" aria-label="Alumni">${gw("🎓", "Alumni")}</span>`
    : schools.map(schoolChip).join(" ");
  const fam = others.includes("Family") ? `<span class="sig sig-family">Family</span>` : "";
  const rest = others.filter(x => x !== "Family");
  const warm = !schools.length && !fams && isWarm(pv)
    ? `<span class="warmtag sig" title="Warm lead" role="img" aria-label="Warm">${gw("🔥", "Warm")}</span>` : "";
  return warm + sch + fams + fam + rest.slice(0,2).map(x=>`<span class="sig">${esc(x)}</span>`).join(" ")
    + (rest.length > 2 ? `<span class="co">+${rest.length-2}</span>` : "");
}
function personCard(pv, nested, forceOpen, kb){
  const links = contactIcons(pv);
  const pid = (pv.name + "|" + pv.company);
  if(!nested && !forceOpen) RENDER_PIDS.push(pid);
  const open = forceOpen || (!nested && PEOPLE_EXPANDED.has(pid));
  const sel = !nested && !forceOpen && PSEL.has(pid);
  const shown = pv.name.split(",")[0].trim();   // degrees live in the expand
  const roleTxt = pv.role ? roleDisp(pv.role) : relLabel(pv);
  // The chip flags founder-ness when the title doesn't say it; with the
  // two-line card there is room for both, so the role line only hides when
  // it would literally repeat the chip.
  const fchip = pv.rel === "founder" && !/founder/i.test(roleTxt);
  const hideRole = pv.rel === "recruiter" && !pv.role;
  const act = forceOpen ? "" : nested ? `gotoPerson('${jsq(pid)}')` : `pcardTap(event,'${jsq(pid)}')`;
  const tri = kb && pv.st !== "saved" ? "" : triageBtns("person", pid, bucketOf(pv.st||"review"));
  // a Saved person hasn't been written to — a contact method here is a
  // left-over from moving the card back (Eric, 2026-08-12)
  const cmShow = pv.st === "saved" ? "" : pv.cm;
  const tone = personTone(pv);
  const wash = tone.replace("hsl(", "hsla(").replace(")", ",0.16)");
  const pcInd = CO_IND[(pv.company||"").toLowerCase().trim()] || "";
  const rule = `repeating-linear-gradient(transparent 0 21px, hsla(${indHue(pcInd)},40%,40%,.05) 21px 22px) top left/100% calc(100% - 16px) no-repeat`;
  const edge = ` style="${(pcInd ? indShade(pcInd) : `background:linear-gradient(${wash},${wash}),var(--card)`)
    .replace("background:", `background:${rule},`)}${forceOpen ? `;border-color:${tone}` : ""};--indh:${indHue(pcInd)}"`;
  return `<div class="pcard ${open?"open":""} ${sel?"sel":""} ${nested&&pv.st==="uninterested"?"ghost":""} ${kb?"kbdrag":""}"${edge} role="button" tabindex="0"
    ${kb?`draggable="true" ondragstart="kbDrag(event,'ppl','${jsq(pid)}')" ondragend="kbDragEnd(event)"`:""}
    aria-label="${nested?"Open":"Details for"} ${esc(shown)}${nested?"":" (⌘-click to select)"}" aria-expanded="${open}"
    data-pid="${esc(pid)}"
    onclick="${act}"
    onkeydown="${forceOpen?"":`if(event.key==='Enter'){${act}}`}">
    ${avatar(pv)}
    <div class="pbody">
      <div class="pname"><span class="pnametxt">${esc(shown)}</span>${open || !hideRole
        ? `<span class="pdiv">·</span><span class="prole">${esc(short(roleTxt,90))}</span>` : ""}</div>
      <div class="pline2">
        ${(kb||PVIEW==="flat")&&pv.company?`<span class="cchip cochip"${pcInd?` style="${indChip(pcInd)}"`:""}>${esc(coDisp(pv.company))}</span>`:""}
        ${!kb&&pv.added?`<span class="cchip addedchip">${esc(ago(pv.added))}</span>`:""}
        ${kb&&(pv.days!=null||cmShow)?`<button type="button" class="cchip qedit${stalled(pv.st,pv.days)?" stallchip":""}" title="When did this actually happen? Click to fix the date/method" onclick="event.stopPropagation();editStageDate('${jsq(pv.name)}','${jsq(pv.company||"")}','${jsq(pv.st)}')">${pv.days!=null?`${pv.days}d`:""}${pv.days!=null&&cmShow?" · ":""}${cmShow?esc(cmShow):""}</button>`:""}
        <span class="sigs">
          ${pv.loc?`<span class="cchip citychip" title="${esc(pv.loc)}"
            style="color:${cityTone(pv.loc)};background:${toneWash(cityTone(pv.loc))}">${gw(cityAbbr(pv.loc), short(pv.loc, 24))}</span>`:""}
          ${kb ? (isWarm(pv) ? `<span class="warmtag sig" title="Warm lead" role="img" aria-label="Warm">${gw("🔥", "Warm")}</span>` : "")
                 : personFam(pv)}
          ${fchip?`<span class="sig sig-founder">Founder</span>`:""}
          ${pv.rel==="recruiter"&&!/recruiter/i.test(roleTxt)?`<span class="sig sig-recruiter">Recruiter</span>`:""}
        </span>
        ${links?`<span class="pmeta icons">${links}</span>`:""}
        ${open?"":tri}
      </div>
      ${open ? personExpanded(pv, pid) : ""}
    </div>
    ${open?tri:""}
  </div>`;
}
function renderPeople(){
  RENDER_PIDS = [];
  const q = (el("q").value || "").toLowerCase();
  let list = (DATA.people || []).filter(pv =>
    !q || `${pv.name} ${pv.company} ${(pv.signals||[]).join(" ")} ${pv.notes}`.toLowerCase().includes(q));
  if(F.warm) list = list.filter(pv => isWarm(pv));
  if(F.sports) list = list.filter(pv => isSports(pv));
  if(F.music) list = list.filter(pv => isMusic(pv));
  if(F.research) list = list.filter(pv => isResearch(pv));
  if(F.family) list = list.filter(pv => (pv.signals||[]).includes("Family"));
  if(F.recruiter) list = list.filter(pv => pv.rel === "recruiter");
  const hc = el("hascontact").value;
  if(hc) list = list.filter(pv => pv[hc]);
  const pbase = list;
  const pN = {review: pbase.filter(pv=>bucketOf(pv.st||"review")==="review").length,
              saved: pbase.filter(pv=>bucketOf(pv.st||"review")==="saved").length};
  el("pt-review").textContent = `Review (${pN.review})`;
  el("pt-saved").textContent = `Saved (${pN.saved})`;
  for(const m of ["review","saved"]){
    el("pt-"+m).classList.toggle("empty0", !pN[m]);
    el("pt-"+m).disabled = !pN[m] && PEOPLE_MODE !== m;
  }
  if(F.alumni) list = list.filter(pv => (pv.signals||[]).some(s => ALUM_SIGNALS.includes(s)));
  const pst = PEOPLE_MODE;
  if(pst) list = list.filter(pv => bucketOf(pv.st||"review") === pst);
  // The talking-stage sub-filter only exists inside the Saved view.
  const tsEl = el("talkstage");
  if(tsEl){
    tsEl.hidden = !(TAB === "people" && pst === "saved");
    if(!tsEl.hidden && tsEl.value) list = list.filter(pv => pv.st === tsEl.value);
  }
  PVIEW = PVIEW_SEL;
  const groups = {};
  for(const pv of list){ (groups[pv.company || "—"] ||= []).push(pv); }
  // Three orders. Actionable is the working default (Eric, 2026-08-11):
  // a TRANSPARENT rank — warm first, furthest talk-stage next, most recently
  // touched inside that — not the opaque relevance weighting retired in July,
  // which confused because nobody could predict it. A–Z and Recently Added
  // stay as the lookup modes, and A–Z keeps the letter rail.
  // The Saved pile reads newest-first by default (Eric, 2026-08-18): a
  // just-added person is the one you meant to act on. A–Z stays selectable;
  // Actionable still rules the Review pile.
  const psort0 = el("psort").value || "act";
  const psort = (PEOPLE_MODE === "saved" && psort0 === "act") ? "recent" : psort0;
  const ST_RANK = {met:4, conversation:3, contacted:2, saved:1};
  const actRank = pv => [isWarm(pv) ? 0 : 1, -(ST_RANK[pv.st] || 0),
                         pv.days != null ? pv.days : 9999];
  const actCmp = (x,y) => { const a = actRank(x), b = actRank(y);
    for(let i = 0; i < 3; i++) if(a[i] !== b[i]) return a[i] - b[i];
    return x.name.localeCompare(y.name); };
  const newest = co => groups[co].reduce((m,pv) => pv.added > m ? pv.added : m, "");
  const best = co => groups[co].slice().sort(actCmp)[0];
  const names = Object.keys(groups).sort((a,b) =>
    psort === "recent" ? newest(b).localeCompare(newest(a)) || a.localeCompare(b)
    : psort === "act"  ? actCmp(best(a), best(b)) || a.localeCompare(b)
                       : a.localeCompare(b));
  if(psort === "recent")
    Object.values(groups).forEach(g => g.sort((x,y) => (y.added||"").localeCompare(x.added||"")));
  if(psort === "act")
    Object.values(groups).forEach(g => g.sort(actCmp));
  // the "—" bucket (people with no company on file) pins to the top-left —
  // they need a company found before anything else can happen (Eric)
  const dashAt = names.indexOf("—");
  if(dashAt > 0){ names.splice(dashAt, 1); names.unshift("—"); }
  updateBulk();
  updateClearBtn();
  // The 40-group cap predates the A–Z sort: under alphabetical order it cut
  // the list off mid-alphabet, and gotoPerson could land on a person whose
  // card was never rendered. ~250 people render fine; cap generously.
  const LIMIT = 500;
  if(PVIEW === "flat"){
    const flat = [...list].sort((a,b) => psort === "act" ? actCmp(a,b)
      : psort === "recent"
      ? (b.added||"").localeCompare(a.added||"") || a.name.localeCompare(b.name)
      : a.name.localeCompare(b.name));
    const cards = flat.map(pv => personCard(pv));
    el("people").innerHTML = `<div class="pmason">` +
      (cards.length ? mason(cards, hostW("people"), 4)
        : '<div class="empty">The rolodex is empty. Founders arrive with enrichment — run <code>make founders</code> — or + Add.</div>')
      + `</div>`;
    observeCards("people");
    return;
  }
  const groupHtml = names.slice(0, LIMIT).map(co => {
      const ppl = groups[co];
      ppl.sort((a,b) => (a.rel==="founder"?0:1)-(b.rel==="founder"?0:1) || a.name.localeCompare(b.name));
      const ck = CO_KEY[(co||"").toLowerCase().trim()];
      const cind = CO_IND[(co||"").toLowerCase().trim()];
      const gcity = (((DATA.companies||[]).find(c=>c.name.toLowerCase()===co.toLowerCase())||{}).cities||[])[0] || "";
      // border-box: 32px padding + 2px border live inside the basis, and
      // wrap decisions use the cards' 375px base size — 2px short stacks
      // the pair vertically. +36 covers padding, border, and rounding.
      return `<div class="cogroup" style="--indh:${indHue(cind)}"><h3 class="cohead"${ck?` role="button" tabindex="0" onclick="gotoCompany('${jsq(ck)}')"
          onkeydown="if(event.key==='Enter')gotoCompany('${jsq(ck)}')"`:""}>${ck
          ? `<span class="colink cname">${esc(coDisp(co))}</span>`
          : `<span class="cname">${esc(coDisp(co))}</span>`}
        <span class="co">·</span>${pplTag(ppl.length, true)}</h3>
        <div class="pgrid">${ppl.map(pv => personCard(pv)).join("")}</div></div>`;
    });
  el("people").innerHTML = `<div class="pmason">` +
    (groupHtml.length ? mason(groupHtml, hostW("people"), 4)
      : '<div class="empty">The rolodex is empty. Founders arrive with enrichment — run <code>make founders</code> — or + Add.</div>')
    + `</div>` +
    (names.length > LIMIT ? `<div class="why" style="text-align:center;padding:10px">
       showing ${LIMIT} of ${names.length} companies — search to narrow</div>` : "");
  observeCards("people");
}


// ---- tabs + chrome ---------------------------------------------------------------
function slideTab(){
  const sl = el("tabslide"), on = document.querySelector(".tab.on");
  if(!sl || !on) return;
  if(!on.offsetWidth){ setTimeout(slideTab, 120); return; }
  sl.style.left = on.offsetLeft + "px";
  sl.style.width = on.offsetWidth + "px";
}
window.addEventListener("resize", slideTab);
requestAnimationFrame(slideTab);
window.addEventListener("load", () => setTimeout(slideTab, 80));
let MASON_RSZ = null;
window.addEventListener("resize", () => {
  clearTimeout(MASON_RSZ);
  MASON_RSZ = setTimeout(() =>
    markDirty("tracker", "postings", "companies", "people", "tweets"), 160);
});
// ---- Tweets ---------------------------------------------------------------
// The X sweep stores hiring tweets verbatim because the tweet IS the lead —
// a VC vouching for a founder has no link to extract. Cards ask
// platform.twitter.com for the real embed; the stored text is the
// blockquote's own content, so the page still reads with the script blocked.
let TWEET_MODE = "new";       // the unread pile first — that's the work
// kind facet (pipeline field `kind`, 2026-08-19). "" = all; "unset" = rows
// collected before classification existed — NEVER defaulted into hiring,
// half the ledger would render as a category it was never sorted into.
let TWEET_KIND = "";
let TW_SHOWN = 12;
let TW_W = 0;                 // width the pane last rendered at
const TW_PAGE = 12;
const themeNow = () => document.documentElement.dataset.theme
  || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
function updateTweetCounts(){
  const all = DATA.tweets || [];
  const n = {new: 0, saved: 0, dismissed: 0};
  all.forEach(x => { const s = x.status || "new"; if(s in n) n[s]++; });
  for(const m of ["new","saved","dismissed"]){
    el("tw-" + m).textContent = `${cap(m)} (${n[m]})`;
    el("tw-" + m).classList.toggle("empty0", !n[m]);
  }
  const badge = el("twn");
  if(badge){ badge.textContent = n.new; badge.hidden = !n.new; }
  const kc = {hiring:0, raise:0, intro:0, advice:0, unset:0};
  all.forEach(x => { const k = x.kind && kc[x.kind] !== undefined ? x.kind : "unset"; kc[k]++; });
  const KL = {hiring:"Hiring", raise:"Raise", intro:"Intro", advice:"Advice", unset:"Unsorted"};
  for(const k in kc){
    const c = el("twk-" + k); if(!c) continue;
    c.textContent = `${KL[k]} (${kc[k]})`;
    // zero kinds hide until the sweep produces them; stay if active.
    // A CLASS, not inline display — the tab-chrome loop rewrites inline
    // display on every tab switch and was resurrecting the (0) chips.
    c.classList.toggle("kzero", !kc[k] && TWEET_KIND !== k);
  }
}
window.setTweetKind = (k) => {
  TWEET_KIND = TWEET_KIND === k ? "" : k;
  ["hiring","raise","intro","advice","unset"].forEach(x => {
    const c = el("twk-" + x);
    c.classList.toggle("on", TWEET_KIND === x);
    c.setAttribute("aria-pressed", String(TWEET_KIND === x));
  });
  renderTweets();
};
window.setTweetMode = (m) => {
  TWEET_MODE = m;
  for(const [id, mm] of [["tw-new","new"],["tw-saved","saved"],["tw-dismissed","dismissed"]]){
    el(id).classList.toggle("on", m === mm);
    el(id).setAttribute("aria-pressed", String(m === mm));
  }
  TW_SHOWN = TW_PAGE; renderTweets();
};
window.twMore = () => { TW_SHOWN += TW_PAGE; renderTweets(); };
let TW_WIDGETS = null;
function twWidgets(){
  // Fetched on first view of the tab, never at page load: the tab may never
  // be opened, and a blocked script must leave the fallback cards standing.
  if(TW_WIDGETS) return TW_WIDGETS;
  TW_WIDGETS = new Promise(res => {
    const s = document.createElement("script");
    s.src = "https://platform.twitter.com/widgets.js";
    s.async = true; s.charSet = "utf-8";
    s.onload = () => res(true);
    s.onerror = () => res(false);
    document.head.appendChild(s);
  });
  return TW_WIDGETS;
}
// Embeds are the expensive part: every rendered tweet is a cross-origin
// iframe with its own document and script, and loading a whole page of them
// at once was slow and heavy. So a card only becomes a real embed when it
// comes near the viewport, and reverts to its stored verbatim blockquote once
// it is well behind — with the measured height pinned so nothing jumps.
let TW_IO_IN = null;
function twMount(card){
  if(card.dataset.twOn) return;
  if(!card.querySelector("blockquote.twitter-tweet")) return;
  card.dataset.twOn = "1";
  twWidgets().then(ok => {
    // offline / blocked: the fallback card stays exactly as it is
    if(!ok || !card.isConnected || !card.dataset.twOn) return;
    try {
      const p = window.twttr && twttr.widgets && twttr.widgets.load(card);
      if(p && p.then) p.then(() => { card.style.minHeight = ""; });
    } catch(_){}
  });
}
function twLoadEmbeds(){
  const host = el("tweets");
  if(!host) return;
  TW_IO_IN && TW_IO_IN.disconnect();
  // Mount within 700px of the viewport, then KEEP the embed for the session
  // (Eric, 2026-08-12): at ≤~50 ledger tweets the iframe memory is bounded,
  // and an embed that reloads on scroll-back or tab-switch is worse. The old
  // far-unmount returns only if the ledger ever grows to hundreds.
  TW_IO_IN = new IntersectionObserver(
    es => es.forEach(e => { if(e.isIntersecting) twMount(e.target); }),
    {rootMargin: "700px 0px"});
  host.querySelectorAll(".twcard").forEach(c => TW_IO_IN.observe(c));
}
function tweetCard(t){
  const who = t.author_name || t.handle || "";
  const st = t.status === "saved" ? "saved"
           : t.status === "dismissed" ? "uninterested" : "review";
  const hints = [
    t.company ? `<span class="cchip inchip">${esc(short(t.company, 30))}</span>` : "",
    t.role ? `<span class="cchip">${esc(short(t.role, 44))}</span>` : "",
    ...(t.people || []).slice(0, 3).map(p => `<span class="cchip">${esc(short(p, 26))}</span>`),
    t.date ? `<span class="cchip">${esc(ago(t.date))}</span>` : "",
  ].filter(Boolean).join("");
  // Kept as a string as well as in the DOM: unmounting an off-screen embed
  // puts this exact fallback back, so the card never goes blank.
  const bq = `<blockquote class="twitter-tweet" data-theme="${themeNow()}" data-dnt="true"
      data-conversation="none" data-align="left">
      <p>${esc(t.text || "").replace(/\n/g, "<br>")}</p>
      <span class="twby">— ${esc(who)}${t.handle ? ` (@${esc(t.handle)})` : ""}
        <a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.date || "on X")}</a></span>
    </blockquote>`;
  return `<div class="ccard twcard" data-tw="${esc(t.url)}">
    ${bq}
    ${t.why ? `<div class="why">${emph(t.why)}</div>` : ""}
    <div class="cfoot">
      ${hints}
      ${t.application_link
        ? `<a class="btn" href="${esc(t.application_link)}" target="_blank" rel="noopener">Apply ↗</a>`
        : ""}
      <a class="cchip" href="${esc(t.url)}" target="_blank" rel="noopener">on X ↗</a>
      ${triageBtns("tweet", t.url, st)}
    </div>
  </div>`;
}
function renderTweets(){
  const host = el("tweets");
  if(!host) return;
  TW_W = hostW("tweets");
  updateTweetCounts();
  const all = DATA.tweets || [];
  const q = (el("q").value || "").toLowerCase();
  let list = all.filter(t => (t.status || "new") === TWEET_MODE);
  if(TWEET_KIND) list = list.filter(t =>
    TWEET_KIND === "unset" ? !t.kind : t.kind === TWEET_KIND);
  if(q) list = list.filter(t =>
    `${t.text} ${t.company} ${t.role} ${t.handle} ${t.author_name} ${(t.people||[]).join(" ")} ${t.why}`
      .toLowerCase().includes(q));
  const total = list.length;
  if(TW_SHOWN > total) TW_SHOWN = Math.max(TW_PAGE, Math.ceil(TW_SHOWN / TW_PAGE) * TW_PAGE);
  const page = list.slice(0, TW_SHOWN);
  host.innerHTML = `<div class="pmason">` + (total
      ? mason(page.map(tweetCard), hostW("pane-tweets"))
      : `<div class="empty">${q ? "No tweets match that search."
          : TWEET_MODE === "new" ? "No unread tweets. The X sweep runs once a day — <code>make xsweep</code>."
          : `Nothing ${TWEET_MODE} yet.`}</div>`)
    + `</div>`
    + (total > TW_SHOWN
        ? `<div style="text-align:center;padding:14px"><button class="btn" onclick="twMore()">`
         + `Show ${Math.min(TW_PAGE, total - TW_SHOWN)} More <span class="co">· ${TW_SHOWN} of ${total} shown</span>`
         + `</button></div>`
        : total > TW_PAGE ? `<div class="why" style="text-align:center;padding:10px">all ${total} shown</div>` : "");
  observeCards("tweets");
  twLoadEmbeds();
}
function switchTab(name){
  TAB = name;
  try { localStorage.setItem("tab", name); } catch(_){}
  const pane = el("pane-" + name).firstElementChild;
  if(pane){ pane.classList.remove("fadein"); void pane.offsetWidth; pane.classList.add("fadein"); }
  document.querySelectorAll(".tab").forEach(x => {
    const on = x.dataset.tab === name;
    x.classList.toggle("on", on);
    x.setAttribute("aria-selected", on ? "true" : "false");
  });
  slideTab();
  ["tracker","postings","companies","people","tweets"].forEach(n => {
    el("pane-" + n).hidden = (n !== name);
  });
  // The masonry was measured against a hidden pane (width 0) until the tab
  // was shown — re-render tweets on arrival only when the width truly
  // changed (embeds are not yet mounted then, so nothing reloads).
  if(name === "tweets" && TW_W !== hostW("tweets")) DIRTY.add("tweets");
  if(DIRTY.has(name)) renderTab(name);
  applyTabChrome(name);
}
function moveModeSlides(){
  document.querySelectorAll(".modebar").forEach(bar => {
    if(bar.offsetParent === null) return;
    const on = bar.querySelector(".chip.on"), sl = bar.querySelector(".modeslide");
    if(!on || !sl) return;
    sl.style.left = on.offsetLeft + "px"; sl.style.top = on.offsetTop + "px";
    sl.style.width = on.offsetWidth + "px"; sl.style.height = on.offsetHeight + "px";
    sl.classList.toggle("kill", /uninterested/.test(on.id));
  });
}
if(window.ResizeObserver){
  // count text changes chip widths after the slide was parked (Eric's
  // screenshot: the pill straddling "Saved") — any bar resize re-parks it
  const MODE_RO = new ResizeObserver(() => moveModeSlides());
  document.querySelectorAll(".modebar").forEach(b => MODE_RO.observe(b));
}
function applyTabChrome(tab){
  el("controls").hidden = (tab === "tracker");
  const show = {
    postings:  new Set(["q","ps-bar","ps-review","ps-saved","ps-uninterested","pssep","sort","pssep2","posted","yqwrap","unscored","visa","dd-cat","dd-city","dd-ind","dd-rnd","dd-src","dd-pmiss","warm","scorefil","minscore","swept","clearfil","triagebtn"]),
    companies: new Set(["q","ct-bar","ct-review","ct-saved","ct-uninterested","csep1","csort","csep2","dd-city","dd-ind","dd-rnd","dd-cmiss","warm","hasroles","funded","clearfil"]),
    people:    new Set(["q","pt-bar","pt-review","pt-saved","psep","pview-co","pview-flat","psepv","psort","psep2","talkstage","warm","family","recruiter","alumni","hascontact","clearfil"]),
    tweets:    new Set(["q","tw-new","tw-saved","tw-dismissed","twksep","twk-hiring","twk-raise","twk-intro","twk-advice","twk-unset"]),
    tracker:   new Set(),
  }[tab] || new Set();
  document.querySelectorAll("#controls select, #controls input, #controls .chip, #controls .sep, #controls .scorefil, #controls .modebar, #controls .ddwrap, #pmodes").forEach(c => {
    if(c.closest(".ddwrap") && !c.classList.contains("ddwrap")) return;
    const key = c.id || (c.dataset ? c.dataset.f : "") ||
                (c.classList.contains("sep") ? "sep" : "");
    if(key === "clearfil"){
      c.style.display = show.has(key) && filtersActive() ? "" : "none"; return; }
    let on = show.has(key) || (c.closest("#pmodes") && show.has("pmodes"));
    // search is one slot: the open bar replaces the button, never both
    if(key === "q") on = on && !el("q").hidden;
    // visa only earns a slot when a non-US city is in view
    if(key === "visa" && tab === "postings") on = on && nonUsSelected();
    c.style.display = on ? "" : "none";
  });
  const add = el("addbtn");
  add.hidden = !(LIVE && tab !== "tracker");
  el("triagebtn").hidden = true;                      // floats now (fab)
  add.hidden = true;                                   // floats now (fab)
  const fabShow = (id, on) => el(id).classList.toggle("fabhide", !on);
  fabShow("fab-search", tab !== "tracker");
  fabShow("fab-triage", LIVE && tab === "postings");
  // Tweets arrive from the sweep only — there is nothing to add by hand.
  fabShow("fab-add", LIVE && tab !== "tracker" && tab !== "tweets");
  fabShow("fab-scout", LIVE && tab === "postings");
  el("fab-add-lbl").textContent = tab === "companies" ? "Add Company"
    : tab === "postings" ? "Add Posting" : tab === "people" ? "Add Person" : "Add";
  el("pagefade").hidden = tab === "tracker";
  document.querySelectorAll("#controls select").forEach(sl => fitSelect(sl));
  requestAnimationFrame(moveModeSlides);   // after display flips; counts resize chips
}
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => { metal("accent"); switchTab(t.dataset.tab); }));
// the pill is also a slider: press and drag across the tabs
(function(){
  let dragging = false;
  const bar = document.querySelector(".tabs");
  bar.addEventListener("mousedown", () => { dragging = true; });
  document.addEventListener("mouseup", () => { dragging = false; });
  bar.addEventListener("mousemove", ev => {
    if(!dragging) return;
    const t = ev.target.closest(".tab");
    if(t && t.dataset.tab !== TAB) switchTab(t.dataset.tab);
  });
})();
// The button BECOMES the bar: swap in place rather than spawning alongside.
function openSearch(){
  modalOpener = document.activeElement;
  el("sq").value = el("q").value;
  el("searchmodal").classList.add("show");
  el("sq").focus();
}
function syncSearch(){
  el("q").value = el("sq").value;
  el("q").hidden = !el("sq").value.trim();
  applyTabChrome(TAB);
  shown = PAGE; markDirty("postings", "companies", "people", "tweets");
}
el("fab-search").addEventListener("click", openSearch);
el("sq").addEventListener("input", syncSearch);
el("sq").addEventListener("keydown", ev => { if(ev.key === "Enter") closeModal(); });
el("q").addEventListener("input", () => { el("sq").value = el("q").value; });
el("q").addEventListener("blur", () => {
  if(!el("q").value.trim()){ el("q").hidden = true; applyTabChrome(TAB); }
});

// theme toggle: a plain light/dark flip — the old third "auto" stop made
// the button need two clicks and show a half-moon nobody asked for
(function(){
  const btn = el("themebtn");
  const FACES = {light: "☀", dark: "☾"};
  const current = () => {
    let t = null; try { t = localStorage.getItem("theme"); } catch(e){}
    if(t === "light" || t === "dark") return t;
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };
  const apply = mode => {
    document.documentElement.dataset.theme = mode;
    try { localStorage.setItem("theme", mode); } catch(e){}
    btn.textContent = FACES[mode];
    btn.setAttribute("aria-label", "Theme: " + mode + " — click to switch");
    btn.title = "theme: " + mode;
  };
  apply(current());
  btn.addEventListener("click", () => {
    btn.classList.remove("flip"); void btn.offsetWidth; btn.classList.add("flip");
    apply(current() === "dark" ? "light" : "dark");
    // Embeds bake their theme in at load — re-render so they follow the flip.
    if(TAB === "tweets") renderTweets();
  });
})();

// ---- live mode -------------------------------------------------------------------
let LIVE = false;
async function api(path, body){
  const r = await fetch(path, {method: body ? "POST" : "GET",
    headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined});
  const d = await r.json().catch(() => ({}));
  if(!r.ok) throw new Error(d.error || r.status);
  return d;
}
function toast(msg){
  let t = document.querySelector(".toast");
  if(!t){ t = document.createElement("div"); t.className = "toast";
    t.setAttribute("role","status"); t.setAttribute("aria-live","polite");
    document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2600);
}
async function watchScout(){
  const bar = el("scoutbar"), msg = el("scoutmsg"), fill = el("scoutfill");
  bar.hidden = false;
  const timer = setInterval(async () => {
    let s;
    try { s = (await api("/api/ping")).scout; } catch(_) { return; }
    const log = s.log || [];
    msg.textContent = log.length ? log[log.length-1].slice(0, 110) : "starting…";
    const boards = s.boards_total ? Math.min(1, s.boards_done / s.boards_total) : 0;
    const funding = log.some(l => l.startsWith("checking")) ? 0.15 : 0;
    fill.style.width = Math.round(Math.min(0.95, boards * 0.8 + funding) * 100) + "%";
    if(!s.running){
      clearInterval(timer);
      fill.style.width = "100%";
      msg.textContent = s.last || "done";
      setTimeout(() => location.reload(), 900);
    }
  }, 1500);
}

// modals: Escape closes whichever is open; focus returns to the opener.
let modalOpener = null;
window.closeOnly = (id) => {
  const m = el(id);
  m.classList.add("closing");
  setTimeout(() => m.classList.remove("show", "closing"), 190);
};
window.closeModal = () => {
  MODAL_CARD = null;
  const open = [...document.querySelectorAll(".modal.show:not(.closing)")];
  open.forEach(m => m.classList.add("closing"));
  setTimeout(() => open.forEach(m => m.classList.remove("show", "closing")), 190);
  if(modalOpener && modalOpener.focus) modalOpener.focus();
};
// Old history rows carry the pre-rename vocabulary; the log displays the
// current one. Mirrors LEGACY_STATUS + LEGACY_PERSON_STAGE in the pipeline.
const ACT_LEGACY = {"new":"review", shortlisted:"saved", drafted:"saved",
  responded:"interviewing", passed:"uninterested", dismissed:"uninterested",
  "to contact":"saved", "reached out":"contacted", replied:"conversation",
  meeting:"met", following:"saved"};
const ACT_FWD = new Set(["saved","applied","interviewing","offer","contacted","conversation","met"]);
const ACT_NEG = new Set(["uninterested","rejected"]);
window.openActivity = () => {
  const acts = DATA.activity || [];
  if(!acts.length){ toast("nothing recorded yet"); return; }
  const dayName = d => d === DATA.built ? "Today"
    : d === new Date(Date.parse(DATA.built) - 86400000).toISOString().slice(0,10) ? "Yesterday" : d;
  const days = {};
  acts.forEach(a => (days[a.at.slice(0,10)] ||= []).push(a));
  const actText = t => t.replace(/^moved to (.+)$/,
    (_, st) => "moved to " + (ACT_LEGACY[st.trim()] || st));
  const actCls = t => {
    const m = t.match(/^moved to (.+)$/); if(!m) return "";
    const st = ACT_LEGACY[m[1].trim()] || m[1].trim();
    return ACT_FWD.has(st) ? " act-fwd" : ACT_NEG.has(st) ? " act-neg" : "";
  };
  const html = Object.keys(days).sort().reverse().map(d =>
    `<div class="actday">${esc(dayName(d))}</div>` + days[d].map(a =>
      `<div class="actrow"><span class="acttime">${esc(a.at.slice(11,16) || "—")}</span>
         <span class="${actCls(a.t)}">${esc(actText(a.t))}</span><span class="co">${esc(short(a.s, 46))}</span></div>`).join("")
  ).join("");
  modalOpener = document.activeElement;
  el("act-list").innerHTML = html;
  el("actmodal").classList.add("show");
  el("act-list").focus();
};
// One-field fixes without the full edit form: click a dashed chip, get a
// small popover, save patches only that field (track_company ignores blanks).
const QE_FIELDS = {
  stage: ["Round", "select", ["pre_seed","seed","series_a","series_b","growth"]],
  site: ["Website URL", "input", "https://…"],
  linkedin: ["LinkedIn company URL", "input", "https://www.linkedin.com/company/…"],
  industry: ["Industry", "industry", ""],
  location: ["Location", "select-city", ""],
};
// Posting-level quick edits share the popover; job type is an explicit
// override of the title-derived bucket.
window.postingEdit = (url, field) => {
  const isLoc = field === "location";
  el("qe-title").textContent = (isLoc ? "Location — " : "Job type — ") + short((ROW_BY_URL[url]||{}).t||"", 30);
  el("qe-body").innerHTML = isLoc
    ? `<select id="qe-val" aria-label="Location">${CITY_ORDER.filter(c=>c!=="Other").map(c=>`<option>${c}</option>`).join("")}</select>`
    : `<select id="qe-val" aria-label="Job type">${(DATA.role_order||[]).map(v=>`<option>${v}</option>`).join("")}</select>`;
  el("qe-save").onclick = async () => {
    const v = el("qe-val").value;
    try {
      await api("/api/posting-edit", isLoc ? {url, location: v} : {url, role_type: v});
      closeOnly("qeditmodal"); toast("saved"); setTimeout(softReload, 300);
    } catch(e){ toast(String(e.message || e)); }
  };
  modalOpener = document.activeElement;
  el("qeditmodal").classList.add("show");
  el("qe-val").focus();
};
window.openHistory = (u) => {
  const r = ROW_BY_URL[u]; if(!r) return;
  el("hist-title").textContent = "History — " + short(dispTitle(r), 42);
  el("hist-body").innerHTML = (r.hist||[]).map(ev =>
    `<div class="ev">${esc(ev.at)} · ${esc(ev.t)}${ev.d?` — ${esc(ev.d)}`:""}</div>`).join("")
    || '<div class="why">no events recorded</div>';
  modalOpener = document.activeElement;
  el("histmodal").classList.add("show");
};
window.quickEdit = (name, field) => {
  const [label, kind, extra] = QE_FIELDS[field];
  let control;
  if(kind === "select")
    control = `<select id="qe-val" aria-label="${esc(label)}">${extra.map(v=>`<option value="${v}">${tc(v.replace("_"," "))}</option>`).join("")}</select>`;
  else if(kind === "industry")
    control = `<select id="qe-val" aria-label="${esc(label)}">${(window.IND_OPTS||[])
      .map(i=>`<option value="${esc(i)}">${esc(tc(i))}</option>`).join("")}<option value="other">Other</option></select>`;
  else if(kind === "select-city")
    control = `<select id="qe-val" aria-label="${esc(label)}">${CITY_ORDER.map(c=>`<option>${c}</option>`).join("")}</select>`;
  else
    control = `<input id="qe-val" aria-label="${esc(label)}" placeholder="${esc(extra)}">`;
  el("qe-title").textContent = `${label} — ${coDisp(name)}`;
  el("qe-body").innerHTML = control;
  el("qe-save").onclick = async () => {
    const v = el("qe-val").value.trim();
    if(!v) { closeOnly("qeditmodal"); return; }
    try {
      await api("/api/track", {company: name, [field]: v, follow: false});
      closeOnly("qeditmodal");
      toast(`${label.toLowerCase()} saved`);
      setTimeout(softReload, 300);
    } catch(e){ toast(String(e.message || e)); }
  };
  modalOpener = document.activeElement;
  el("qeditmodal").classList.add("show");
  el("qe-val").focus();
};
// One sweep affordance per card: its state, or the button to queue one.
// The sweep itself (Emory + NMH people pages, profile verification, mutual
// capture, and storing any LinkedIn URL it discovers) runs in a Chrome
// session against data/alumni_queue.json.
function sweepStatus(c){
  const sw = c.sweep || {};
  if(sw.state === "done")
    return `<span class="cchip sweepdone" title="Founders, alumni + mutuals screened on LinkedIn; every hit verified on their profile">✓ Screened${sw.when?` ${esc(ago(sw.when))}`:""}</span>`;
  if(sw.state === "queued")
    return `<span class="cchip sweepq" title="Waiting for the next Chrome sitting">Founders autofill queued</span>`;
  if(sw.state === "blocked")
    return `<span class="cchip missing" title="${esc(sw.why||"")}">Can't sweep — ${esc(sw.why||"no LinkedIn")}</span>`;
  return "";
}
function sweepBtn(c){
  const st = (c.sweep||{}).state;
  if(st && st !== "none") return "";   // a chip in the metadata row tells the state
  return LIVE?`<button class="btn autobtn" onclick="event.stopPropagation();autoComplete('${jsq(c.name)}','people')">Autofill Founders</button>`:"";
}
// One button fills every gap it can reach: the company's own site for
// description/LinkedIn, the founder-lookup queue when nobody's on file,
// and the people-sweep queue. Stage arrives via EDGAR when they raise —
// nothing here invents one.
// Re-pull a posting's page when its text is stale, wrong, or missing.
window.personAutofill = async (linkedin, name) => {
  try {
    await api("/api/person-fill-queue", {linkedin, name});
    toast("queued — the next Chrome sitting fills them from their profile");
    setTimeout(softReload, 300);
  } catch(e){ toast(String(e.message || e)); }
};
window.setInterviewStage = async (u, stage) => {
  try { await api("/api/interview-stage", {url: u, stage});
        toast(stage ? "interview stage: " + stage : "stage cleared");
        setTimeout(softReload, 300); }
  catch(e){ toast(String(e.message || e)); }
};
window.jobAutofill = async (u) => {
  try {
    const d = await api("/api/job-autofill", {url: u});
    if(d.ok){ delete DESC[u]; toast("description re-pulled from the posting page");
      if(MODAL_CARD && MODAL_CARD.key === u) openCard("post", u); }
    else toast(d.error || "couldn't improve it");
  } catch(e){ toast(String(e.message || e)); }
};
window.autoComplete = async (name, lane) => {
  try {
    const co = (DATA.companies||[]).find(x => x.name === name) || {};
    const d = await api("/api/company-auto", {company: name, lane: lane || "both", site: co.site || ""});
    toast(d.did && d.did.length ? d.did.join(" · ") : "nothing missing that I can reach");
    setTimeout(softReload, 400);
  } catch(e){ toast(String(e.message || e)); }
};
window.queueAlumni = async (name) => {
  try {
    const d = await api("/api/queue-alumni", {company: name});
    toast(d.queued ? "queued for the alumni + mutuals sweep" : "already queued or fully swept");
  } catch(e){ toast(String(e.message || e)); }
};
let QPAGE = {wait: 0, srv: 0, done: 0};
const QPAGE_SIZE = 5;
window.qpage = (k, dd) => { QPAGE[k] = Math.max(0, QPAGE[k] + dd); renderQueuePanel(); };
function qpager(k, n){
  const pages = Math.ceil(n / QPAGE_SIZE);
  if(pages <= 1) return "";
  const pg = Math.min(QPAGE[k], pages - 1); QPAGE[k] = pg;
  return `<span class="kbpager"><button type="button" class="kbpg" ${pg===0?"disabled":""}
      onclick="qpage('${k}',-1)">‹</button><span class="co">${pg+1}/${pages}</span>
    <button type="button" class="kbpg" ${pg===pages-1?"disabled":""} onclick="qpage('${k}',1)">›</button></span>`;
}
const qslice = (arr, k) => arr.slice(QPAGE[k]*QPAGE_SIZE, (QPAGE[k]+1)*QPAGE_SIZE);
function renderQueueList(){
  const d = DATA.diag || {};
  const TIPS = {"Founders": "founders, alumni + mutual connections at this company — runs in your Chrome",
    "Individual": "fills this profile from LinkedIn — runs in your Chrome",
    "Company": "description, LinkedIn, founder names from its own site + SEC — server-side"};
  const item = (label, cls) => `<span class="cchip ${cls||""}" title="${esc(TIPS[label]||"")}">${label}</span>`;
  const waitAll = [
    ...(d.alumni_queue||[]).map(n => `<div class="drow qrow"><span>${item("Founders","sweepq")}
      <button type="button" class="linklike" onclick="closeModal();gotoCompany('${jsq(n.toLowerCase())}')">${esc(coDisp(n))}</button></span><span class="co">waiting</span></div>`),
    ...(d.fill_queue||[]).map(n => `<div class="drow qrow"><span>${item("Individual","sweepq")} ${esc(n)}</span><span class="co">waiting</span></div>`),
  ];
  const srvAll = (d.founder_queue||[]).map(n => `<div class="drow qrow"><span>${item("Company","")} ${esc(coDisp(n))}</span><span class="co">waiting</span></div>`);
  const wait = qslice(waitAll, "wait").join("");
  const srv = qslice(srvAll, "srv").join("");
  const runsAll = (d.sitting_log||[]).map(r => `<div class="drow qrow">
      <span>${r.status === "ok" ? '<span class="ddot d-on"></span>' :
             r.status === "empty" ? '<span class="ddot d-alt"></span>' :
             '<span class="ddot d-off"></span>'}${esc(r.summary || r.status)}</span>
      <span class="co">${esc((r.at||"").slice(0,16).replace("T"," "))}</span></div>`);
  const runs = qslice(runsAll, "runs").join("");
  const doneMerged = [
    ...(d.recent_swept||[]).map(x => ({at: x.at, name: x.c, what: "screened for alums", link: true})),
    ...(d.autofill_log||[]).map(x => ({at: (x.at||"").slice(0,10), atFull: x.at, name: x.name,
      what: (x.kind||"autofill").toLowerCase(), link: x.kind === "Company details"})),
  ].sort((a,b) => (b.atFull||b.at||"").localeCompare(a.atFull||a.at||""));
  const doneAll = doneMerged.map(x => `<div class="drow qrow">
      <span><span class="doneck">✓</span>
      ${x.link?`<button type="button" class="linklike" onclick="closeModal();gotoCompany('${jsq((x.name||"").toLowerCase())}')">${esc(coDisp(x.name))}</button>`:esc(coDisp(x.name||""))}</span>
      <span class="co">${esc(x.what)} · ${esc(ago(x.at))}</span></div>`);
  const doneRows = qslice(doneAll, "done").join("");
  return {wait, waitN: waitAll.length, srv, srvN: srvAll.length, runs, runsN: runsAll.length, doneRows, doneN: doneAll.length};
}
window.queueBoardSweep = async () => {
  try {
    await api("/api/queue-board-sweep", {board: "wellfound"});
    metal("accent"); toast("Wellfound queued for the next Chrome sitting");
    setTimeout(softReload, 300); setTimeout(renderQueuePanel, 600);
  } catch(e){ toast(String(e.message || e)); }
};
window.openQueue = () => {
  const d = DATA.diag || {};
  const hrs = d.sitting_hours || [];
  let nextRun = "no sitting scheduled";
  if(hrs.length){
    const now = new Date(); let t = null;
    for(const h of hrs){ const c = new Date(now); c.setHours(h, 4, 0, 0); if(c > now){ t = c; break; } }
    if(!t){ t = new Date(now); t.setDate(t.getDate() + 1); t.setHours(hrs[0], 4, 0, 0); }
    const mins = Math.round((t - now) / 60000);
    nextRun = `next sitting ≈ ${t.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"})}` +
      (t.getDate() !== now.getDate() ? " tomorrow" : "") + (mins < 90 ? ` (${mins}m)` : "");
  }
  el("queuemodal").dataset.nextrun = nextRun;
  renderQueuePanel();
  modalOpener = document.activeElement;
  el("queuemodal").classList.add("show");
  el("queue-list").focus();
};
function renderQueuePanel(){
  const q = renderQueueList();
  // The run LOG only matters when something went wrong — one status line
  // covers the happy path, and a failure gets said in red.
  const last = ((DATA.diag||{}).sitting_log||[])[0];
  const lastLine = !last ? "no sittings have run yet" :
    `${last.status === "ok" ? '<span class="ddot d-on"></span>'
      : last.status === "empty" ? '<span class="ddot d-alt"></span>'
      : '<span class="ddot d-off"></span>'}last sitting ${esc((last.at||"").slice(11,16))} — ${esc(last.summary || last.status)}`;
  el("queue-list").innerHTML = `
    <div class="qsec">
    <div class="actday">Chrome · ${esc(el("queuemodal").dataset.nextrun||"")}</div>
    <div class="dfacts" style="margin-bottom:4px"><span>${lastLine}</span></div>
    <div class="dfacts" style="margin-bottom:6px"><span>${
      ((DATA.diag||{}).board_sweep_queue||[]).length
        ? `Wellfound sweep queued for the next sitting (${esc((DATA.diag||{}).board_sweep_queue[0].queued_at||"")})`
        : `<button class="btn" onclick="queueBoardSweep()">Queue Wellfound Sweep</button> <span class="co">runs in your Chrome next sitting</span>`}</span></div>
    ${q.wait || '<div class="dfacts"><span>nothing waiting on Chrome</span></div>'}
    <div class="qcenter">${qpager("wait", q.waitN)}</div></div>
    <div class="qsec">
    <div class="actday">Server · drains on the next enrichment pass</div>
    ${q.srv || '<div class="dfacts"><span>nothing waiting on the server</span></div>'}
    <div class="qcenter">${qpager("srv", q.srvN)}</div></div>
    <div class="qsec">
    <div class="actday">Recently autofilled</div>
    ${q.doneRows || '<div class="dfacts"><span>none yet</span></div>'}
    <div class="qcenter">${qpager("done", q.doneN)}</div></div>`;
}
window.openDiag = () => {
  const d = DATA.diag || {};
  const t = d.totals || {};
  const dot = tier => tier === "skip" ? `<span class="ddot d-off" title="retired"></span>`
    : tier === "derived" ? `<span class="ddot d-alt" title="not a board — derived source"></span>`
    : `<span class="ddot d-on" title="live board"></span>`;
  const pct = (a,b) => b ? Math.round(100*a/b) + "%" : "—";
  const sorted = [...(d.boards||[])].sort((a,b) =>
    (a.tier==="skip") - (b.tier==="skip") ||
    (b.scored ? b.hot/b.scored : -1) - (a.scored ? a.hot/a.scored : -1));
  const boardRows = sorted.map(b => `<div class="drow${b.tier==="skip"?" doff":""}">
      <span>${dot(b.tier)}${esc(b.name === "?" ? "(unattributed)" : srcDisp(b.name))}</span>
      <span>${b.entries}</span><span>${b.scored}</span><span>${b.hot}</span>
      <span>${pct(b.hot, b.scored)}</span>
      <span>${b.last ? esc(ago(b.last)) : "—"}</span>
    </div>${b.reason?`<div class="dwhy">${esc(b.reason)}</div>`:""}`).join("");
  const mini = (n2, l) => `<div class="dtile${n2 ? "" : " zero"}"><div class="dtnum">${n2}</div><div class="dtlabel">${l}</div></div>`;
  const live = sorted.filter(b => b.tier !== "skip" && b.entries);
  const dead = sorted.filter(b => b.tier === "skip");
  const boardRows2 = live.map(b => `<div class="drow srow">
      <span>${dot(b.tier)}${esc(b.name === "?" ? "(unattributed)" : srcDisp(b.name))}</span>
      <span>${b.entries}</span>
      <span>${b.scored ? `${b.hot} <span class="co">(${pct(b.hot, b.scored)})</span>` : '<span class="co">unscored</span>'}</span>
      <span class="co">${b.last ? esc(ago(b.last)) : "—"}</span>
    </div>`).join("");
  el("diag-list").innerHTML = `
  <div class="dsec dwide">
    <h4>Sources <span class="co">ranked by hit rate</span></h4>
    <div class="drow srow dhead"><span>source</span><span>entries</span><span>70+ (hit)</span><span>last pull</span></div>
    ${boardRows2}
    ${dead.length ? `<div class="dwhy" style="padding-top:8px">retired: ${dead.map(b =>
      `${esc(srcDisp(b.name))}${b.reason ? ` <span title="${esc(b.reason)}">ⓘ</span>` : ""}`).join(" · ")}</div>` : ""}
  </div>
  <div class="dsec">
    <h4>Pipeline</h4>
    <div class="dtiles">
      ${mini(t.entries||0, "entries")}
      ${mini((t.entries||0)-(t.unscored||0), "scored")}
      ${mini(t.hot||0, "scored 70+")}
      ${mini(t.descs||0, "descriptions")}
      ${mini(t.calibration||0, "calibration")}
      ${mini(t.connections||0, "connections")}
    </div>
    <div class="dnote">${d.scheduled ? "daily scout installed" : "no daily scout — roles only arrive when you pull (<code>make schedule</code>)"}
      · sittings every 3h, 9am–9pm, while the app is open</div>
    ${t.connections ? "" : `<div class="dnote">no connections.csv yet — export from LinkedIn → data/ for 1st° chips</div>`}
  </div>
  <div class="dsec" id="diag-scorer"><h4>Scorer vs your verdicts</h4>
    <div class="dnote">${LIVE ? "loading…" : "start the server for the live readback"}</div></div>
  <div class="dsec" id="diag-taste"><h4>Your taste, in your own words</h4>
    <div class="dnote">…</div></div>
  <div class="dsec" id="diag-outreach"><h4>Outreach</h4>
    <div class="dnote">…</div></div>`;
  if(LIVE) api("/api/insights").then(d2 => {
    if(!d2.data) return;
    const a = d2.data.agreement || {};
    const gap = (a.mean_pos != null && a.mean_neg != null) ? a.mean_pos - a.mean_neg : null;
    const sc = el("diag-scorer");
    if(sc){
      const missRow = (x, label) => `<div class="drow qrow"><span><span class="cchip">${label}</span>
        <button type="button" class="linklike" onclick="closeModal();gotoPosting('${jsq(x.u)}')">${esc(short(x.t, 30))}</button></span>
        <span class="co">${x.s}</span></div>`;
      const misses = [
        ...(a.false_high||[]).slice(0,3).map(x => missRow(x, "Overrated")),
        ...(a.false_low||[]).slice(0,3).map(x => missRow(x, "Underrated")),
      ].join("");
      sc.innerHTML = `<h4>Scorer vs your verdicts</h4>
        <div class="dtiles">
          <div class="dtile"><div class="dtnum">${a.mean_pos ?? "—"}</div><div class="dtlabel">your keeps avg</div></div>
          <div class="dtile"><div class="dtnum">${a.mean_neg ?? "—"}</div><div class="dtlabel">your passes avg</div></div>
          <div class="dtile"><div class="dtnum qfinds">${gap != null ? "+" + gap : "—"}</div><div class="dtlabel">gap (bigger = better)</div></div>
        </div>
        ${misses ? `<div class="dnote">where it disagrees:</div>${misses}` : ""}`;
    }
    const ta = el("diag-taste");
    if(ta){
      const reasons = (d2.data.reasons||[]).slice(0,8).map(([g, n2]) =>
        `<span class="cchip" title="appears in ${n2} of your pass notes">${esc(g)} ×${n2}</span>`).join(" ");
      ta.innerHTML = `<h4>Your taste, in your own words</h4>
        ${reasons ? `<div class="dchips">${reasons}</div>
          <div class="dnote">a recurring reason is a rule CLAUDE.md doesn't state yet</div>`
          : '<div class="dnote">no recurring pass reasons yet — passes with notes teach the most</div>'}`;
    }
    const ou = el("diag-outreach");
    if(ou){
      const sent = d2.data.outreach ? Object.entries(d2.data.outreach.sent||{}) : [];
      ou.innerHTML = `<h4>Outreach</h4>` + (sent.length
        ? sent.map(([proof, n2]) => `<div class="drow qrow"><span>${esc(proof)}</span>
            <span class="co">${(d2.data.outreach.replied||{})[proof]||0}/${n2} replied</span></div>`).join("")
        : `<div class="dnote">no sends recorded yet — the first applied posting starts answering which proof point opens doors</div>`);
    }
  }).catch(() => {});
  modalOpener = document.activeElement;
  el("diagmodal").classList.add("show");
  el("diag-list").focus();
};
window.openModal = (text) => {
  modalOpener = document.activeElement;
  el("modal-pre").textContent = text;
  el("modal").classList.add("show");
  el("modal-pre").focus();
};
window.personFollow = async (name, company, following) => {
  try { await api("/api/person-follow", {name, company, following});
        toast((following ? "following " : "unfollowed ") + name.split(" ")[0]);
        setTimeout(softReload, 300); }
  catch(e){ toast(String(e.message || e)); }
};
// Pasting a profile URL guesses the name from the slug; the Chrome-session
// sweep fills the rest (role, company, education) — profiles are login-walled,
// so the server can't.
function nameFromLinkedIn(url){
  const m = (url||"").match(/linkedin\.com\/in\/([a-z0-9-]+)/i);
  if(!m) return "";
  return m[1].replace(/-[0-9a-f]{6,}$/i, "").replace(/\d+$/, "")
    .split("-").filter(Boolean).map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ").trim();
}
window.editPerson = (pid) => {
  const [name, company] = pid.split("|");
  const pv = (DATA.people||[]).find(q => q.name === name && q.company === company);
  if(!pv) return;
  openForm("person");
  el("fp-title").textContent = "Edit " + pv.name;
  el("fp-save").textContent = "Save";
  el("fp-name").value = pv.name; el("fp-company").value = pv.company || "";
  el("fp-role").value = pv.role || ""; el("fp-rel").value = ["founder","employee","recruiter"].includes(pv.rel) ? pv.rel : "contact";
  // The ternary this replaces couldn't round-trip Family: open a Family
  // contact's form and the select read blank, so saving quietly dropped it.
  el("fp-signal").value =
    ["Emory","NMH","Sports","Family"].find(s => (pv.signals||[]).includes(s)) || "";
  el("fp-linkedin").value = pv.linkedin || ""; el("fp-email").value = pv.email || "";
  el("fp-x").value = pv.x || ""; el("fp-phone").value = pv.phone || "";
  el("fp-loc").value = pv.loc || "";
  el("fp-warm").value = pv.warm ? "1" : ""; el("fp-wvia").value = pv.wvia || "";
  el("fp-notes").value = pv.notes || "";
  warmVia();
};
// warm-via only means something on a warm lead — greyed out for cold
window.warmVia = () => { el("fp-wvia").disabled = el("fp-warm").value !== "1"; };
// Fill what we can from a pasted LinkedIn URL: an exact match against people
// already in the pipeline wins; otherwise the slug at least yields the name.
// No network call — nothing is looked up, only reused or parsed.
// Pasting into the form does nothing on its own (Eric, 2026-08-06) — the
// Autofill button below is the one explicit path to the fill queue.
window.openForm = (which) => {
  modalOpener = document.activeElement;
  el("form-person").hidden = which !== "person";
  el("form-company").hidden = which !== "company";
  if(which === "company"){ CO_EDITING = false;
    el("fc-title").textContent = "Track a Company"; el("fc-save").textContent = "Follow Company";
    el("fc-name").readOnly = false;
    ["fc-name","fc-desc","fc-why","fc-site","fc-linkedin","fc-loc"].forEach(i => el(i).value = "");
    el("fc-ind").value = ""; el("fc-round").value = ""; }
  if(which === "person"){ el("fp-title").textContent = "Add a Person"; el("fp-save").textContent = "Add Person";
    ["fp-name","fp-company","fp-role","fp-linkedin","fp-email","fp-x","fp-phone","fp-loc","fp-wvia","fp-notes"].forEach(i => el(i).value = "");
    el("fp-warm").value = ""; warmVia();
    el("fp-rel").value = "contact"; el("fp-signal").value = ""; }
  el("formmodal").classList.add("show");
  el(which === "person" ? "fp-name" : "fc-name").focus();
};
document.addEventListener("keydown", ev => {
  if(ev.key !== "Escape" || !document.querySelector(".modal.show")) return;
  if(el("qeditmodal").classList.contains("show")) closeOnly("qeditmodal");
  else if(el("histmodal").classList.contains("show")) closeOnly("histmodal");
  else closeModal();
});
// One-key shortcuts (never while typing, never with a modifier held);
// holding ⌥ peeks the cheat-sheet in the corner, ? pins it.
let KBPIN = false, KBTIMER = null;
const kbShow = on => {
  const showing = on || KBPIN;
  el("kbhelp").classList.toggle("show", showing);
  el("kbhint").classList.toggle("hide", showing);   // the hint morphs away
};
// A click-peek always times out; only ? pins it open.
const kbPeek = () => {
  kbShow(true);
  clearTimeout(KBTIMER);
  KBTIMER = setTimeout(() => { if(!KBPIN) kbShow(false); }, 3500);
};
document.addEventListener("keydown", ev => {
  if(ev.key === "Alt"){ kbShow(true); return; }
  if(ev.metaKey || ev.ctrlKey) return;
  const tag = (ev.target.tagName || "").toLowerCase();
  if(["input","textarea","select"].includes(tag) || ev.target.isContentEditable) return;
  // "Hold ⌥ for Shortcuts" reads as "press the key WHILE holding ⌥" — honor
  // that gesture too. macOS rewrites ⌥2 into "™", so recover the key from
  // ev.code when ⌥ is down.
  let k = ev.key;
  if(ev.altKey){
    const c = ev.code || "";
    if(c.startsWith("Digit")) k = c.slice(5);
    else if(c.startsWith("Key")) k = c.slice(3).toLowerCase();
    else if(/^[0-9a-z]$/i.test(k)) k = k.toLowerCase();
    else if(k !== "Escape") return;
  }
  if(k === "1") switchTab("tracker");
  else if(k === "2") switchTab("postings");
  else if(k === "3") switchTab("tweets");
  else if(k === "4") switchTab("companies");
  else if(k === "5") switchTab("people");
  else if(k === "/"){ ev.preventDefault(); openSearch(); }
  else if(k === "a" && LIVE) el("fab-add").click();
  else if(k === "Escape") closeModal();
  else if(k === "f" && LIVE && !el("refresh").disabled) el("refresh").click();
  else if(k === "t") el("themebtn").click();
  else if(k === "?"){ KBPIN = !KBPIN; kbShow(false); }
});
document.addEventListener("keyup", ev => { if(ev.key === "Alt") kbShow(false); });
el("kbhint").addEventListener("click", kbPeek);
document.querySelector(".hword").addEventListener("click", function(){
  this.classList.remove("party"); void this.offsetWidth; this.classList.add("party");
  metal("blast");
});
window.addEventListener("blur", () => kbShow(false));
document.querySelectorAll(".modal").forEach(m =>
  m.addEventListener("click", ev => { if(ev.target === m){
    if(m.id === "qeditmodal" || m.id === "histmodal") closeOnly(m.id); else closeModal();
  } }));

function _dispatchPending(date){
  if(!_pendingStage) return;
  const p = _pendingStage; _pendingStage = null;
  if(p.kind === "posting"){ _applyPostingStage({...p, date}); return; }
  const extra = el("dm-methodrow").hidden ? {} :
    {method: el("dm-method").value, detail: el("dm-detail").value.trim()};
  _applyPersonStage({...p, date, ...extra});
}
el("dm-method").addEventListener("change", () => {
  el("dm-detail").placeholder = DM_HINTS[el("dm-method").value] || "";
  el("dm-detail").disabled = el("dm-method").value === "linkedin";
});
el("dm-today").addEventListener("click", () => { metal("accent"); _dispatchPending(""); closeModal(); });
el("dm-save").addEventListener("click", () => { metal("accent"); _dispatchPending(el("dm-date").value); closeModal(); });

(async () => {
  try {
    await api("/api/ping");
    LIVE = true;
    const rf = el("refresh");
    rf.hidden = false;
    el("activity-btn").addEventListener("click", openActivity);
    el("queue-btn").addEventListener("click", openQueue);
    el("about-btn").addEventListener("click", () => {
      modalOpener = document.activeElement;
      el("aboutmodal").classList.add("show");
    });
    el("diag-btn").addEventListener("click", openDiag);

    const scoutGo = async () => {
      metal("blast");
      rf.disabled = true; rf.textContent = "Searching…";
      el("fab-scout").disabled = true;
      try { await api("/api/scout", {}); watchScout(); }
      catch(e){ toast(String(e.message || e)); rf.disabled = false; rf.textContent = "Find New Roles";
                el("fab-scout").disabled = false; }
    };
    rf.addEventListener("click", scoutGo);
    el("fab-scout").addEventListener("click", scoutGo);
    const p0 = await api("/api/ping");
    if(p0.scout && p0.scout.running) { rf.disabled = true; rf.textContent = "Searching…";
      el("fab-scout").disabled = true; watchScout(); }
    el("triagebtn").addEventListener("click", startTriage);
    el("fab-triage").addEventListener("click", startTriage);
    const addGo = () => {
      if(TAB === "postings"){
        ["ap-url","ap-title","ap-company"].forEach(id => el(id).value = "");
        modalOpener = document.activeElement;
        el("addpostmodal").classList.add("show");
        el("ap-url").focus();
        return;
      }
      openForm(TAB === "companies" ? "company" : "person");
    };
    el("ap-save").addEventListener("click", async () => {
      const url = el("ap-url").value.trim();
      const title = el("ap-title").value.trim(), company = el("ap-company").value.trim();
      // off-market roles have no URL — the server synthesizes a stable
      // manual:// key from title + company instead
      if(!url && !(title && company))
        return toast("paste a URL — or give both title and company for an off-market role");
      const body = {};
      if(url) body.url = url;
      if(title) body.title = title;
      if(company) body.company = company;
      try {
        const d = await api("/api/add-posting", body);
        closeOnly("addpostmodal");
        toast(`saved ${d.title || "posting"}${d.company ? " @ " + d.company : ""}`);
        setPostingMode("saved");   // land where the new card actually is
        setTimeout(softReload, 350);
      } catch(e){ toast(String(e.message || e)); }   // 409 reads "already in the ledger"
    });
    el("ap-url").addEventListener("keydown", ev => {
      if(ev.key === "Enter") el("ap-save").click(); });
    el("addbtn").addEventListener("click", addGo);
    el("fab-add").addEventListener("click", addGo);
    el("fp-save").addEventListener("click", async () => {
      const name = el("fp-name").value.trim();
      if(!name) return toast("enter a name first");
      try {
        const d = await api("/api/person", {
          name, company: el("fp-company").value.trim(), role: el("fp-role").value.trim(),
          relationship: el("fp-rel").value, signal: el("fp-signal").value,
          linkedin: el("fp-linkedin").value.trim(), email: el("fp-email").value.trim(),
          x: el("fp-x").value.trim(), phone: el("fp-phone").value.trim(),
          location: el("fp-loc").value.trim(),
          warm: el("fp-warm").value === "1", warm_via: el("fp-wvia").value.trim(),
          notes: el("fp-notes").value.trim(),
        });
        toast("added " + name + (d.fill_queued ? " — profile fill queued for the next sitting" : ""));
        // Land back on People with the Saved filter — a new person is
        // status "saved", and the default Review filter would hide them.
        try { localStorage.setItem("tab", "people");
              localStorage.setItem("pstage-after", "saved"); } catch(_){}
        openForm("person");   // wiped and refocused for the next entry
        setTimeout(softReload, 350);
      } catch(e){ toast(String(e.message || e)); }
    });
    el("fp-autofill").addEventListener("click", async () => {
      const li = (prompt("LinkedIn profile URL to autofill from:",
        el("fp-linkedin").value.trim()) || "").trim();
      if(!li) return;
      if(!li.includes("linkedin.com/in/")){ toast("that isn't a linkedin.com/in/… URL"); return; }
      el("fp-linkedin").value = li;
      const nm = el("fp-name").value.trim() || nameFromLinkedIn(li);
      if(!confirm(`Add ${nm || "this person"} and queue their profile for the next Chrome sitting?`)) return;
      try {
        await api("/api/person", {name: nm, company: el("fp-company").value.trim(),
          linkedin: li, relationship: el("fp-rel").value});
        await api("/api/person-fill-queue", {linkedin: li, name: nm});
        closeModal();
        toast(`${nm} added — profile fills at the next Chrome sitting`);
        setTimeout(softReload, 350);
      } catch(e){ toast(String(e.message || e)); }
    });
    el("fc-fetch").addEventListener("click", async () => {
      const url = el("fc-site").value.trim();
      if(!url){ toast("paste the company site URL first"); return; }
      el("fc-fetch").disabled = true; el("fc-fetch").textContent = "Fetching…";
      try {
        const d = await api("/api/company-info", {url, name: el("fc-name").value.trim()});
        if(d.description && !el("fc-desc").value.trim()) el("fc-desc").value = d.description;
        if(d.linkedin && !el("fc-linkedin").value.trim()) el("fc-linkedin").value = d.linkedin;
        if(d.site) el("fc-site").value = d.site;
        toast(d.description || d.linkedin ? "pulled what the site says about itself" : "site reachable — nothing quotable found");
      } catch(e){ toast(String(e.message || e)); }
      el("fc-fetch").disabled = false; el("fc-fetch").textContent = "Fetch from site";
    });
    el("fc-autofill").addEventListener("click", async () => {
      // pipeline knowledge first, then the company's own site for the gaps
      setTimeout(() => { if(el("fc-site").value.trim() &&
        (!el("fc-desc").value.trim() || !el("fc-linkedin").value.trim()))
          el("fc-fetch").click(); }, 400);
      const name = el("fc-name").value.trim();
      if(!name) return toast("enter a company name first");
      try {
        const d = await api("/api/company-lookup", {name});
        if(!d.found) return toast("nothing on file for " + name + " — fill in manually");
        if(d.site && !el("fc-site").value) el("fc-site").value = d.site;
        if(d.linkedin && !el("fc-linkedin").value) el("fc-linkedin").value = d.linkedin;
        if(d.description && !el("fc-desc").value) el("fc-desc").value = d.description;
        toast("filled from what the pipeline already knows");
      } catch(e){ toast(String(e.message || e)); }
    });
    el("fc-save").addEventListener("click", async () => {
      const name = el("fc-name").value.trim();
      if(!name) return toast("enter a company name first");
      try {
        await api("/api/track", {company: name, description: el("fc-desc").value.trim(),
          why: el("fc-why").value.trim(),
          site: el("fc-site").value.trim(), linkedin: el("fc-linkedin").value.trim(),
          industry: el("fc-ind").value, stage: el("fc-round").value,
          location: el("fc-loc").value.trim(),
          follow: !CO_EDITING});
        toast(CO_EDITING ? "saved " + name : "now following " + name);
        if(CO_EDITING) closeOnly("formmodal");
        else openForm("company");   // wiped and refocused for the next entry
        setTimeout(softReload, 350);
      } catch(e){ toast(String(e.message || e)); }
    });
  } catch(_) {}
  // Every action reloads the page. The tab, the mode chips, and whatever was
  // expanded all come back — adding a note must not dump you into Review.
  window.addEventListener("beforeunload", () => {
    try { localStorage.setItem("viewstate", JSON.stringify({
      t: Date.now(), pmode: POSTING_MODE, cmode: COMPANY_MODE, pplmode: PEOPLE_MODE,
      modal: MODAL_CARD, pxp: [...PEOPLE_EXPANDED],
    })); } catch(_){}
  });
  let land = "tracker";
  try {
    const vs = JSON.parse(localStorage.getItem("viewstate") || "null");
    localStorage.removeItem("viewstate");
    if(vs && Date.now() - vs.t < 10*60*1000){
      POSTING_MODE = vs.pmode || "review";
      PEOPLE_MODE = vs.pplmode || "review";
      if(vs.cmode && vs.cmode !== "review") setCompanyMode(vs.cmode);
      (vs.pxp||[]).forEach(x => PEOPLE_EXPANDED.add(x));
      if(vs.modal && vs.modal.kind !== "person")
        setTimeout(() => openCard(vs.modal.kind, vs.modal.key), 60);
    }
    const pa = localStorage.getItem("pstage-after");
    if(pa){ PEOPLE_MODE = pa; localStorage.removeItem("pstage-after"); }
    land = localStorage.getItem("tab") || "tracker";
  } catch(_){}
  for(const [id, m] of [["pt-review","review"],["pt-saved","saved"]])
    el(id).addEventListener("click", () => setPeopleMode(m));
  for(const [id, m] of [["ps-review","review"],["ps-saved","saved"],["ps-uninterested","uninterested"]])
    el(id).addEventListener("click", () => setPostingMode(m));
  if(POSTING_MODE === "swept") POSTING_MODE = "uninterested";   // pre-migration viewstate
  if(POSTING_MODE !== "review") setPostingMode(POSTING_MODE);
  if(PEOPLE_MODE !== "review") setPeopleMode(PEOPLE_MODE);
  for(const [id, m] of [["tw-new","new"],["tw-saved","saved"],["tw-dismissed","dismissed"]])
    el(id).addEventListener("click", () => setTweetMode(m));
  render(); renderTracker(); renderCompanies(); renderPeople(); renderTweets();
  applyTabChrome("tracker");
  if(land !== "tracker") switchTab(land);
  // First-open splash: only plays while the tab is actually being looked at.
  try {
    if(!sessionStorage.getItem("splashed")){
      const sp = el("splash"); sp.hidden = false;
      const go = () => { sp.classList.add("splgo"); setTimeout(() => { sp.hidden = true; }, 620);
        try { sessionStorage.setItem("splashed", "1"); } catch(_){} };
      if(document.visibilityState === "visible") setTimeout(go, 850);
      else document.addEventListener("visibilitychange",
        () => setTimeout(go, 850), {once: true});
      setTimeout(go, 4000);   // a tab that never becomes visible still clears
    }
  } catch(_){}
})();

["q","minscore","sort","posted","hascontact","talkstage","csort","psort"].forEach(id=>{
  const rerun = () => { shown = PAGE; CO_SHOWN = 120;
    markDirty("postings", "companies", "people", "tweets"); };
  if(id === "minscore"){
    el(id).addEventListener("input", () => {
      el("minscore-lbl").textContent = +el(id).value > +el(id).min ? `≥${el(id).value}` : "Score";
      rerun(); });
    // the browser may restore a dragged value across the reload-every-action
    // cycle — the label must say so instead of reading "Score" (2026-08-12)
    if(+el(id).value > +el(id).min) el("minscore-lbl").textContent = `≥${el(id).value}`;
  } else if(id === "q"){
    // typing costs one render of the visible tab, 150ms after the last key
    let deb = null;
    el(id).addEventListener("input", () => {
      clearTimeout(deb); deb = setTimeout(rerun, 150); });
  } else {
    el(id).addEventListener("input", rerun);
    el(id).addEventListener("change", rerun);
  }
});
// Every dropdown hugs its CURRENT choice instead of reserving room for the
// longest option — pick a wider filter and it widens then.
let CO_SHOWN = 120;   // companies render in pages too — 1400+ cards froze the tab
const SELECT_DEFAULTS = {psort: "az", csort: "az", sort: "score"};
function fitSelect(sel){
  // a sort is never "active" — only filters earn the green (Eric, 2026-08-12)
  sel.classList.toggle("actv", !["sort","csort","psort"].includes(sel.id)
    && !!sel.value && sel.value !== (SELECT_DEFAULTS[sel.id] || ""));
  const tmp = document.createElement("span");
  tmp.style.cssText = "position:absolute;visibility:hidden;white-space:nowrap;" +
    "font-size:var(--t-sm);font-family:inherit";
  tmp.textContent = (sel.options[sel.selectedIndex] || {}).text || "";
  document.body.appendChild(tmp);
  sel.style.width = Math.ceil(tmp.getBoundingClientRect().width + 46) + "px";
  tmp.remove();
}
document.querySelectorAll("#controls select").forEach(sl => {
  fitSelect(sl);
  sl.addEventListener("change", () => fitSelect(sl));
});
// Two peer buttons, not one self-renaming toggle: the old button showed the
// view you were IN while looking like the view you'd get, and it read as one
// setting rather than two. Dividers fence the pair off from the sort.
window.setPeopleView = (v) => {
  PVIEW_SEL = v;
  for(const m of ["co","flat"]){
    const b = el("pview-" + m);
    if(!b) continue;
    b.classList.toggle("on", v === m);
    b.setAttribute("aria-pressed", String(v === m));
  }
  renderPeople();
};
for(const m of ["co","flat"])
  el("pview-" + m) && el("pview-" + m).addEventListener("click", () => setPeopleView(m));
function filtersActive(){
  if(el("q").value.trim()) return true;
  if(el("posted") && el("posted").value) return true;
  const ms = el("minscore");
  if(ms && +ms.value > +ms.min) return true;
  if(YQ.size || Object.values(MSEL).some(s => s.size)) return true;
  return Object.values(F).some(Boolean);
}
function updateClearBtn(){
  const cf = el("clearfil");
  if(cf) cf.style.display = filtersActive() ? "" : "none";
}
window.clearFilters = () => {
  ["posted"].forEach(id => {
    const s = el(id); if(s && s.value){ s.value = ""; fitSelect(s); }});
  Object.keys(MSEL).forEach(k => { MSEL[k].clear(); ddLabel(k); });
  document.querySelectorAll(".ddpanel input[data-dd]").forEach(cb => cb.checked = false);
  YQ.clear();
  ["le1","mid","hi","none"].forEach(k => { const c = el("yq-" + k);
    if(c) c.checked = false; });
  yqButtonLabel();
  el("q").value = ""; el("q").hidden = true; if(el("sq")) el("sq").value = "";
  const ms = el("minscore");
  if(ms){ ms.value = ms.min; el("minscore-lbl").textContent = "Score"; }
  Object.keys(F).forEach(f => { if(!F[f]) return; F[f] = false;
    const c = document.querySelector(`.chip[data-f="${f}"]`);
    if(c){ c.classList.remove("on"); c.setAttribute("aria-pressed", "false"); }});
  shown = PAGE; CO_SHOWN = 120;
  markDirty("postings", "companies", "people", "tweets");
};
el("clearfil").addEventListener("click", clearFilters);
const YQ_LBL = {le1: "≤1", mid: "2–3", hi: "4+", none: "no req"};
function yqButtonLabel(){
  const b = el("yqbtn"); if(!b) return;
  b.textContent = YQ.size ? [...YQ].map(k => YQ_LBL[k]).join(" · ") : "Experience";
  b.classList.toggle("actv", !!YQ.size);
}
["le1","mid","hi","none"].forEach(k => {
  el("yq-" + k).addEventListener("change", () => {
    if(el("yq-" + k).checked) YQ.add(k); else YQ.delete(k);
    yqButtonLabel();
    shown = PAGE; markDirty("postings");
  });
});
document.addEventListener("click", ev => {
  if(ev.target.closest(".ddwrap")) return;
  document.querySelectorAll(".ddpanel").forEach(p => { if(!p.hidden){
    p.hidden = true;
    p.parentElement.querySelector(".chip").setAttribute("aria-expanded", "false"); } });
});
document.querySelectorAll(".chip[data-f]").forEach(c=>{
  c.addEventListener("click", ()=>{
    F[c.dataset.f] = !F[c.dataset.f];
    c.classList.toggle("on", F[c.dataset.f]);
    c.setAttribute("aria-pressed", String(F[c.dataset.f]));
    shown = PAGE; CO_SHOWN = 120;
    // scheduler, not direct calls: rendering the two HIDDEN tabs here made
    // renderCompanies overwrite the dropdowns' posting-shaped facet counts
    // with company-shaped ones a frame later (NY read 6, truth was 62 —
    // 2026-08-13), on top of the wasted work
    markDirty("postings", "companies", "people");
  });
});
</script><div id="demobanner" style="position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:var(--z-top);
padding:7px 14px;border-radius:999px;font:600 12px/1.2 -apple-system,system-ui,sans-serif;
background:color-mix(in srgb,var(--bg,#111) 70%,transparent);color:var(--fg,#eee);
border:1px solid color-mix(in srgb,var(--fg,#eee) 18%,transparent);backdrop-filter:blur(14px);
-webkit-backdrop-filter:blur(14px);pointer-events:none;white-space:nowrap">
Demo · real public postings, fictional people and pipeline</div>
</body></html>
"""
