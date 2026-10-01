"""Tests for the judgment layers: CV selection, pipeline state, enrichment.

These cover behaviour the spec promises rather than bugs already hit — the
always-keep rule, the two-nudge follow-up budget, company-level memory, and the
rule that enrichment never invents a founder.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from pipeline import cv, enrich, importer, status
from pipeline.models import Entry, RawPosting, today


def entry(**kw) -> Entry:
    base = dict(
        title="Founding Growth Lead", company="Acme", url="https://acme.com/j/1",
        source="test", location="New York, NY", score=80, why="fits",
    )
    base.update(kw)
    return Entry(**base)


# --- CV parsing and selection ---------------------------------------------


def test_cv_parses_entries_with_priority_and_tags():
    entries = cv.parse()
    assert entries, "master_cv.md should parse"
    jets = next(e for e in entries if "Jets" in e.org)
    assert jets.priority == "core"
    assert "sports" in jets.tags
    assert jets.bullets and "24th to 1st" in " ".join(jets.bullets)


def test_cv_authoring_comments_are_stripped_from_bullets():
    """master_cv.md carries <!-- TODO --> notes that must never reach a resume."""
    for e in cv.parse():
        for b in e.bullets:
            assert "<!--" not in b and "TODO" not in b


def test_tag_matching_is_case_insensitive():
    """The CV writes `tags: bd, ml`; role hints read as "BD"/"ML"."""
    e = cv.CVEntry(heading="X · Y", section="Work experience", meta="",
                   priority="core", tags=["bd", "growth"], bullets=["b"])
    assert e.score_against(["BD"]) > 0


def test_proof_point_map_is_read_from_the_cv():
    m = cv.proof_point_map()
    assert m.get("sports", "").startswith("Jets")
    assert "Neuroscape" in m.get("psychedelics", "")


@pytest.mark.parametrize("title,desc,expect_org", [
    ("Marketing Lead", "AI communication layer for healthcare", "Emory"),
    ("Founding BD", "We help law enforcement solve crime", "Carter Center"),
    ("Creator Growth Lead", "grow the creator community", "Trash Compactor"),
])
def test_role_reaches_the_right_cv_entry(title, desc, expect_org):
    tags = cv.tags_for_role(title, desc)
    orgs = " ".join(e.org for e in cv.best_entries(tags, limit=3))
    assert expect_org in orgs


# --- Tailoring -------------------------------------------------------------


def test_mark_refuses_an_ambiguous_match():
    entries = [entry(url="https://a.com/1", title="Growth Lead"),
               entry(url="https://a.com/2", title="Growth Marketer")]
    changed, msg = status.mark(entries, "a.com", "shortlisted")
    assert changed == [] and "matched 2" in msg


def test_mark_allows_a_bulk_pass():
    # "passed" is the pre-rename spelling — mark() must still accept it and
    # normalize to "uninterested", or old muscle memory silently errors.
    entries = [entry(url="https://a.com/1"), entry(url="https://a.com/2")]
    changed, _ = status.mark(entries, "a.com", "passed", "not a fit")
    assert len(changed) == 2
    assert all(e.status == "uninterested" for e in entries)


def test_mark_rejects_an_unknown_status():
    changed, msg = status.mark([entry()], "acme", "maybe")
    assert changed == [] and "unknown status" in msg


def test_company_memory_flags_prior_contact():
    entries = [entry(company="Acme", status="applied"),
               entry(company="Acme", url="https://acme.com/j/2", status="review")]
    assert "acme" in status.engaged_companies(entries)


def test_calibration_records_the_verdict_and_reason():
    e = entry(status="uninterested", score=42)
    rec = status.calibration_record(e, "too far along, 60 people")
    assert rec["verdict"] == "Screened out"
    assert rec["why"] == "too far along, 60 people"
    assert rec["score_given"] == 42


# --- Follow-ups: two nudges, then stop ------------------------------------


def _aged(days: int, **kw) -> Entry:
    return entry(last_touched=(date.today() - timedelta(days=days)).isoformat(), **kw)


def test_nudge_due_at_day_four():
    due = status.followups([_aged(5, status="applied")])
    assert len(due) == 1 and due[0][2] == 1


def test_nothing_due_before_day_four():
    assert status.followups([_aged(2, status="applied")]) == []


def test_second_nudge_after_the_first():
    e = _aged(9, status="applied", status_note="nudge 1 sent")
    assert len(status.followups([e])) == 1


def test_budget_stops_at_two():
    e = _aged(30, status="applied", status_note="nudge 1 sent; nudge 2 sent")
    assert status.followups([e]) == []


def test_the_budget_is_two_and_the_schedule_defines_it():
    """Pins the budget itself, not just one case past it.

    The earlier version of this test passed even when the cap was removed,
    because the day-threshold lookup already failed beyond the schedule. Assert
    on the schedule so widening it is a deliberate, visible change.
    """
    assert len(status.NUDGE_SCHEDULE) == 2
    assert status.NUDGE_SCHEDULE == (4, 7)
    # A third nudge is never scheduled, however long it has been.
    e = _aged(365, status="applied", status_note="nudge nudge")
    assert status.followups([e]) == []


@pytest.mark.parametrize("state", ["responded", "passed", "new", "shortlisted"])
def test_only_live_outreach_is_chased(state):
    assert status.followups([_aged(20, status=state)]) == []


# --- Enrichment ------------------------------------------------------------


def test_founders_propagate_between_roles_at_one_company():
    founders = [{"name": "Jane Doe", "title": "CEO", "linkedin": "x",
                 "email": "", "email_confidence": "none"}]
    a = entry(url="https://acme.com/1", founders=founders, funding="$5M seed")
    b = entry(url="https://acme.com/2")
    filled = enrich.propagate_known([a, b])
    assert filled == 1
    assert b.founders[0]["name"] == "Jane Doe"
    assert b.funding == "$5M seed"


def test_propagation_never_crosses_companies():
    a = entry(company="Acme", founders=[{"name": "Jane", "title": "CEO",
                                         "linkedin": "", "email": "", "email_confidence": "none"}])
    b = entry(company="Globex", url="https://globex.com/1")
    enrich.propagate_known([a, b])
    assert b.founders == []


def test_propagation_prefers_the_richest_record():
    one = entry(url="https://acme.com/1", founders=[{"name": "A", "title": "", "linkedin": "",
                                                     "email": "", "email_confidence": "none"}])
    two = entry(url="https://acme.com/2", founders=[
        {"name": "A", "title": "", "linkedin": "", "email": "", "email_confidence": "none"},
        {"name": "B", "title": "", "linkedin": "", "email": "", "email_confidence": "none"}])
    empty = entry(url="https://acme.com/3")
    enrich.propagate_known([one, two, empty])
    assert len(empty.founders) == 2


def test_warm_thread_requires_a_real_hit():
    assert enrich.find_warm_thread("Founded by two Emory grads") != ""
    assert enrich.find_warm_thread("A generic company building software") == ""


def test_warm_thread_never_matches_mid_word_or_page_scripts():
    """Symptom (2026-08-07): `make enrich` wrote "Emory — …" onto Salient
    because their copy said "borrower-level MEMORY", and "the NFL — …" onto
    five companies because the substring sat inside a minified FullStory
    bundle. Ten fabricated warm paths reached the feed — the exact invented
    signal the data-honesty rule forbids, and one Eric could have put in an
    email. Needles anchor to a word START; script/style bodies aren't prose.
    Deliberate stems (thru-hik → thru-hiking) must survive the anchoring."""
    # mid-word substrings are not signals
    assert enrich.find_warm_thread("Borrower-level memory, end to end") == ""
    assert enrich.find_warm_thread("we unflatten the payload") == ""
    # neither is anything inside a script/style body
    js = "<script>function(h){return!(h in m)||unflatten(m)}</script><p>CRM tools</p>"
    assert enrich.find_warm_thread(js) == ""
    assert enrich.find_warm_thread("<style>.nfl{color:red}</style><p>Payments</p>") == ""
    assert "script" not in enrich.strip_markup(js)
    # real mentions still land, stems included
    assert enrich.find_warm_thread("Our CTO played in the NFL") != ""
    assert enrich.find_warm_thread("She studied at Emory University") != ""
    assert enrich.find_warm_thread("thru-hiking the trail") != ""
    assert enrich.find_warm_thread("psychedelics research") != ""


def test_enrichment_queue_leads_with_the_best_roles(monkeypatch, tmp_path):
    """The queue is worked by hand top-down, so feed order buried an 88 behind
    a dozen 75s. Highest score first, and the instructions quote the threshold
    actually used rather than a hardcoded 75."""
    import json

    from pipeline import enrich as en

    monkeypatch.setattr(en, "QUEUE", tmp_path / "enrichment_queue.json")
    q = json.loads(en.write_queue(
        [entry(company="Low", score=76), entry(company="High", score=91),
         entry(company="Mid", score=83)], min_score=80).read_text())
    assert [c["score"] for c in q["companies"]] == [91, 83, 76]
    assert "score 80+" in q["instructions"]


def test_aggregator_urls_yield_no_company_site():
    assert enrich.company_domain(entry(url="https://jobs.ashbyhq.com/x/y")) == ""
    assert enrich.company_domain(entry(company_url="https://acme.com")) == "https://acme.com"


def test_only_high_scorers_need_enrichment():
    assert enrich.needs_enrichment(entry(score=80)) is True
    assert enrich.needs_enrichment(entry(score=60)) is False
    assert enrich.needs_enrichment(entry(score=80, founders=[{"name": "x"}])) is False


# --- Shortlist import ------------------------------------------------------


def test_bucket_maps_to_status_and_band():
    row = {"Bucket": "Top match", "Company": "Acme", "Role": "Founding GTM",
           "URL": "https://a.com/1", "Fit verdict": "great", "Funding/Stage/Size": "Seed $7M"}
    e = importer.to_entry(row, "shortlist")
    assert e.status == "saved" and e.score == 85
    assert e.why == "great" and e.stage == "seed"


def test_screened_out_rows_still_enter_the_feed_as_passed():
    row = {"Bucket": "Screened out", "Company": "X", "Role": "Data Scientist",
           "URL": "https://a.com/2", "Fit verdict": "IC analytics"}
    e = importer.to_entry(row, "shortlist")
    assert e.status == "uninterested"


def test_board_rows_are_not_jobs():
    row = {"Bucket": "Board", "Company": "—", "Role": "search url", "URL": "https://a.com/3"}
    assert importer.to_entry(row, "shortlist") is None


def test_rejections_become_calibration_examples():
    row = {"Bucket": "Screened out", "Company": "X", "Role": "Data Scientist",
           "URL": "u", "Fit verdict": "pure IC analytics"}
    rec = importer.to_calibration(row)
    assert rec["verdict"] == "Screened out" and "IC analytics" in rec["why"]


# --- Activity history ------------------------------------------------------


def test_history_is_append_only():
    h = __import__("pipeline.history", fromlist=["x"]).History()
    h.add("https://acme.com/1", "status", "moved to shortlisted", "great fit")
    h.add("https://acme.com/1", "artifact", "outreach draft written")
    h.add("https://acme.com/1", "status", "moved to applied", "sent it")
    evs = h.for_entry("https://acme.com/1")
    assert [e.text for e in evs] == [
        "moved to shortlisted", "outreach draft written", "moved to applied",
    ]
    # earlier notes survive later transitions — the whole reason this exists
    assert evs[0].detail == "great fit"


def test_history_normalises_the_url_key():
    h = __import__("pipeline.history", fromlist=["x"]).History()
    h.add("https://Acme.com/1?utm_source=x", "note", "a")
    assert h.count("https://acme.com/1") == 1


def test_history_last_and_count_filter_by_kind():
    mod = __import__("pipeline.history", fromlist=["x"])
    h = mod.History()
    h.add("u", "status", "moved to drafted")
    h.add("u", "note", "called them")
    h.add("u", "status", "moved to applied")
    assert h.count("u", "status") == 2
    assert h.last("u", "status").text == "moved to applied"


def test_unknown_kind_falls_back_to_note():
    h = __import__("pipeline.history", fromlist=["x"]).History()
    ev = h.add("u", "bogus", "x")
    assert ev.kind == "note"


# --- Cover letters ----------------------------------------------------------


def test_company_rollup_takes_best_of_everything(monkeypatch, tmp_path):
    from pipeline import entities as ent

    monkeypatch.setattr(ent, "COMPANIES", tmp_path / "c.json")
    monkeypatch.setattr(ent, "PEOPLE", tmp_path / "p.json")
    founders = [{"name": "Jane", "title": "CEO", "linkedin": "x",
                 "email": "", "email_confidence": "none"}]
    rows = [
        entry(url="https://a.com/1", score=60, status="review"),
        entry(url="https://a.com/2", score=85, status="saved", founders=founders),
    ]
    cs = ent.companies(rows)
    assert len(cs) == 1
    c = cs[0]
    assert c.best_score == 85 and c.status == "saved"
    assert len(c.postings) == 2 and c.founders[0]["name"] == "Jane"


def test_tracked_company_without_postings_still_appears(monkeypatch, tmp_path):
    from pipeline import entities as ent

    monkeypatch.setattr(ent, "COMPANIES", tmp_path / "c.json")
    monkeypatch.setattr(ent, "PEOPLE", tmp_path / "p.json")
    ent.track_company("Function Health", why="longevity diagnostics")
    cs = ent.companies([])
    assert any(c.name == "Function Health" and c.tracked for c in cs)


def test_person_signals_come_only_from_their_own_text(monkeypatch, tmp_path):
    """Company warm-path context must never become a claim about a person.

    A false "same school" flag could end up in an email to that person.
    """
    from pipeline import entities as ent

    monkeypatch.setattr(ent, "COMPANIES", tmp_path / "c.json")
    monkeypatch.setattr(ent, "PEOPLE", tmp_path / "p.json")
    founders = [{"name": "Jane", "title": "CEO", "linkedin": "",
                 "email": "", "email_confidence": "none"}]
    e = entry(founders=founders,
              warm_path="same world as the Emory School of Medicine epilepsy work")
    ppl = ent.people([e])
    jane = next(p for p in ppl if p.name == "Jane")
    assert jane.signals == []                       # nothing from Eric-side context
    cs = ent.companies([e])
    assert "Emory" in " ".join(cs[0].signals)       # the company keeps the signal


def test_add_person_updates_rather_than_duplicates(monkeypatch, tmp_path):
    from pipeline import entities as ent

    monkeypatch.setattr(ent, "COMPANIES", tmp_path / "c.json")
    monkeypatch.setattr(ent, "PEOPLE", tmp_path / "p.json")
    ent.add_person("Sam Ro", company="Acme", signal="NMH")
    ent.add_person("Sam Ro", company="Acme", linkedin="https://linkedin.com/in/samro",
                   signal="Atlanta")
    rows = ent.load_people_overlay()
    assert len(rows) == 1
    assert set(rows[0]["signals"]) == {"NMH", "Atlanta"}
    assert rows[0]["linkedin"].endswith("/samro")


def test_manual_signal_plus_auto_scan_of_notes(monkeypatch, tmp_path):
    from pipeline import entities as ent

    monkeypatch.setattr(ent, "COMPANIES", tmp_path / "c.json")
    monkeypatch.setattr(ent, "PEOPLE", tmp_path / "p.json")
    ent.add_person("Kai", company="X", note="met at a powerlifting meet in Atlanta")
    ppl = ent.people([])
    kai = next(p for p in ppl if p.name == "Kai")
    assert "powerlifting" in kai.signals and "Atlanta" in kai.signals


# --- Insights ----------------------------------------------------------------


def test_agreement_flags_disagreements_both_ways():
    from pipeline import insights

    rows = [
        entry(url="u1", score=80, status="uninterested", status_note="meh"),  # false high
        entry(url="u2", score=40, status="saved"),                       # false low
        entry(url="u3", score=85, status="saved"),                       # agree
        entry(url="u4", score=20, status="uninterested"),                # agree
    ]
    a = insights.agreement(rows)
    assert [e.url for e in a["false_high"]] == ["u1"]
    assert [e.url for e in a["false_low"]] == ["u2"]


def test_pass_reasons_surface_recurring_bigrams():
    from pipeline import insights

    cal = [{"verdict": "Screened out", "why": "big-company narrow scope"}] * 3 + [
        {"verdict": "Top match", "why": "big-company narrow scope"}  # ignored: not a pass
    ]
    top = insights.pass_reasons(cal)
    assert any("narrow scope" in g for g, _ in top)


def test_source_roi_ranks_by_hit_rate():
    from pipeline import insights

    rows = [entry(url=f"a{i}", source="good", score=90) for i in range(3)]
    rows += [entry(url=f"b{i}", source="bad", score=10) for i in range(3)]
    roi = insights.source_roi(rows)
    assert roi[0]["source"] == "good" and roi[0]["hit_rate"] == 100.0


def test_blurb_strips_boilerplate_by_company_name_not_greedily():
    """The greedy pattern ate real words: 'About Structured AI We're building
    the AI workforce...' came out as 'force for construction engineering'."""
    from pipeline.entities import _blurb_from

    src = "About Structured AI We're building the AI workforce for construction engineering"
    assert _blurb_from(src, "Structured AI").startswith("We're building the AI workforce")
    # stacked boilerplate: "About X At X, ..."
    src2 = "About Clarion At Clarion, we're rebuilding how healthcare communicates"
    assert _blurb_from(src2, "Clarion").startswith("We're rebuilding")
    # no company match → text untouched
    assert _blurb_from("Plain sentence.", "Other Co") == "Plain sentence."


