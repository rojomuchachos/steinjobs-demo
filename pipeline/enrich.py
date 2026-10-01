"""Company enrichment for high-scoring postings.

CLAUDE.md: "For every posting scoring 75+, look up the company and add founder
names + titles + LinkedIn URLs, latest funding round + notable investors,
approximate headcount. Applying cold is the weakest move — the founder is the
real target."

Only Work at a Startup hands this over for free. Everything from generalist,
the ATS adapters and the imported shortlist arrives with no founder at all, so
the best-scoring role in the feed (Hera, 88) has nobody to write to. That is the
gap this closes.

Three sources, cheapest and most reliable first:

  1. **Already known** — WaaS postings carry founders in their payload. Never
     re-look-up something we already have exactly.
  2. **Company site** — /about, /team, /company pages, and the JSON-LD most
     marketing sites emit. Deterministic, free, no model call.
  3. **Model + web search** — the fallback for everything else.

Same two-backend split as scoring: `api` runs unattended, `agent` writes a queue
for Claude Code to fill in-session. Fields that can't be established are left
empty rather than guessed — a hallucinated founder name is worse than a blank
one, because the entire point is that Eric writes to a real person.

Warm paths are searched for too, against the specific threads in master_cv.md:
Emory, the Jets, UCSF/Neuroscape, the Carter Center, CIEE Prague, Foxino,
powerlifting, the PCT, metal/music, WWOOF. A shared thread is the opening line
the outreach voice spec asks for.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# One matcher, shared with the person-side signals so the two can't drift — they
# already did once: word-anchoring landed here 2026-08-07 and entities.signals_in
# kept substring-matching for another day.
from .entities import SPORTS_NEEDLES, signal_pattern
from .models import Entry

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "data" / "enrichment_queue.json"

MIN_SCORE = 75  # the spec's threshold

# Threads from master_cv.md that would make Eric not-a-stranger. Searched for in
# founder bios and company copy.
WARM_THREADS = {
    "emory": "Emory",
    "new york jets": "the NY Jets",
    "nfl": "the NFL / football analytics",
    "ucsf": "UCSF",
    "neuroscape": "UCSF Neuroscape",
    "carter center": "the Carter Center",
    "northfield mount hermon": "Northfield Mount Hermon",
    "foxino": "Foxino",
    "ciee": "CIEE Prague",
    "prague": "Prague",
    "czech*": "the Czech Republic",
    "powerlift*": "powerlifting",
    "pacific crest trail": "the PCT",
    "thru-hik*": "thru-hiking",
    "wwoof*": "WWOOF / sustainable farming",
    "psychedelic*": "psychedelics research",
    "epilepsy": "epilepsy research",
    "big data bowl": "the NFL Big Data Bowl",
}

# Sports, appended LAST on purpose: find_warm_thread returns the FIRST hit, so
# dict order is the ranking, and a page mentioning both the Jets and generic
# sports must open with the Jets — that's the line Eric can actually write.
# Same needle list as the person side (one list, no drift), but labelled as an
# INDUSTRY rather than a biography: a company hit means "this is Eric's sweet
# spot", not "someone here worked in sports".
WARM_THREADS.update({n: "sports / sports tech" for n in SPORTS_NEEDLES})

_TEAM_PATHS = ("/about", "/team", "/about-us", "/our-team", "/company", "/people", "/founders")

# Stripping tags alone leaves the BODY of <script>/<style> behind, so every
# analytics bundle on the page became "prose" to the warm-thread matcher.
_NOISE_BLOCK = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)


def strip_markup(html: str) -> str:
    """HTML → human-readable text: script/style bodies dropped, then tags."""
    return re.sub(r"<[^>]+>", " ", _NOISE_BLOCK.sub(" ", html))

_LINKEDIN = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%-]+")
_TITLE_NEAR = re.compile(
    r"\b(co[\s-]?founder|founder|ceo|chief executive|cto|coo|cpo|president)\b", re.I
)


@dataclass
class Enrichment:
    founders: list[dict] = field(default_factory=list)
    funding: str = ""
    headcount: str = ""
    warm_path: str = ""
    source: str = ""

    def is_empty(self) -> bool:
        return not (self.founders or self.funding or self.headcount or self.warm_path)


def needs_enrichment(e: Entry, min_score: int = MIN_SCORE) -> bool:
    return (e.score or 0) >= min_score and not e.founders


def propagate_known(entries: list[Entry]) -> int:
    """Share founder data between entries for the same company.

    The same company routinely appears several times — once per open role, and
    again via a different board or the imported shortlist. Work at a Startup
    supplies founders, generalist and the shortlist don't, so Structured AI's
    Founding AE has the founders while its Founding Ops Lead (scoring higher)
    has none. Copying across is free, exact, and removes a chunk of the queue
    before any lookup happens.

    Funding and headcount travel the same way. Warm paths deliberately do not:
    those are tied to the specific posting text they were found in.
    """
    from .models import normalize_company

    best: dict[str, Entry] = {}
    for e in entries:
        if not e.founders:
            continue
        key = normalize_company(e.company)
        # Prefer the record with the most founders — usually the WaaS one.
        if key not in best or len(e.founders) > len(best[key].founders):
            best[key] = e

    filled = 0
    for e in entries:
        key = normalize_company(e.company)
        donor = best.get(key)
        if not donor or donor is e:
            continue
        if not e.founders and donor.founders:
            e.founders = [dict(f) for f in donor.founders]
            filled += 1
        if not e.funding and donor.funding:
            e.funding = donor.funding
        if not e.headcount and donor.headcount:
            e.headcount = donor.headcount
    return filled


def company_domain(e: Entry) -> str:
    """The company's own site, avoiding job-board hosts.

    Work at a Startup hands this over directly, so prefer it. Otherwise fall
    back to the posting URL's host, which is only useful when the company hosts
    its own careers page rather than using an ATS.
    """
    if e.company_url:
        return e.company_url.rstrip("/")
    host = urlparse(e.url or "").netloc.lower()
    aggregators = (
        "ashbyhq", "greenhouse", "lever.co", "workatastartup", "generalist.world",
        "linkedin", "getro", "breezy", "workable", "personio", "recruitee",
        "ycombinator", "notion.site",
    )
    if host and not any(a in host for a in aggregators):
        return f"https://{host}"
    return ""


_THREAD_PATTERNS = [(signal_pattern(n), label) for n, label in WARM_THREADS.items()]


def find_warm_thread(text: str) -> str:
    """Look for a shared thread. Returns the first real hit, or nothing."""
    text = strip_markup(text or "")
    for pattern, label in _THREAD_PATTERNS:
        m = pattern.search(text)
        if m:
            # Pull a little context so the claim is checkable, not asserted.
            snippet = re.sub(
                r"\s+", " ", text[max(0, m.start() - 90) : m.start() + 110]
            ).strip()
            return f"{label} — “…{snippet}…”"
    return ""


def complete_companies(entries: list["Entry"], cap: int = 25) -> tuple[int, int]:
    """Post-scout completion: for companies that have a website but no stored
    description or LinkedIn, fetch the site's own meta description and any
    linkedin.com/company link. Bounded by `cap` fetches per run so scout stays
    fast. Fills BLANKS only — hand-written overlay fields are never touched,
    and nothing is invented. Returns (descriptions_filled, linkedins_found).
    """
    import html as _h
    import re as _re

    from . import entities
    from .boards.base import client
    from .models import normalize_company

    views = entities.companies(entries)
    overlay = entities.load_company_overlay()
    filled_d = filled_l = fetched = 0
    for v in views:
        if fetched >= cap:
            break
        rec = overlay.get(normalize_company(v.name), {})
        need_desc = not (rec.get("description") or v.blurb)
        need_li = not (rec.get("linkedin") or v.linkedin)
        need_ind = not (rec.get("industry") or getattr(v, "industry", ""))
        if not (need_desc or need_li or need_ind):
            continue
        site = v.site or rec.get("site", "")
        if not site:
            # No site on record: one discovery attempt, same rule as the
            # one-click Repopulate — domain must echo the name or we pass.
            fetched += 1
            site = _discover_site(v.name)
            if not site:
                continue
        else:
            fetched += 1
        try:
            with client() as c:
                r = c.get(site if site.startswith("http") else "https://" + site)
                r.raise_for_status()
        except Exception:  # noqa: BLE001 — a dead site is just a skip
            continue
        rec = overlay.setdefault(normalize_company(v.name), {"name": v.name})
        if not rec.get("site"):
            rec["site"] = site
        if need_desc:
            m = (_re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]*content=["\']([^"\']{20,400})', r.text, _re.I)
                 or _re.search(r'content=["\']([^"\']{20,400})["\'][^>]*(?:name|property)=["\'](?:og:)?description', r.text, _re.I))
            if m and not rec.get("description"):
                rec["description"] = _h.unescape(m.group(1)).strip()
                rec["description_src"] = "site-meta"
                filled_d += 1
        if need_li and not rec.get("linkedin"):
            m = _re.search(r'https?://(?:www\.)?linkedin\.com/company/[A-Za-z0-9._-]+', r.text)
            if m:
                rec["linkedin"] = m.group(0).rstrip("/")
                filled_l += 1
        if need_ind and not rec.get("industry"):
            ind = _guess_industry((rec.get("description") or "") + " " + r.text[:4000])
            if ind:
                rec["industry"] = ind
    entities.save_company_overlay(overlay)
    return filled_d, filled_l


def scrape_site(url: str, timeout: float = 15.0) -> Enrichment:
    """Deterministic pass over a company's own site. No model call."""
    from .boards.base import client

    out = Enrichment(source="company site")
    if not url:
        return out

    seen_profiles: dict[str, dict] = {}
    text_blob = ""

    try:
        with client() as c:
            for path in ("",) + _TEAM_PATHS:
                try:
                    r = c.get(url.rstrip("/") + path)
                    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                        continue
                except Exception:  # noqa: BLE001
                    continue

                html = r.text
                text_blob += " " + strip_markup(html)

                for m in _LINKEDIN.finditer(html):
                    profile = m.group(0).rstrip("/")
                    if profile in seen_profiles:
                        continue
                    # Only keep profiles that sit near founder/CEO language —
                    # otherwise we scrape every employee and every blog author.
                    window = re.sub(r"<[^>]+>", " ", html[max(0, m.start() - 400) : m.end() + 400])
                    if not _TITLE_NEAR.search(window):
                        continue
                    title_m = _TITLE_NEAR.search(window)
                    name_m = re.search(
                        r"([A-Z][a-z]+(?:\s+[A-Z][a-z'’-]+){1,2})\s*[,\-–—|]?\s*"
                        + re.escape(title_m.group(0)),
                        window,
                    )
                    seen_profiles[profile] = {
                        "name": name_m.group(1).strip() if name_m else "",
                        "title": title_m.group(0),
                        "linkedin": profile,
                        "email": "",
                        "email_confidence": "none",
                    }
    except Exception:  # noqa: BLE001 — enrichment is best-effort, never fatal
        return out

    out.founders = [f for f in seen_profiles.values() if f["name"]][:4]
    out.warm_path = find_warm_thread(text_blob)
    return out


