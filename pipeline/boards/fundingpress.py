"""Funding press — the rounds SEC Form D never sees.

EDGAR is the best funding source we have, but it has two structural blind
spots that hit exactly Eric's stage (measured against the feed, 2026-08-18):

  - PRE-SEED SAFEs. A Form D is filed on a securities sale under Reg D. Most
    US pre-seed and much seed money moves as SAFEs that are never filed, or
    filed much later. Those are the smallest, earliest, most founder-reachable
    companies in the pipeline — the ones where a first GTM hire is real.
  - EUROPE. Reg D is US-only, so a Dublin or Prague raise is invisible to
    EDGAR entirely, and Europe is in scope with relocation.

The press covers both, and a funding headline is a structured sentence:
"<Company> raises <amount> <round> to <do thing>". This reads four RSS feeds
and extracts only that sentence's facts.

Like EDGAR after the outreach-play retirement (2026-08-12), a raise produces a
COMPANY entering review, never a pseudo-posting — you don't apply to a round,
you write to the founder.

Honesty rules, because a headline is not a filing:
  - Only what the headline STATES. No founders (the press names a CEO in the
    body, not reliably), no description beyond the company's own one-liner,
    no stage unless the headline names the round.
  - The amount gates the entry: the same MIN/MAX band EDGAR uses, so growth
    rounds and megadeals don't enter a pre-seed pipeline.
  - The article URL is the evidence and is stored as such. A reader can check.
"""

from __future__ import annotations

import html as _h
import re
from datetime import date, datetime, timedelta

from ..models import normalize_company
from .base import BoardResult, classify_http_error, client

# Verified live 2026-08-18. Feeds that 404'd or 403'd are not listed: Axios
# Pro Rata (no public RSS), Finsmes (403 to any non-browser client).
FEEDS = {
    "techcrunch": "https://techcrunch.com/category/startups/feed/",
    "techcrunch-venture": "https://techcrunch.com/category/venture/feed/",
    "eu-startups": "https://www.eu-startups.com/feed/",
    "tech-eu": "https://tech.eu/feed/",
    "sifted": "https://sifted.eu/feed",
}

# Same band as EDGAR: below is friends-and-family, above is past the stage
# CLAUDE.md wants. Currency is not converted — EUR/GBP/USD are close enough
# at this granularity that converting would imply a precision we don't have.
MIN_RAISE = 300_000
MAX_RAISE = 40_000_000

MAX_AGE_DAYS = 30

_MULT = {"k": 1_000, "m": 1_000_000, "mn": 1_000_000, "mil": 1_000_000,
         "million": 1_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
         "billion": 1_000_000_000}

# "<Company> raises $12M Series A", "<Company> lands €4.5 million seed".
# The company is whatever precedes the verb — headlines put it first, and
# anything else (a clause, a publication name) fails the sanity check below.
_RAISE = re.compile(
    r"^(?P<company>.{2,60}?)\s+"
    r"(?:has\s+)?(?:raises|raised|lands|secures|secured|closes|closed|banks|nets|"
    r"picks up|pulls in|snags|scores)\s+"
    r"(?:a\s+|an\s+|its\s+|their\s+)?"
    r"(?P<cur>[$€£])?\s?(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<mult>k|m|mn|mil|million|bn|b|billion)?\b",
    re.I)

_ROUND = re.compile(
    r"\b(pre[-\s]?seed|seed|series\s+([a-d]))\b", re.I)

# Headlines that mention money but aren't a startup raising it.
_NOT_A_RAISE = re.compile(
    r"\b(fund|fund\s+[IVX]+|vc firm|venture firm|raises its|closes its .*fund"
    r"|acquires|acquisition|ipo|valuation of|debt facility|credit line"
    r"|government|grant program|accelerator|now open|applications)\b", re.I)


# Geography and category prefixes the press puts in front of a company name.
# Both apostrophes: the feeds ship the curly one (verified live 2026-08-18,
# where "Germany’s Flip" survived a stripper written for the straight quote).
_APOS = "['’‘´`]"
_GEO_PREFIX = re.compile(
    rf"^(?:"
    rf"[A-Z][\w.&-]*(?:\s+[A-Z][\w.&-]*)?{_APOS}s\s+"        # Germany's, New York's
    rf"|[\w-]+[-\s]based\s+"                                  # Berlin-based
    rf"|[A-Z][\w-]+\s+(?:startup|scaleup|firm|company)\s+"    # Detroit startup X
    rf"|(?:UK|US|EU|Irish|Dutch|German|French|Spanish|Italian|Czech|Nordic|"
    rf"British|Swiss|Danish|Swedish|Norwegian|Finnish|Polish|Belgian|Austrian)\s+"
    rf"(?:startup|scaleup|company|firm|fintech|healthtech)?\s*"
    rf"|(?:Startup|Scaleup|Fintech|Healthtech)\s+"
    rf")", re.I)