# --- Outreach timing: who, when, how ----------------------------------------
# Rules traced to docs/research-landscape.md. They're worth pinning because they
# encode research findings that a future edit could silently reverse.


def test_founder_is_the_target_at_early_stage():
    """Kamerow's rule: under ~20 people / through Series A, mail the CEO."""
    from pipeline.models import Entry
    from pipeline.timing import pick_contact

    e = Entry(title="Founding GTM", company="X", url="u", source="s", location="", score=80,
              why="", stage="seed",
              founders=[{"name": "A Dev", "title": "CTO"},
                        {"name": "B Boss", "title": "CEO"}])
    contact, reason = pick_contact(e)
    assert contact["name"] == "B Boss"          # CEO outranks CTO
    assert "founder is the hiring manager" in reason


def test_past_series_a_flags_that_the_founder_may_be_wrong_door():
    from pipeline.models import Entry
    from pipeline.timing import pick_contact

    e = Entry(title="Growth", company="X", url="u", source="s", location="", score=80,
              why="", stage="series_b", founders=[{"name": "B Boss", "title": "CEO"}])
    _, reason = pick_contact(e)
    assert "VP" in reason


def test_form_d_filing_is_the_strongest_trigger():
    """Post-funding is the best moment to write; EDGAR dates it precisely."""
    from datetime import date, timedelta

    from pipeline.models import Entry
    from pipeline.timing import assess

    today = date(2026, 7, 20)
    e = Entry(title="", company="X", url="u", source="edgar", location="", score=80, why="",
              posted_at=(today - timedelta(days=9)).isoformat(),
              founders=[{"name": "B Boss", "title": "CEO"}])
    a = assess(e, today_=today)
    assert a.urgency == 40
    assert "Form D" in a.trigger


