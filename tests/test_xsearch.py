"""X sweep — capped Grok calls, verbatim tweet ledger, error runs never stamp."""

from __future__ import annotations

import json
from datetime import date

from pipeline import xsearch


def _item(**kw):
    base = {"tweet_url": "https://x.com/founder/status/1", "handle": "founder",
            "author_name": "A Founder", "tweet_date": "2026-08-06",
            "text": "we just raised and I'm hiring our first growth person, DM me",
            "why": "founder personally hiring, seed, growth", "company": "Acme",
            "people": [], "role": "", "application_link": ""}
    base.update(kw)
    return base


def test_extract_items_requires_url_and_verbatim_text():
    """A tweet with no URL can't be linked or deduped; one with no text has
    nothing to show in the Tweets view. Markup-wrapped replies must not
    survive either (the <cite> leak reached real descriptions once)."""
    text = ('Here you go: <b>results</b>\n[' +
            json.dumps(_item()) + ',' +
            json.dumps(_item(tweet_url="")) + ',' +
            json.dumps(_item(text="")) + ']')
    items = xsearch.extract_items(text)
    assert len(items) == 1 and items[0]["company"] == "Acme"
    assert xsearch.extract_items("no json here") == []


def test_no_key_is_a_silent_noop(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert xsearch.sweep() is None


def test_errored_call_never_stamps_and_cap_holds(monkeypatch, tmp_path):
    """A failed research run once stamped 303 companies on an empty credit
    balance. Same rule here: an errored Grok call must leave last_run unset
    so the next scout retries; a successful run stamps and caps the day."""
    monkeypatch.setattr(xsearch, "SEEN", tmp_path / "x_seen.json")
    monkeypatch.setattr(xsearch, "TWEETS", tmp_path / "x_tweets.json")
    monkeypatch.setenv("XAI_API_KEY", "test")
    monkeypatch.setattr(xsearch, "_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("402")))
    assert xsearch.sweep() is None
    assert not (tmp_path / "x_seen.json").exists()

    calls = []
    monkeypatch.setattr(xsearch, "_request", lambda *a, **k: calls.append(1) or
                        ("[]", {"input_tokens": 1, "output_tokens": 1, "x_searches": 0, "cost_usd": 0.0}))
    assert xsearch.sweep() == {"tweets": 0, "postings": 0}
    state = json.loads((tmp_path / "x_seen.json").read_text())
    assert state["last_run"] == date.today().isoformat()
    assert xsearch.sweep() is None and len(calls) == 1  # once a day, period
    assert xsearch.sweep(force=True) is not None and len(calls) == 2


def test_tweets_land_verbatim_and_only_linked_roles_become_postings(monkeypatch, tmp_path):
    """The tweet IS the lead: every fresh one lands in the ledger verbatim
    with status 'new' (a VC vouch with no link would be destroyed by
    conversion). The ONLY auto-import is the unambiguous case - direct
    application link + named role + named company. Seen tweets never
    re-enter the ledger."""
    from pipeline import feed, store

    monkeypatch.setattr(xsearch, "SEEN", tmp_path / "x_seen.json")
    monkeypatch.setattr(xsearch, "TWEETS", tmp_path / "x_tweets.json")
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "DESCRIPTIONS", tmp_path / "descriptions.json")
    monkeypatch.setenv("XAI_API_KEY", "test")

    reply = json.dumps([
        _item(),  # founder hiring, no link -> ledger only
        _item(tweet_url="https://x.com/vc/status/2", handle="vc",
              text="a founder we just backed is hiring a chief of staff",
              people=["Jane Doe"], company=""),  # VC vouch -> ledger only
        _item(tweet_url="https://x.com/f2/status/3", handle="f2", company="Loom2",
              role="Founding GTM", application_link="https://jobs.ashbyhq.com/loom2/x"),
        # Linked but prefilter-excluded: the first live sweep auto-imported
        # two full-stack-engineer roles the prefilter screens on every board.
        _item(tweet_url="https://x.com/f3/status/4", handle="f3", company="AgCo",
              role="Senior Full Stack Engineer",
              application_link="https://jobs.ashbyhq.com/agco/y"),
    ])
    monkeypatch.setattr(xsearch, "_request", lambda *a, **k: (
        reply, {"input_tokens": 1, "output_tokens": 1, "x_searches": 0, "cost_usd": 0.0}))
    saved = []
    monkeypatch.setattr(feed, "load", lambda: [])
    monkeypatch.setattr(feed, "save", lambda entries: saved.extend(entries))

    counts = xsearch.sweep()
    assert counts == {"tweets": 4, "postings": 1}  # eng role: ledger yes, feed no
    ledger = json.loads((tmp_path / "x_tweets.json").read_text())["tweets"]
    assert len(ledger) == 4
    assert ledger[0]["text"].startswith("we just raised")  # verbatim
    assert ledger[1]["people"] == ["Jane Doe"] and ledger[1]["status"] == "new"
    e = saved[0]
    assert e.source == "x" and e.score is None and e.url == "https://jobs.ashbyhq.com/loom2/x"

    # Same tweets again next day: seen-memory drops all three.
    state = json.loads((tmp_path / "x_seen.json").read_text())
    state["last_run"] = "2000-01-01"
    (tmp_path / "x_seen.json").write_text(json.dumps(state))
    counts2 = xsearch.sweep()
    assert counts2 == {"tweets": 0, "postings": 0}
    assert len(json.loads((tmp_path / "x_tweets.json").read_text())["tweets"]) == 4


def test_daily_sweep_window_narrows_to_the_fresh_edge(monkeypatch):
    """2026-08-18: the fixed 21-day window found 0 fresh tweets for 7 straight
    days — Grok's top-25 over three weeks is the same tweets every morning,
    all seen. A run the day after the last one must ask for a ~3-day window;
    a laptop that slept a week must stretch to cover the gap; no state at all
    falls back to the full window."""
    from datetime import date, timedelta

    from pipeline import xsearch

    captured = {}

    def fake_request(api_key, model, lens="", window=xsearch.WINDOW_DAYS):
        captured["window"] = window
        raise RuntimeError("stop before network")

    monkeypatch.setattr(xsearch, "_request", fake_request)
    monkeypatch.setenv("XAI_API_KEY", "test")

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    monkeypatch.setattr(xsearch, "_load", lambda p: {"last_run": yesterday})
    xsearch.sweep(force=True)
    assert captured["window"] == 3

    week_ago = (date.today() - timedelta(days=7)).isoformat()
    monkeypatch.setattr(xsearch, "_load", lambda p: {"last_run": week_ago})
    xsearch.sweep(force=True)
    assert captured["window"] == 9

    monkeypatch.setattr(xsearch, "_load", lambda p: {})
    xsearch.sweep(force=True)
    assert captured["window"] == xsearch.WINDOW_DAYS


def test_tweets_get_a_kind_and_obey_the_boards_location_and_years_gates():
    """Eric (2026-08-18) flagged a Bengaluru tweet wanting heavy experience
    that reached the ledger: the boards' constraints were never applied to X.
    Now they are — but ONLY against what the tweet states, since silence is
    not a rejection (a posting that states neither is kept too). `kind` gives
    the UI something to facet on; anything unrecognized reads as hiring,
    which is what the ledger meant before the field existed."""
    import json

    from pipeline import xsearch

    reply = json.dumps([
        {"tweet_url": "https://x.com/a/1", "text": "hiring a founding GTM lead",
         "kind": "hiring", "location": "NYC"},
        {"tweet_url": "https://x.com/a/2", "text": "hiring, 8+ yrs",
         "kind": "hiring", "location": "Bengaluru, India", "min_years": 8},
        {"tweet_url": "https://x.com/a/3", "text": "we raised a seed",
         "kind": "funding"},                       # alias -> raise, no location
        {"tweet_url": "https://x.com/a/4", "text": "senior only",
         "kind": "hiring", "location": "Remote", "min_years": 9},
        {"tweet_url": "https://x.com/a/5", "text": "no location stated",
         "kind": "nonsense"},                      # unknown kind -> hiring
    ])
    got = {i["tweet_url"][-1]: i for i in xsearch.extract_items(reply)}
    assert set(got) == {"1", "3", "5"}
    assert got["1"]["kind"] == "hiring"
    assert got["3"]["kind"] == "raise"     # alias normalized
    assert got["5"]["kind"] == "hiring"    # unknown falls back, never dropped
