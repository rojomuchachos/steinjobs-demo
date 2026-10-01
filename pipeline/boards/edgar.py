"""SEC Form D — the funding sweep, and a free founder database.

CLAUDE.md asks for this and it was never built: "Recent pre-seed/seed/Series A
funding announcements in sweet-spot and exception industries. If a freshly-funded
company fits, add it with url = company site, why = 'just raised — no posting yet,
outreach play', and full founder enrichment. These are often the best entries."

Form D is the filing a US company must submit within 15 days of first sale when
it raises under a Reg D exemption — which is how essentially every VC-backed US
startup raises. It is public, free, structured XML, and it carries exactly what
we need:

    entityName            the company
    relatedPersonInfo     executive officers, directors and promoters BY NAME
    totalAmountSold       the exact raise
    dateOfFirstSale       when it closed
    industryGroupType     coarse sector
    yearOfInc             how old the company is

That makes it both the funding sweep *and* a founder source, and it beats press
coverage on timing: the filing lands before most rounds are announced.

What it does not have, so nothing here pretends otherwise: no LinkedIn URLs, no
emails, no website, no headcount, and no European coverage — Reg D is US-only.
Founder names still have to be resolved to a profile before outreach.

The main work is separating startups from everything else, because the same form
is used by hedge funds, real-estate syndicates and SPVs, which vastly outnumber
operating companies.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from ..models import RawPosting
from .base import BoardResult, classify_http_error, client

SEARCH = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/primary_doc.xml"

# SEC asks for a descriptive UA with contact details. Sending a browser string
# here would be both rude and against their stated policy.
SEC_UA = "SteinJobs demo contact@example.com"

# Form D industry codes worth watching, mapped to CLAUDE.md's industries.
INDUSTRIES = {
    "Other Health Care": "health",
    "Biotechnology": "health",
    "Pharmaceuticals": "health",
    "Health Insurance": "health",
    "Hospitals & Physicians": "health",
    "Computers": "tech",
    "Other Technology": "tech",
    "Telecommunications": "tech",
    "Food & Beverage": "food-systems",
    "Agriculture": "agriculture",
    "Restaurants": "food-systems",
    # Widened 2026-08-18 (Eric asked why Just Raised skews tech/health).
    # Form D's industry list is the SEC's own taxonomy and has no code for
    # music, ed-tech or civic tech — those companies file under whichever of
    # these fits, usually "Other Technology". So the skew is partly inherent,
    # but three codes were being dropped that map cleanly onto CLAUDE.md's
    # interests and were simply missing from this whitelist.
    "Other Consumer": "consumer",          # fitness/wellness/CPG brands file here
    "Retailing": "consumer",
    "Education": "ed-tech",                # the ed-tech code DOES exist
}

# Reg D is used by far more funds than startups. These name patterns are how you
# tell an operating company from a vehicle.
_NOT_A_STARTUP = re.compile(
    r"\b(fund|fund\s+[IVX]+|L\.?P\.?|LLP|partners|capital|ventures|holdings|trust"
    r"|realty|real\s+estate|properties|SPV|series\s+[A-Z]{1,2}\b|acquisition"
    r"|opportunit(y|ies)|advisors|management|equity|credit|income|yield"
    r"|investors|investment)\b",
    re.I,
)

# Special-purpose vehicles pooling money into one company are usually named
# "<initials> <TARGET COMPANY> LLC" — e.g. "CSV RHYTHM HEALTH LLC" investing in
# Rythm Health. All-caps plus a short prefix plus LLC is the giveaway.
_LOOKS_LIKE_SPV = re.compile(r"^[A-Z]{2,5}\s+[A-Z][A-Z\s]{4,}\s+LLC\.?$")

# Seed through Series A. Below this is friends-and-family; above it the company
# is past the stage CLAUDE.md wants.
MIN_RAISE = 500_000
MAX_RAISE = 40_000_000

# A round older than this isn't news any more — the hiring wave has passed.
MAX_SALE_AGE_DAYS = 120

US_STATES = {
    "ALABAMA","ALASKA","ARIZONA","ARKANSAS","CALIFORNIA","COLORADO","CONNECTICUT",
    "DELAWARE","DISTRICT OF COLUMBIA","FLORIDA","GEORGIA","HAWAII","IDAHO","ILLINOIS",
    "INDIANA","IOWA","KANSAS","KENTUCKY","LOUISIANA","MAINE","MARYLAND","MASSACHUSETTS",
    "MICHIGAN","MINNESOTA","MISSISSIPPI","MISSOURI","MONTANA","NEBRASKA","NEVADA",
    "NEW HAMPSHIRE","NEW JERSEY","NEW MEXICO","NEW YORK","NORTH CAROLINA","NORTH DAKOTA",
    "OHIO","OKLAHOMA","OREGON","PENNSYLVANIA","RHODE ISLAND","SOUTH CAROLINA",
    "SOUTH DAKOTA","TENNESSEE","TEXAS","UTAH","VERMONT","VIRGINIA","WASHINGTON",
    "WEST VIRGINIA","WISCONSIN","WYOMING","PUERTO RICO",
}

FOUNDER_ROLES = ("executive officer", "promoter")


def _tag(xml: str, name: str) -> list[str]:
    return [t.strip() for t in re.findall(rf"<{name}>(.*?)</{name}>", xml, re.S)]


def _first(xml: str, name: str) -> str:
    vals = _tag(xml, name)
    return vals[0] if vals else ""


def _nested_value(xml: str, name: str) -> str:
    """Some fields wrap their content in a <value> child."""
    raw = _first(xml, name)
    m = re.search(r"<value>(.*?)</value>", raw, re.S)
    return (m.group(1) if m else raw).strip()


def _people(xml: str) -> list[dict]:
    """Officers and promoters are the founders; plain directors usually aren't."""
    out = []
    for blk in re.findall(r"<relatedPersonInfo>(.*?)</relatedPersonInfo>", xml, re.S):
        rels = [r.strip().lower() for r in _tag(blk, "relationship")]
        first, last = _first(blk, "firstName"), _first(blk, "lastName")
        name = " ".join(x for x in (first, _first(blk, "middleName"), last) if x).strip()
        if not name:
            continue
        is_founder = any(any(fr in r for fr in FOUNDER_ROLES) for r in rels)
        out.append(
            {
                "name": name,
                "title": ", ".join(r.title() for r in rels),
                "linkedin": "",   # Form D has none — resolve before outreach
                "email": "",
                "email_confidence": "none",
                "_founder": is_founder,
            }
        )
    # Officers first; they're who you'd actually write to.
    out.sort(key=lambda p: 0 if p["_founder"] else 1)
    return [{k: v for k, v in p.items() if k != "_founder"} for p in out]