def test_warm_path_outranks_a_cold_send_of_equal_fit():
    from datetime import date

    from pipeline.models import Entry
    from pipeline.timing import assess

    base = dict(title="t", company="X", url="u", source="s", location="", score=80, why="",
                founders=[{"name": "B Boss", "title": "CEO", "email": "b@x.com"}])
    cold = assess(Entry(**base), today_=date(2026, 7, 20))
    warm = assess(Entry(**base, warm_path="Emory"), today_=date(2026, 7, 20))
    assert warm.priority > cold.priority


def test_address_candidates_are_patterns_not_assertions():
    """firstname@ dominates at early stage — but all three stay guesses."""
    from pipeline.timing import address_candidates

    got = address_candidates("Jamie Lin", "https://hellohera.com")
    assert got[0] == "jamie@hellohera.com"
    assert "jamie.lin@hellohera.com" in got
    # No name or no domain means no guessing at all.
    assert address_candidates("", "https://x.com") == []
    assert address_candidates("Jamie Lin", "") == []


def test_send_window_avoids_monday_and_friday():
    from datetime import date

    from pipeline.timing import next_send_slot

    assert "today" in next_send_slot(date(2026, 7, 22))      # Wednesday
    assert "2026-07-21" in next_send_slot(date(2026, 7, 20))  # Monday -> Tuesday
    assert "2026-07-28" in next_send_slot(date(2026, 7, 24))  # Friday -> next Tuesday