# --- agent backend: emit a queue for in-session enrichment ---


def write_queue(entries: list[Entry], min_score: int = MIN_SCORE) -> Path:
    # Highest score first: an in-session pass is bounded by attention, not by
    # the file, so it must spend that attention on the best roles. Feed order
    # buried an 88 behind a dozen 75s.
    entries = sorted(entries, key=lambda e: (-(e.score or 0), e.company.lower()))
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE.write_text(
        json.dumps(
            {
                "instructions": (
                    f"These postings score {min_score}+ and have no founder on file. For each, "
                    "find: founder names + titles + LinkedIn URLs (CEO first), latest "
                    "funding round + notable investors, approximate headcount, and any "
                    "shared thread with Eric's background (see WARM_THREADS in "
                    "pipeline/enrich.py). Leave a field empty rather than guessing — a "
                    "wrong founder name is worse than a blank one. Write results back "
                    "with `make apply-enrichment FILE=...`."
                ),
                "count": len(entries),
                "companies": [
                    {
                        "company": e.company,
                        "title": e.title,
                        "url": e.url,
                        "site": company_domain(e),
                        "score": e.score,
                        "location": e.location,
                        "funding_known": e.funding,
                    }
                    for e in entries
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return QUEUE


def apply(entries: list[Entry], results: dict[str, dict]) -> int:
    """Merge enrichment results keyed by company name. Never overwrite good data."""
    by_company: dict[str, list[Entry]] = {}
    for e in entries:
        by_company.setdefault(e.company.lower().strip(), []).append(e)

    updated = 0
    for company, data in results.items():
        for e in by_company.get(company.lower().strip(), []):
            if data.get("founders") and not e.founders:
                e.founders = data["founders"]
            if data.get("funding") and not e.funding:
                e.funding = data["funding"]
            if data.get("headcount") and not e.headcount:
                e.headcount = data["headcount"]
            if data.get("warm_path") and not e.warm_path:
                e.warm_path = data["warm_path"]
            updated += 1
    return updated


def resolve_backend() -> str:
    choice = (os.environ.get("SCORER_BACKEND") or "auto").lower()
    if choice == "auto":
        return "api" if os.environ.get("ANTHROPIC_API_KEY") else "agent"
    return choice

_INDUSTRY_KEYWORDS = [
    ("digital health", ("health", "patient", "clinic", "medical", "care ", "therap")),
    ("fitness & wellness", ("fitness", "workout", "wellness", "recovery", "sleep")),
    ("bio & pharma", ("biotech", "pharma", "drug", "molecule", "oncology")),
    ("fintech", ("fintech", "payment", "banking", "invest", "lending", "insurance", "credit", "fraud")),
    ("ed-tech", ("education", "learning", "students", "teacher", "school")),
    ("food & ag", ("food", "agricultur", "farm", "grocer", "restaurant")),
    ("legal & compliance", ("legal", "law firm", "compliance", "contract")),
    ("dev & data tools", ("developer", " api ", "data platform", "infrastructure", "open source")),
    ("robotics & hardware", ("robot", "hardware", "sensor", "device")),
    ("climate & energy", ("climate", "carbon", "solar", "energy", "sustainab")),
    ("commerce & CPG", ("brand", "e-commerce", "ecommerce", "consumer", "retail", "shopp")),
    ("music & creative", ("music", "artist", "creative", "studio")),
    ("sports", ("sports", "athlete", "league", "betting", "fans")),
    # Mirrors entities._INDUSTRIES (2026-08-11): last, so verticals win ties.
    ("enterprise software", ("enterprise", "saas", "b2b", "workflow", "automation",
                             "crm", "hiring", "recruiting", "security", "cyber",
                             "government", "agentic")),
]


def _guess_industry(text: str) -> str:
    """Keyword vote over site copy. First taxonomy bucket with >=2 distinct
    keyword hits wins; anything weaker stays blank rather than guessed."""
    t = " " + text.lower() + " "
    best, hits = "", 0
    for label, kws in _INDUSTRY_KEYWORDS:
        n = sum(1 for k in kws if k in t)
        if n > hits:
            best, hits = label, n
    return best if hits >= 2 else ""


def _formd_stage(name: str) -> tuple[str, float]:
    """Latest SEC Form D for this company → (stage guess, $M raised).

    Uses funding.check with a throwaway state so the funding.json "seen"
    memory — what makes `make funding` mean "new" — is never touched.
    Amount buckets are a guess and labeled as such in the report line.
    """
    try:
        from . import funding as _f
        raises, _ = _f.check([name], state={})
        if not raises:
            return "", 0.0
        r = max(raises, key=lambda x: x.filed)
        if not r.amount:
            return "", 0.0
        m = r.amount / 1e6
        return ("pre_seed" if m < 2 else "seed" if m < 8 else "series_a"), m
    except Exception:  # noqa: BLE001 — enrichment is best-effort
        return "", 0.0

def _discover_site(name: str) -> str:
    """Best-effort website discovery via DuckDuckGo's HTML endpoint.

    Accepts a result only when its domain contains a token of the company
    name — a wrong-company website would poison every later autofill, so a
    miss returns "" rather than the top result.
    """
    import re as _re
    import urllib.parse as _up

    from .boards.base import client
    toks = [t for t in _re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 4]
    if not toks:
        return ""
    try:
        with client() as c:
            r = c.get("https://duckduckgo.com/html/?q="
                      + _up.quote(f'"{name}" startup official site'))
            r.raise_for_status()
        skip = ("linkedin.", "crunchbase.", "wikipedia.", "facebook.", "twitter.",
                "x.com", "instagram.", "youtube.", "glassdoor.", "indeed.",
                "pitchbook.", "duckduckgo.")
        for m in _re.finditer(r'class="result__url"[^>]*>\s*([^<\s]+)', r.text):
            dom = m.group(1).strip().rstrip("/").lower()
            host = dom.split("/")[0]
            if any(b in host for b in skip):
                continue
            if any(t in host.replace("-", "").replace(".", "") for t in toks):
                return "https://" + dom.split("/")[0]
    except Exception:  # noqa: BLE001 — discovery is best-effort
        pass
    return ""
