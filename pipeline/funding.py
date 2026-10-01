"""Funding watch — notice when a company you care about raises.

The research pass found this twice, independently: the best moment to write to a
founder is just after a round closes. The money is there, the hiring plan is
fresh, and the roles usually aren't posted yet — so you're early instead of
applicant #300. `docs/research-landscape.md` has the citations.

`boards/edgar.py` already sweeps *new* Form D filings to discover companies.
This is the other direction: for companies already in the feed — followed,
shortlisted, or simply scoring well — check whether they have filed something
new since the last look. A company that was a maybe last month is a priority
the week after it raises.

Form D is the right instrument because it is a legal requirement within 15 days
of first sale, so it lands *before* the TechCrunch piece. The cost is one search
request per watched company, which is why the watch list is scoped rather than
running over all 500-odd.

State lives in `data/funding.json`: the filings we have already seen per company,
so "new" means genuinely new rather than "first time we looked". Without that
memory every run would report the same round forever and the flag would stop
meaning anything.

Nothing here sends. It raises a flag, bumps the entry's funding line, and lets
`make sends` and the Following tab do what they already do with urgency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .models import Entry, normalize_company, today

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "funding.json"

# A round is "news" for about this long. After that it's context, not a trigger.
NEWS_DAYS = 45

# Watch list scope. Checking everything would be ~500 SEC requests a run for
# almost no return — the tail is companies we've already passed on.
WATCH_MIN_SCORE = 65


@dataclass
class Raise:
    company: str
    filed: str
    amount: int = 0
    entity: str = ""
    filing_url: str = ""
    people: list[dict] = field(default_factory=list)
    is_new: bool = True          # unseen by a previous run
    # Stamped by check() so a headline reports the same age the news window used.
    as_of: str = ""

    @property
    def amount_m(self) -> float:
        return self.amount / 1_000_000

    @property
    def age_days(self) -> int | None:
        try:
            ref = date.fromisoformat(self.as_of) if self.as_of else date.today()
            return (ref - date.fromisoformat(self.filed)).days
        except (ValueError, TypeError):
            return None

    def headline(self) -> str:
        amt = f"${self.amount_m:.1f}M" if self.amount else "an undisclosed amount"
        age = self.age_days
        when = f"{age}d ago" if age is not None else self.filed
        return f"{self.company} filed a Form D for {amt} — {when}"


def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=1, sort_keys=True))


def watch_list(entries: list[Entry], min_score: int = WATCH_MIN_SCORE) -> list[str]:
    """Companies worth spending a request on: followed, live, or high-scoring."""
    from . import entities

    overlay = entities.load_company_overlay()
    tracked = {k for k, v in overlay.items() if v.get("tracked")}

    names: dict[str, str] = {}
    for e in entries:
        key = normalize_company(e.company)
        if not key or e.status == "uninterested":
            continue
        if key in tracked or e.status != "review" or (e.score or 0) >= min_score:
            names.setdefault(key, e.company)
    # Followed companies with no posting at all still deserve the watch.
    for key in tracked:
        names.setdefault(key, overlay[key].get("name", key))
    return sorted(names.values())


def check(companies: list[str], state: dict | None = None,
          news_days: int = NEWS_DAYS, today_: date | None = None) -> tuple[list[Raise], dict]:
    """Look for Form D filings we haven't seen. Returns (raises, new_state).

    `today_` is injectable so the news-window boundary can be tested at a fixed
    date, matching today.py / timing.py / status.py — and so one invocation
    can't classify filings against two different days if it spans midnight.
    """
    import time

    from .boards.base import client
    from .boards.edgar import filing_detail, matching_filings

    today_ = today_ or date.today()
    state = dict(state if state is not None else load_state())
    found: list[Raise] = []

    with client() as c:
        for name in companies:
            key = normalize_company(name)
            rec = state.setdefault(key, {"name": name, "seen": [], "checked": ""})
            try:
                filings = matching_filings(name, c=c)
            except Exception:  # noqa: BLE001 — one company must not kill the run
                continue
            rec["checked"] = today()

            # Remember the most recent filing DATE regardless of age — "when
            # did they last raise?" (Eric, 2026-08-09). News flags stay
            # recent-only below; this is history, kept because the request
            # was already paid for. Detail is fetched once per new latest.
            latest = max(filings, key=lambda f: f[2], default=None)
            if latest and latest[2] != rec.get("latest_filed"):
                try:
                    detail = filing_detail(latest[0], latest[1], c=c)
                except Exception:  # noqa: BLE001
                    detail = {}
                rec.update({"latest_filed": latest[2], "latest_entity": latest[3],
                            "latest_amount": detail.get("amount", 0),
                            "latest_url": detail.get("url", "")})

            for cik, acc, filed, entity in filings:
                if acc in rec["seen"]:
                    continue
                rec["seen"].append(acc)
                # Only *recent* unseen filings are news. An old filing we simply
                # hadn't looked at before is history, not a trigger — this is
                # what stops the first run from flagging everything at once.
                try:
                    age = (today_ - date.fromisoformat(filed)).days
                except (ValueError, TypeError):
                    continue
                if age > news_days:
                    continue
                try:
                    detail = filing_detail(cik, acc, c=c)
                except Exception:  # noqa: BLE001
                    detail = {}
                found.append(
                    Raise(
                        company=name,
                        filed=filed,
                        amount=detail.get("amount", 0),
                        entity=entity,
                        filing_url=detail.get("url", ""),
                        people=detail.get("people", []),
                        as_of=today_.isoformat(),
                    )
                )
            time.sleep(0.4)      # SEC asks for <10 req/s; this is well under

    return found, state


def stamp_overlay(state: dict) -> int:
    """Write each company's latest known Form D date into the company overlay
    (`last_raised`), so cards and views can show raise recency. Update only
    when the date is NEWER than what's stored — a re-check must never regress
    a date, and a manual record is only ever improved on, not clobbered.
    Returns companies updated."""
    from . import entities

    overlay = entities.load_company_overlay()
    changed = 0
    for key, rec in state.items():
        filed = rec.get("latest_filed")
        if not filed:
            continue
        co = overlay.setdefault(key, {"name": rec.get("name", key)})
        prior = (co.get("last_raised") or {}).get("filed", "")
        if filed > prior:
            co["last_raised"] = {"filed": filed,
                                 "amount": rec.get("latest_amount", 0),
                                 "url": rec.get("latest_url", "")}
            changed += 1
    if changed:
        entities.save_company_overlay(overlay)
    return changed


def apply(entries: list[Entry], raises: list[Raise]) -> int:
    """Record the round on every entry for that company. Returns entries touched."""
    from . import history as hist

    by_key = {normalize_company(r.company): r for r in raises}
    touched = 0
    # One load/save for the whole pass. hist.record() is load-modify-save per
    # call, so doing it inside the loop rewrote history.json once per entry.
    h = hist.load()
    for e in entries:
        r = by_key.get(normalize_company(e.company))
        if not r:
            continue
        amt = f"${r.amount_m:.1f}M" if r.amount else "undisclosed"
        line = f"{amt} Form D filed {r.filed}"
        if line not in (e.funding or ""):
            # The funding line holds the CURRENT round only. Wrapping the old
            # value in "(was: …)" nested without bound — three rounds produced
            # "$9M … (was: $5M … (was: $1M …))", which the send card then
            # truncated to a dangling "(was:". History is the record of prior
            # rounds; that's what the event below is for.
            e.funding = line
        # Form D names officers, so a raise can also fill a missing founder.
        if not e.founders and r.people:
            e.founders = r.people
        e.last_touched = today()
        touched += 1
        h.add(e.url, "funding", r.headline(), r.filing_url)
    hist.save(h)
    return touched


def render(raises: list[Raise], checked: int) -> str:
    if not raises:
        return (
            f"Checked {checked} companies. No new Form D filings.\n"
            f"(Only filings from the last {NEWS_DAYS} days count as news.)"
        )
    lines = ["", f"NEW FUNDING — {len(raises)} company(ies) just raised", "=" * 48, ""]
    for r in sorted(raises, key=lambda x: x.filed, reverse=True):
        lines.append(f"  {r.headline()}")
        if r.people:
            who = ", ".join(p["name"] for p in r.people[:3])
            lines.append(f"     officers on the filing: {who}")
        if r.filing_url:
            lines.append(f"     {r.filing_url}")
        lines.append("")
    lines.append("This is the window — money landed and the roles often aren't posted yet.")
    lines.append("`make sends` now ranks these at the top.")
    return "\n".join(lines)