def test_applied_entries_drop_out_of_the_send_queue(tmp_path, monkeypatch):
    """Past `applied`, the follow-up queue owns the relationship. Overlay is
    isolated: since the play retirement (2026-08-12) queue() also surfaces
    saved COMPANIES with live raise triggers, so the real overlay would leak
    ghost rows into this assertion."""
    from pipeline import entities
    from pipeline.models import Entry
    from pipeline.timing import queue

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    es = [
        Entry(title="a", company="A", url="u1", source="s", location="", score=90, why="",
              status="applied", founders=[{"name": "X Y", "title": "CEO"}]),
        Entry(title="b", company="B", url="u2", source="s", location="", score=90, why="",
              status="saved", founders=[{"name": "X Y", "title": "CEO"}]),
    ]
    assert [a.company for a in queue(es)] == ["B"]


# --- Funding watch ----------------------------------------------------------
# Post-funding is the strongest outreach trigger in the research, so the watch
# has to be trustworthy in two directions: it must not miss a real raise, and it
# must not cry wolf on a filing it simply hadn't looked at before.


def test_watch_list_scopes_to_companies_worth_a_request():
    from pipeline.funding import watch_list
    from pipeline.models import Entry

    es = [
        Entry(title="a", company="HighScore", url="u1", source="s", location="", score=80, why=""),
        Entry(title="b", company="LowScore", url="u2", source="s", location="", score=20, why=""),
        Entry(title="c", company="Engaged", url="u3", source="s", location="", score=10, why="",
              status="saved"),
        Entry(title="d", company="Rejected", url="u4", source="s", location="", score=90, why="",
              status="uninterested"),
    ]
    names = watch_list(es, min_score=65)
    assert "HighScore" in names          # high score alone qualifies
    assert "Engaged" in names            # so does an active status at any score
    assert "LowScore" not in names       # noise
    assert "Rejected" not in names       # already decided against