def _amount(m: re.Match) -> int | None:
    try:
        n = float(m.group("num"))
    except (TypeError, ValueError):
        return None
    mult = (m.group("mult") or "").lower()
    if not mult:
        # A bare number in a funding headline is almost always millions
        # ("raises 22" is not 22 dollars), but guessing is how bad data gets
        # in. Require the unit.
        return None
    return int(n * _MULT.get(mult, 1))


def _stage(title: str) -> str:
    m = _ROUND.search(title)
    if not m:
        return ""
    tok = m.group(1).lower().replace(" ", "_").replace("-", "_")
    if tok.startswith("pre"):
        return "pre_seed"
    if tok == "seed":
        return "seed"
    letter = (m.group(2) or "").lower()
    return f"series_{letter}" if letter else ""


def _items(xml: str) -> list[dict]:
    out = []
    for blk in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def tag(name):
            m = re.search(rf"<{name}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>",
                          blk, re.S)
            return _h.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else ""
        out.append({"title": tag("title"), "link": tag("link"),
                    "date": tag("pubDate"), "summary": tag("description")[:400]})
    return out


def _parse_date(s: str) -> date | None:
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def parse_headline(title: str, link: str = "", summary: str = "",
                   when: date | None = None) -> dict | None:
    """A funding headline -> a company payload, or None when it isn't one."""
    title = (title or "").strip()
    if not title or _NOT_A_RAISE.search(title):
        return None
    m = _RAISE.match(title)
    if not m:
        return None
    amount = _amount(m)
    if amount is None or not (MIN_RAISE <= amount <= MAX_RAISE):
        return None
    company = m.group("company").strip(" ,–—-")
    # Guard against a leading clause being read as the company name
    # ("A year after their last raise, Germany's Flip raises €22 mil").
    if "," in company:
        company = company.rsplit(",", 1)[-1].strip()
    if ":" in company:
        company = company.rsplit(":", 1)[-1].strip()
    # The European press prefixes geography — "Germany's Flip",
    # "Berlin-based Acme", "UK startup Foo". The company is what follows.
    company = _GEO_PREFIX.sub("", company).strip()
    if not company or len(company) > 45 or len(company.split()) > 5:
        return None
    cur = m.group("cur") or "$"
    unit = "M" if amount >= 1_000_000 else "K"
    shown = amount / (1_000_000 if unit == "M" else 1_000)
    amt_str = f"{cur}{shown:g}{unit}"
    stage = _stage(title)
    return {
        "name": company,
        "why": (f"just raised — {amt_str}"
                + (f" {stage.replace('_', ' ')}" if stage else "")
                + (f", reported {when.isoformat()}" if when else "")),
        "stage": stage,
        "description": "",     # a headline says nothing about what they do
        "founders": [],        # the press names a CEO in the body, unreliably
        "last_raised": {"filed": when.isoformat() if when else "",
                        "amount": amount, "url": link},
        "source_title": title,
    }


def fetch(name: str, cfg: dict) -> BoardResult:
    feeds = cfg.get("feeds") or FEEDS
    cutoff = date.today() - timedelta(days=int(cfg.get("max_age_days", MAX_AGE_DAYS)))
    out, seen, errors = [], {}, []
    try:
        with client() as c:
            for feed_name, url in feeds.items():
                try:
                    r = c.get(url, headers={
                        "Accept": "application/rss+xml,application/xml,text/xml"})
                    if r.status_code != 200:
                        errors.append(f"{feed_name}: HTTP {r.status_code}")
                        continue
                    for it in _items(r.text):
                        when = _parse_date(it["date"])
                        if when and when < cutoff:
                            continue
                        got = parse_headline(it["title"], it["link"],
                                             it["summary"], when)
                        if not got:
                            continue
                        # Canonical dedupe, not raw lowercase: the same raise
                        # reaches two feeds under "Aisel" and "Aisel Health"
                        # (seen live 2026-08-18). Keep the longer name — it
                        # carries the suffix the other dropped.
                        key = normalize_company(got["name"])
                        if key in seen:
                            prior = seen[key]
                            if len(got["name"]) > len(prior["name"]):
                                prior.update(name=got["name"])
                            continue
                        seen[key] = got
                        out.append(got)
                except Exception as exc:  # noqa: BLE001 — one feed must not kill the sweep
                    errors.append(f"{feed_name}: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        return BoardResult(board=name, postings=[], **classify_http_error(exc))
    return BoardResult(board=name, postings=[], companies=out,
                       error="; ".join(errors) if errors else "")
