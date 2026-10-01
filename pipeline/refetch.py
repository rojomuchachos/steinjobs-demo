"""Bulk posting refetch: real descriptions in, dead links out.

Two of Eric's complaints (2026-08-17) share one root and one fix:

  - "years of experience are wrong/missing" and "why are new entries
    unscored" — because ~2,000 feed rows have no real description cached.
    Boards like Getro, TeamWork and Built In publish lists, not text, and
    newsletter imports carry a one-line stub. No text → no yoe chip, no
    score (no blind scoring).
  - "mark a job as no longer existing" — nobody was checking whether a
    posting's page still resolves.

One HTTP walk answers both: a page that loads refreshes the description
cache (same extraction as the app's Repopulate Job button, which now
imports from here); a page that is GONE (404/410) marks the entry
`expired`. Anything ambiguous — walls, timeouts, 5xx, JS-only pages —
changes nothing: a wall is not a death, and expiring a live role would
silently hide a real opportunity.

Zero tokens. This is plain HTTP; the scorer runs separately.
"""

from __future__ import annotations

import html as _h
import json
import re as _re
import sys
import time

from .models import Entry, normalize_url

# Cached text shorter than this is a stub (newsletter line, list-page
# snippet), not a posting: re-fetch it as if missing.
STUB_CHARS = 300

# Statuses whose pages get checked every run — Eric watches these.
WATCHED = ("saved", "applied", "interviewing")

# A dead page is only these. 403/429 are walls, 5xx is their outage,
# timeouts are anyone's guess — none of those kill a posting.
GONE = (404, 410)


def extract_text(html_text: str) -> str:
    """Visible text of a posting page; Inertia data-page fallback (WaaS)."""
    txt = _re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>",
                  " ", html_text)
    txt = _h.unescape(_re.sub(r"<[^>]+>", " ", txt))
    txt = _re.sub(r"\s+", " ", txt).strip()
    if len(txt) < 200:
        m = _re.search(r'data-page="([^"]+)"', html_text)
        if m:
            try:
                def _longest(o):
                    if isinstance(o, str):
                        return o
                    vals = (o.values() if isinstance(o, dict)
                            else o if isinstance(o, list) else [])
                    return max((_longest(v) for v in vals), key=len, default="")
                blob = _longest(json.loads(_h.unescape(m.group(1))))
                blob = _h.unescape(_re.sub(r"<[^>]+>", " ", blob))
                txt = _re.sub(r"\s+", " ", blob).strip()
            except (ValueError, TypeError):
                pass
    return txt


STATE = None  # set below; module-level so tests can monkeypatch the path


def ride_scout(entries: list[Entry]) -> dict | None:
    """The scout-riding shape: watched pages every run, the wide sweep weekly.

    Watched postings are few and Eric acts on them, so their liveness is
    checked on every scout. The descriptionless backlog is large and static,
    so it drains at most weekly — remembered in a state file, like funding's
    seen-memory.
    """
    from datetime import date, timedelta
    from pathlib import Path

    state_path = STATE or (Path(__file__).resolve().parent.parent
                           / "data" / ".cache" / "link_sweep.json")
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        state = {}
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    full = (state.get("last_full") or "") < week_ago
    counts = sweep(entries, cap=300 if full else 0)
    if full:
        state["last_full"] = date.today().isoformat()
        p = Path(state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state))
    return counts


# Ashby job pages are JS-rendered — a plain fetch returns an empty shell,
# which the first sweep miscounted as "walled" (231 postings, measured
# 2026-08-18 after Eric pushed back). Both Ashby and Greenhouse publish the
# text via JSON APIs; same endpoints ats.py scouts with. One request per
# ORG fills every posting under it, so results are memoized per sweep.
_ASHBY_URL = _re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", _re.I)
_GH_URL = _re.compile(r"greenhouse\.io/(?:embed/job_app\?[^#]*token=(\d+)|([^/?#]+)/jobs/(\d+))", _re.I)
# Lever, Workable, Workday (2026-08-18, Eric: get through the walled hosts
# free first). Same shape as Ashby/Greenhouse: public JSON, no browser.
_LEVER_URL = _re.compile(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})", _re.I)
_WORKABLE_URL = _re.compile(r"apply\.workable\.com/([^/?#]+)/j/([A-Za-z0-9]+)", _re.I)
_WORKDAY_URL = _re.compile(
    r"https?://([^/]+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/job/(.+?)(?:[?#]|$)", _re.I)