def test_old_unseen_filings_are_not_reported_as_news(monkeypatch):
    """The first run must not flag a 2019 round as if it just happened."""
    from datetime import date, timedelta

    from pipeline import funding

    old = (date.today() - timedelta(days=400)).isoformat()
    recent = (date.today() - timedelta(days=5)).isoformat()

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(funding, "load_state", lambda: {})
    monkeypatch.setattr("pipeline.boards.base.client", lambda: FakeClient())
    monkeypatch.setattr("pipeline.boards.edgar.matching_filings",
                        lambda name, c=None, **kw: [("1", "acc-old", old, "X Inc"),
                                                    ("1", "acc-new", recent, "X Inc")])
    monkeypatch.setattr("pipeline.boards.edgar.filing_detail",
                        lambda cik, acc, c=None: {"amount": 5_000_000, "people": [], "url": "u"})

    raises, state = funding.check(["X"])
    assert [r.filed for r in raises] == [recent]     # only the recent one is news
    # …but both are remembered, so neither is ever reported again.
    assert len(state["x"]["seen"]) == 2


def test_a_seen_filing_is_never_reported_twice(monkeypatch):
    from datetime import date, timedelta

    from pipeline import funding

    recent = (date.today() - timedelta(days=3)).isoformat()

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr("pipeline.boards.base.client", lambda: FakeClient())
    monkeypatch.setattr("pipeline.boards.edgar.matching_filings",
                        lambda name, c=None, **kw: [("1", "acc-1", recent, "X Inc")])
    monkeypatch.setattr("pipeline.boards.edgar.filing_detail",
                        lambda cik, acc, c=None: {"amount": 1, "people": [], "url": "u"})

    prior = {"x": {"name": "X", "seen": ["acc-1"], "checked": "2026-01-01"}}
    raises, _ = funding.check(["X"], state=prior)
    assert raises == []