def _search(c, term: str, since: date, limit: int) -> list[tuple[str, str, str]]:
    r = c.get(
        SEARCH,
        params={
            "q": f'"{term}"',
            "forms": "D",
            "startdt": since.isoformat(),
            "enddt": date.today().isoformat(),
        },
        headers={"User-Agent": SEC_UA, "Accept": "application/json"},
    )
    r.raise_for_status()
    out = []
    for hit in (r.json().get("hits", {}).get("hits") or [])[:limit]:
        src = hit.get("_source", {})
        names = src.get("display_names") or [""]
        cik = re.search(r"CIK (\d+)", names[0])
        acc = hit.get("_id", "").split(":")[0]
        if cik and acc:
            out.append((cik.group(1), acc, src.get("file_date", "")))
    return out


def _parse_filing(c, cik: str, acc: str, filed: str, source: str) -> dict | None:
    url = ARCHIVE.format(cik=int(cik), acc=acc.replace("-", ""))
    r = c.get(url, headers={"User-Agent": SEC_UA})
    r.raise_for_status()
    xml = r.text

    name = _first(xml, "entityName")
    if not name or _NOT_A_STARTUP.search(name) or _LOOKS_LIKE_SPV.match(name.strip()):
        return None

    industry = _first(xml, "industryGroupType")
    if industry not in INDUSTRIES:
        return None

    try:
        sold = int(_first(xml, "totalAmountSold") or 0)
    except ValueError:
        return None
    if not (MIN_RAISE <= sold <= MAX_RAISE):
        return None

    # Young company only. yearOfInc has three shapes and only one carries a
    # number: <withinFiveYears>+<value>, a bare <overFiveYears>, or
    # <yetToBeFormed>. Checking the value alone silently passed every old
    # company, which is how Jaguar Health — listed on NASDAQ since 2015 — got
    # through the first run.
    year_block = _first(xml, "yearOfInc")
    if "overFiveYears" in year_block and "true" in year_block:
        return None
    year = _nested_value(xml, "yearOfInc")
    if year.isdigit() and date.today().year - int(year) > 8:
        return None

    # The filing date is not the raise date. A company can file late, or amend
    # an old round — FourC Health filed in 2026 for a first sale in 2022. It's
    # the sale that makes them worth contacting.
    first_sale_raw = _nested_value(xml, "dateOfFirstSale")
    if first_sale_raw:
        try:
            sale_date = datetime.fromisoformat(first_sale_raw).date()
            if sale_date < date.today() - timedelta(days=MAX_SALE_AGE_DAYS):
                return None
        except ValueError:
            pass

    # Reg D covers foreign issuers selling into the US, so non-US filers appear.
    # Those are out of scope here — Europe needs its own source, not this one.
    state_raw = _first(xml, "stateOrCountryDescription")
    if state_raw and state_raw.upper() not in US_STATES:
        return None

    people = _people(xml)
    state = _first(xml, "stateOrCountryDescription")
    first_sale = _nested_value(xml, "dateOfFirstSale")

    try:
        posted = datetime.fromisoformat(filed).date() if filed else None
    except ValueError:
        posted = None

    raise_m = sold / 1_000_000
    # Company-first (Eric, 2026-08-12, docs/outreach-plays-retirement.md): a
    # fresh raise is a COMPANY entering review with a timing trigger, not a
    # pseudo-posting. Outreach progress belongs to the founder person record.
    return {
        "name": name,
        # Facts only — no "no roles posted yet" narration (Eric, 2026-08-13:
        # meta-text about the pipeline's own state doesn't belong in why or
        # description). The raise IS the why; what the company does is
        # unknown from a filing, so description stays blank for the research
        # pass to fill with something real.
        "why": (f"just raised — Form D reports ${raise_m:.1f}M sold"
                + (f", first sale {first_sale}" if first_sale else "")),
        "location": state.title() if state else "",
        "stage": "seed" if sold < 5_000_000 else "series_a",
        "industry": INDUSTRIES[industry],
        "description": "",
        "founders": people,
        "last_raised": {
            "filed": posted.isoformat() if posted else "",
            "amount": sold,
            # The filing itself is the citable source; there's no job to link to.
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/",
        },
    }