# Hosts only Eric's real logged-in Chrome can read (his rule: never headless).
# The sweep routes them to a sitting queue instead of hammering the wall.
_SITTING_HOSTS = ("linkedin.com", "wellfound.com")
FILL_QUEUE = None  # test override; default set in sweep()


def _ats_org_jobs(c, url: str, memo: dict) -> str:
    """Posting text via the ATS's JSON API, '' when not an ATS url / not found."""
    m = _ASHBY_URL.search(url)
    if m:
        org, job_id = m.group(1), m.group(2).lower()
        if org not in memo:
            memo[org] = {}
            try:
                r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true")
                if r.status_code == 200:
                    for j in r.json().get("jobs", []):
                        memo[org][str(j.get("id", "")).lower()] = (
                            j.get("descriptionPlain") or j.get("descriptionHtml") or "")
            except Exception:  # noqa: BLE001
                pass
        raw = memo[org].get(job_id, "")
        return _re.sub(r"\s+", " ", _h.unescape(_re.sub(r"<[^>]+>", " ", raw))).strip()
    m = _GH_URL.search(url)
    if m:
        job_id = m.group(1) or m.group(3)
        org = m.group(2)
        if org and job_id:
            try:
                r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{job_id}")
                if r.status_code == 200:
                    raw = _h.unescape(r.json().get("content") or "")
                    return _re.sub(r"\s+", " ",
                                   _h.unescape(_re.sub(r"<[^>]+>", " ", raw))).strip()
            except Exception:  # noqa: BLE001
                pass
    m = _LEVER_URL.search(url)
    if m:
        try:
            r = c.get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}")
            if r.status_code == 200:
                j = r.json()
                raw = (j.get("descriptionPlain") or j.get("description") or "") + " " + " ".join(
                    (li.get("text") or "") for lst in j.get("lists", []) for li in [lst]
                )
                return _re.sub(r"\s+", " ",
                               _h.unescape(_re.sub(r"<[^>]+>", " ", raw))).strip()
        except Exception:  # noqa: BLE001
            pass
    m = _WORKABLE_URL.search(url)
    if m:
        # Per-ACCOUNT, not per-job: the v2 per-job endpoint 404s (probed live
        # 2026-08-18), while this widget call carries every open role's full
        # description keyed by shortcode. One request fills the whole org.
        org, code = m.group(1), m.group(2).upper()
        key = f"workable:{org}"
        if key not in memo:
            memo[key] = {}
            try:
                r = c.get(f"https://apply.workable.com/api/v1/widget/accounts/{org}?details=true",
                          headers={"Accept": "application/json"})
                if r.status_code == 200:
                    for j in r.json().get("jobs", []):
                        memo[key][str(j.get("shortcode", "")).upper()] = (
                            " ".join(str(j.get(k) or "") for k in
                                     ("description", "requirements", "benefits")))
            except Exception:  # noqa: BLE001
                pass
        raw = memo[key].get(code, "")
        if raw:
            return _re.sub(r"\s+", " ",
                           _h.unescape(_re.sub(r"<[^>]+>", " ", raw))).strip()
    m = _WORKDAY_URL.search(url)
    if m:
        host, site, jobpath = m.group(1), m.group(2), m.group(3)
        tenant = host.split(".")[0]
        try:
            r = c.get(f"https://{host}/wday/cxs/{tenant}/{site}/job/{jobpath}",
                      headers={"Accept": "application/json"})
            if r.status_code == 200:
                raw = (r.json().get("jobPostingInfo") or {}).get("jobDescription") or ""
                return _re.sub(r"\s+", " ",
                               _h.unescape(_re.sub(r"<[^>]+>", " ", raw))).strip()
        except Exception:  # noqa: BLE001
            pass
    return ""