def test_a_raise_can_fill_a_missing_founder(tmp_path, monkeypatch):
    """Form D names officers, so detecting a round also closes the founder gap.

    HISTORY is patched because apply() records the raise — unpatched, every
    suite run appended "X filed a Form D" junk under key "u" in the REAL
    data/history.json, which then surfaced in the app's Activity log."""
    from pipeline import history as hist
    from pipeline.funding import Raise, apply
    from pipeline.models import Entry

    monkeypatch.setattr(hist, "HISTORY", tmp_path / "history.json")
    e = Entry(title="t", company="X", url="u", source="s", location="", score=70, why="")
    r = Raise(company="X", filed="2026-07-01", amount=4_000_000,
              people=[{"name": "A Founder", "title": "Executive Officer"}])
    apply([e], [r])
    assert e.founders[0]["name"] == "A Founder"
    assert "$4.0M" in e.funding


# --- One apply command, four result shapes ----------------------------------
# Four near-identical apply-* commands became one that sniffs the file. The
# sniffer has to be right, because dispatching a descriptions file into the
# score writer would quietly overwrite real scores.


def test_sniff_distinguishes_the_four_result_shapes():
    from pipeline.cli import _sniff_result

    assert _sniff_result({"jobs/1": {"score": 80, "why": "fits"}}) == "scores"
    assert _sniff_result({"acme": "Acme builds diagnostics."}) == "descriptions"
    assert _sniff_result([{"company": "Acme", "founders": [{"name": "A"}]}]) == "enrichment"
    assert _sniff_result({"people": [{"name": "A", "linkedin": "u"}]}) == "resolution"