DAILY_IDX = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"

# One index row: form type D exactly (D/A amendments re-report old rounds),
# company name, CIK, YYYYMMDD, then the accession path.
_IDX_ROW = re.compile(
    r"^D\s{2,}(.+?)\s{2,}(\d{4,10})\s+(\d{8})\s+edgar/data/\d+/(\S+?)\.txt\s*$"
)


def _index_sweep(c, days: int, max_fetch: int) -> list[tuple[str, str, str]]:
    """Every Form D filed in the window, newest first, funds pre-culled by name.

    The term search below only matches words in the ENTITY NAME — Form D has no
    prose for full-text search to bite on, which is why it surfaced 9 companies
    in three weeks. The daily form index lists every filing regardless of what
    the company is called; the startup-vs-fund regexes run on the index row, so
    the XML fetch budget is spent only on plausible operating companies.
    """
    rows: list[tuple[str, str, str]] = []
    d = date.today()
    checked = 0
    while checked < days:
        d -= timedelta(days=1)
        if d.weekday() >= 5:          # EDGAR publishes business days only
            continue
        checked += 1
        url = DAILY_IDX.format(y=d.year, q=(d.month - 1) // 3 + 1, ymd=d.strftime("%Y%m%d"))
        try:
            r = c.get(url, headers={"User-Agent": SEC_UA})
            if r.status_code != 200:
                continue
        except Exception:  # noqa: BLE001 — a missing day is not a failed sweep
            continue
        for line in r.text.splitlines():
            m = _IDX_ROW.match(line)
            if not m:
                continue
            nm, cik, ymd, acc = m.groups()
            nm = nm.strip()
            if _NOT_A_STARTUP.search(nm) or _LOOKS_LIKE_SPV.match(nm):
                continue
            rows.append((cik, acc, f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"))
    rows.sort(key=lambda t: t[2], reverse=True)
    return rows[:max_fetch]


def fetch(name: str, cfg: dict) -> BoardResult:
    terms = cfg.get("terms") or ["health", "fitness", "nutrition", "mental health"]
    days = int(cfg.get("days", 45))
    per_term = int(cfg.get("limit_per_term", 40))
    sweep_days = int(cfg.get("sweep_days", 10))
    max_filings = int(cfg.get("max_filings", 150))
    since = date.today() - timedelta(days=days)

    out: list[dict] = []   # company payloads since the play retirement (2026-08-12)
    seen: set[str] = set()
    errors = 0

    try:
        with client() as c:
            # The index sweep is the primary source; the name-term search
            # stays as a cheap complement for older windows.
            import time as _time

            for cik, acc, filed in _index_sweep(c, sweep_days, max_filings):
                if acc in seen:
                    continue
                seen.add(acc)
                try:
                    _time.sleep(0.12)          # SEC fair-use: stay well under 10 req/s
                    p = _parse_filing(c, cik, acc, filed, name)
                except Exception:  # noqa: BLE001
                    errors += 1
                    continue
                if p:
                    out.append(p)
            for term in terms:
                try:
                    hits = _search(c, term, since, per_term)
                except Exception:  # noqa: BLE001
                    errors += 1
                    continue
                for cik, acc, filed in hits:
                    if acc in seen:
                        continue
                    seen.add(acc)
                    try:
                        p = _parse_filing(c, cik, acc, filed, name)
                    except Exception:  # noqa: BLE001
                        errors += 1
                        continue
                    if p:
                        out.append(p)
    except Exception as exc:  # noqa: BLE001
        msg, blocked = classify_http_error(exc)
        return BoardResult(board=name, companies=out, error=msg, blocked=blocked)

    return BoardResult(
        board=name,
        companies=out,
        error=f"{errors} filings unreadable" if errors else "",
    )


# --- Founder lookup for a company we already care about ---------------------
# The sweep above answers "who just raised?". This answers the opposite and more
# common question: "I have this company and no human to write to."
#
# Form D names executive officers, so any US startup that has raised has its
# founders on public record. That covers the gap left by every board except Work
# at a Startup, which is the only one that ships founders itself.
#
# The whole difficulty is telling the right company from a same-named stranger.
# Full-text search matches the term anywhere in the document, so "Nabi" returns
# NEATOLABS LLC and a stack of Browning West hedge-fund SPVs. Taking the first
# hit would put a fund manager's name in the feed as a startup founder and send
# Eric's email to someone who has never heard of him — the worst failure this
# system can produce. So a hit is only accepted when the filing entity's own
# normalized name equals the company's, and an old filing is downgraded rather
# than trusted: "Cotera, Inc." exists, but its filings stop in 2017 and today's
# Cotera is a different company.

LOOKUP_MAX_HITS = 10
# A filing older than this probably belongs to a different company wearing the
# same name, or to a version of this one whose team has turned over.
LOOKUP_FRESH_YEARS = 6


def matching_filings(company: str, c=None, max_hits: int = LOOKUP_MAX_HITS) -> list[tuple]:
    """Form D filings whose *filing entity* really is `company`.

    Shared by the founder lookup and the funding watch, because both live or die
    on the same rule: full-text search matches the term anywhere in a document,
    so "Nabi" comes back as NEATOLABS LLC and a stack of Browning West hedge-fund
    SPVs. Only an exact normalized entity-name match counts as this company.

    Returns [(cik, accession, file_date, entity_name)], newest first.
    Pass an open client to reuse a connection across many companies.
    """
    from ..models import normalize_company

    want = normalize_company(company)
    if not want:
        return []

    owns_client = c is None
    c = c or client()
    try:
        r = c.get(
            SEARCH,
            params={"q": f'"{company}"', "forms": "D"},
            headers={"User-Agent": SEC_UA, "Accept": "application/json"},
        )
        r.raise_for_status()
        hits = (r.json().get("hits", {}).get("hits") or [])[:max_hits]
    finally:
        if owns_client:
            c.close()

    out = []
    for h in hits:
        src = h.get("_source", {})
        disp = (src.get("display_names") or [""])[0]
        entity = re.sub(r"\s*\(CIK \d+\)\s*$", "", disp).strip()
        if normalize_company(entity) != want:
            continue                      # same-name stranger — skip
        if _NOT_A_STARTUP.search(entity) or _LOOKS_LIKE_SPV.match(entity):
            continue
        cik = re.search(r"CIK (\d+)", disp)
        acc = h.get("_id", "").split(":")[0]
        if cik and acc:
            out.append((cik.group(1), acc, src.get("file_date", ""), entity))
    out.sort(key=lambda x: x[2], reverse=True)
    return out


def filing_detail(cik: str, acc: str, c=None) -> dict:
    """Amount raised, officers, and dates for one filing."""
    owns_client = c is None
    c = c or client()
    try:
        url = ARCHIVE.format(cik=int(cik), acc=acc.replace("-", ""))
        r = c.get(url, headers={"User-Agent": SEC_UA})
        r.raise_for_status()
        xml = r.text
    finally:
        if owns_client:
            c.close()
    try:
        sold = int(_first(xml, "totalAmountSold") or 0)
    except ValueError:
        sold = 0
    return {
        "url": url,
        "amount": sold,
        "people": _people(xml),
        "first_sale": _nested_value(xml, "dateOfFirstSale"),
        "state": _first(xml, "stateOrCountryDescription"),
        "industry": _first(xml, "industryGroupType"),
    }


def lookup_founders(company: str, max_hits: int = LOOKUP_MAX_HITS) -> dict:
    """Officers on record for `company`, or an honest miss.

    Returns {founders, confidence, evidence, note}. `confidence` is:
      confirmed — entity name matches and the filing is recent
      probable  — name matches but the filing is old enough to doubt
      none      — nothing matched; founders is empty

    Never guesses. A miss here is cheap; a wrong founder is not.
    """
    from ..models import normalize_company

    want = normalize_company(company)
    if not want:
        return {"founders": [], "confidence": "none", "evidence": {}, "note": "empty name"}

    try:
        with client() as c:
            candidates = matching_filings(company, c=c, max_hits=max_hits)

            if not candidates:
                return {
                    "founders": [], "confidence": "none", "evidence": {},
                    "note": f"no Form D filed under a company named {company!r}"
                            f" — full-text hits were all other entities",
                }

            # matching_filings sorts newest-first; officer lists go stale.
            cik, acc, filed, entity = candidates[0]
            detail = filing_detail(cik, acc, c=c)
            url, people, sold = detail["url"], detail["people"], detail["amount"]
    except Exception as exc:  # noqa: BLE001
        return {"founders": [], "confidence": "none", "evidence": {},
                "note": f"lookup failed: {type(exc).__name__}"}

    if not people:
        return {"founders": [], "confidence": "none",
                "evidence": {"filing": url, "filed": filed},
                "note": "filing names no related persons"}

    age_years = 0
    try:
        age_years = (date.today() - datetime.fromisoformat(filed).date()).days / 365
    except (ValueError, TypeError):
        pass
    fresh = age_years <= LOOKUP_FRESH_YEARS

    return {
        "founders": people,
        "confidence": "confirmed" if fresh else "probable",
        "evidence": {
            "filing": url,
            "filed": filed,
            "entity": entity,
            "raised": str(sold) if sold else "",
            "state": detail.get("state", ""),
        },
        "note": "" if fresh else (
            f"most recent Form D is {age_years:.0f} years old — verify this is the "
            f"same company before writing to anyone"
        ),
    }