def fill_postings(postings: list, throttle: float = 0.25) -> int:
    """Fetch text for RawPostings that arrived without any, in place.

    Runs at INTAKE (Eric, 2026-08-18: nothing enters the pile unjudged), so a
    posting is usually scorable the moment it is found instead of waiting for
    the weekly sweep. Same free routes as sweep(): ATS JSON first, then the
    page itself. Silent on failure — the caller marks those `incomplete`.
    """
    from .boards.base import client

    filled = 0
    memo: dict = {}
    with client() as c:
        for p in postings:
            url = getattr(p, "url", "") or ""
            if not url.startswith("http"):
                continue
            txt = _ats_org_jobs(c, url, memo)
            if not looks_like_a_posting(txt):
                try:
                    r = c.get(url, headers={"Accept": "text/html"})
                    txt = extract_text(r.text) if r.status_code == 200 else ""
                except Exception:  # noqa: BLE001 — a wall is not a failure here
                    txt = ""
            if looks_like_a_posting(txt):
                p.description = txt[:8000]
                filled += 1
            time.sleep(throttle)
    return filled


# Pages that load fine and carry plenty of text, none of which describes THIS
# job: a company careers index listing ten titles, a consent banner, a shop
# nav, a raw theme blob. Caching those makes a posting look scorable when it
# isn't — and re-caching them every sweep undid hand purges on a loop (found
# 2026-09-03).
#
# The markers here are DEAD-PAGE statements, not careers vocabulary. A first
# cut also matched "join our team" and "open positions", which appear in
# thousands of perfectly real job ads — it purged 19 live 70+ postings before
# the false positives showed up. An index page is identified structurally
# instead: several apply-links on one page means several jobs on one page.
_DEAD_PAGE = _re.compile(
    r"there are currently no open positions|we don'?t have any open positions"
    r"|this job (?:was|has been) removed|job was removed at|has been filled"
    r"|no matching results|content is no longer available"
    r"|below is a list of positions",
    _re.I,
)
_APPLY_LINK = _re.compile(r"apply now|apply for this job|apply here", _re.I)
_INDEX_HEADING = _re.compile(r"current openings|open positions|all open roles", _re.I)
# A consent dialogue and nothing else: the give-away is the toggle copy, which
# never appears inside a real job description.
_CONSENT_ONLY = _re.compile(
    r"(cookie preferences|manage cookie preferences).{0,400}"
    r"(strictly necessary|only necessary cookies|decline all non-necessary)",
    _re.I | _re.S,
)


def looks_like_a_posting(text: str) -> bool:
    """False when the page is a careers index, consent wall or dead template."""
    t = (text or "").strip()
    if len(t) < STUB_CHARS:
        return False
    # The blob is sometimes preceded by the page title, so don't require it
    # to start the string (ServiceChannel/Fortive, 2026-09-03).
    if '"themeOptions"' in t[:600]:
        return False
    if _CONSENT_ONLY.search(t) or _DEAD_PAGE.search(t):
        return False
    # Several apply-links plus an index heading = a list of jobs, not one job.
    if len(_APPLY_LINK.findall(t)) >= 4 and _INDEX_HEADING.search(t):
        return False
    return True