def test_sniff_refuses_to_guess_when_ambiguous():
    """An empty or unrecognisable file must ask, not pick one at random."""
    from pipeline.cli import _sniff_result

    assert _sniff_result({}) == ""
    assert _sniff_result([]) == ""
    assert _sniff_result("nonsense") == ""


# --- Alumni flags: derived from verified people, never stored on companies --


def test_company_alumni_derive_from_recorded_people(tmp_path, monkeypatch):
    """The Emory chip on a company exists ONLY while a verified person record
    carries the signal — delete the person and the flag disappears with them."""
    from pipeline import entities
    from pipeline.models import Entry

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")

    es = [Entry(title="Growth", company="Acme", url="u1", source="s",
                location="", score=70, why="")]
    assert entities.companies(es)[0].alumni == []

    entities.add_person(name="Jane Doe", company="Acme", role="Ops",
                        linkedin="https://linkedin.com/in/jd",
                        relationship="lead", signal="Emory",
                        note="Emory MPH verified on her own education page")
    cs = entities.companies(es)
    assert cs[0].alumni == ["Emory: Jane Doe"]

    entities.save_people_overlay([])          # person removed
    assert entities.companies(es)[0].alumni == []


def test_company_linkedin_is_stored_once(tmp_path, monkeypatch):
    from pipeline import entities

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    entities.set_company_linkedin("Acme", "https://www.linkedin.com/company/acme/")
    rec = entities.load_company_overlay()["acme"]
    assert rec["linkedin"] == "https://www.linkedin.com/company/acme"

    entities.mark_alumni_checked("Acme", "emory")
    rec = entities.load_company_overlay()["acme"]
    assert "emory" in rec["alumni_checked"]