def sweep(entries: list[Entry], cap: int = 300, throttle: float = 0.3,
          check_all_liveness: bool = False) -> dict:
    """Fetch missing/stub descriptions and expire dead links, blanks-first.

    Priority order inside the cap: watched statuses, then review rows
    newest-first (a fresh posting is worth text sooner than a stale one).
    Returns counts; mutates entries' status in place (caller saves).
    """
    from . import store
    from .boards.base import client

    cache = store.load()

    def needs_text(e: Entry) -> bool:
        return len(cache.get(normalize_url(e.url), "")) < STUB_CHARS

    watched = [e for e in entries if e.status in WATCHED and e.url.startswith("http")]
    # `incomplete` belongs here, not outside (found 2026-08-26): the intake
    # gate parks a posting as incomplete BECAUSE it has no text, and this is
    # the pass that fetches text. Leaving it out stranded 545 postings in the
    # one state the fetcher ignored — they could never promote to review.
    review = [e for e in entries
              if (e.status or "review") in ("review", "new", "incomplete")
              and e.url.startswith("http")
              and (needs_text(e) or check_all_liveness)]
    review.sort(key=lambda e: e.first_seen or "", reverse=True)
    batch = watched + review[: max(0, cap - len(watched))]

    counts = {"checked": 0, "described": 0, "expired": 0,
              "walled_or_flaky": 0, "no_text": 0, "queued_for_sitting": 0}
    ats_memo: dict = {}
    sitting: list[dict] = []
    with client() as c:
        for e in batch:
            counts["checked"] += 1
            # LinkedIn/Wellfound: Eric's-Chrome-only by rule. Queue for a
            # sitting instead of poking the wall headless.
            if any(h in e.url for h in _SITTING_HOSTS):
                if needs_text(e):
                    sitting.append({"url": e.url, "title": e.title,
                                    "company": e.company})
                continue
            # ATS JSON first: Ashby pages are JS shells to plain HTTP, and
            # one org-level API call carries text for every posting under it.
            ats_txt = _ats_org_jobs(c, e.url, ats_memo)
            if ats_txt and looks_like_a_posting(ats_txt):
                if needs_text(e):
                    cache[normalize_url(e.url)] = ats_txt[:8000]
                    counts["described"] += 1
                continue
            # An Ashby org board that loaded WITH jobs but without this one:
            # the posting is delisted — Ashby's API only serves open jobs.
            m = _ASHBY_URL.search(e.url)
            if m and ats_memo.get(m.group(1)) and m.group(2).lower() not in ats_memo[m.group(1)]:
                if e.status != "expired":
                    e.status = "expired"
                    e.status_note = "delisted from the company's Ashby board (auto link sweep)"
                counts["expired"] += 1
                continue
            try:
                r = c.get(e.url, headers={"Accept": "text/html"})
            except Exception:  # noqa: BLE001 — network flake: not a verdict
                counts["walled_or_flaky"] += 1
                continue
            if r.status_code in GONE:
                if e.status != "expired":
                    e.status = "expired"
                    e.status_note = "posting page gone (auto link sweep)"
                counts["expired"] += 1
            elif r.status_code == 200:
                txt = extract_text(r.text)
                if looks_like_a_posting(txt) and needs_text(e):
                    cache[normalize_url(e.url)] = txt[:8000]
                    counts["described"] += 1
                elif len(txt) < STUB_CHARS:
                    counts["no_text"] += 1
            else:
                counts["walled_or_flaky"] += 1
            time.sleep(throttle)
    store.save(cache)
    if sitting:
        from pathlib import Path
        qp = Path(FILL_QUEUE) if FILL_QUEUE else (
            Path(__file__).resolve().parent.parent / "data" / "posting_fill_queue.json")
        try:
            existing_q = json.loads(qp.read_text()).get("items", [])
        except (OSError, ValueError):
            existing_q = []
        known = {i["url"] for i in existing_q}
        fresh_q = [i for i in sitting if i["url"] not in known]
        if fresh_q:
            qp.write_text(json.dumps(
                {"instructions": "Walled posting pages (LinkedIn/Wellfound) — visit each in "
                                 "Eric's real Chrome during a sitting ('run the posting fill'), "
                                 "copy the visible description text, and write it into the "
                                 "description cache via make apply. Throttle per the sitting "
                                 "SKILL.md. Remove entries whose page is gone.",
                 "items": existing_q + fresh_q}, indent=1))
        counts["queued_for_sitting"] = len(fresh_q)
    print(f"  link sweep: {counts['checked']} checked · "
          f"{counts['described']} descriptions filled · "
          f"{counts['expired']} expired · "
          f"{counts['walled_or_flaky']} walled/flaky (untouched)"
          + (f" · {counts['queued_for_sitting']} queued for a Chrome sitting"
             if counts["queued_for_sitting"] else ""),
          file=sys.stderr)
    return counts
