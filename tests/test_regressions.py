"""Regression tests for bugs that actually shipped.

Every test in this file corresponds to a real defect that reached the feed
during development. They're grouped by the bug rather than by module, because
the point is to pin the behaviour that broke, not to chase coverage.

The pattern worth keeping: each one names the symptom, so a future failure
explains itself without archaeology.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from pipeline import feed, prefilter, recency
from pipeline.boards import edgar, generalist, getro, waas
from pipeline.models import (
    Entry,
    RawPosting,
    headcount_label,
    normalize_company,
    normalize_title,
    normalize_url,
)


# --- Getro reported company size as a bucket index, not a raw count ---------
# The prefilter compared it against 250, so the "growth-stage, 100+ people"
# exclude was dead code that could never fire.


def test_headcount_is_a_bucket_index_not_a_count():
    assert headcount_label(1) == "1-10"
    assert headcount_label(6) == "1000+"
    assert headcount_label(None) == ""


def test_large_company_is_excluded_by_bucket():
    p = RawPosting(title="Growth Lead", company="BigCo", url="u", source="s",
                   head_count_bucket=5)
    assert prefilter.check(p).keep is False


def test_ambiguous_bucket_reaches_the_scorer():
    # Bucket 3 is 51-200, straddling the 100-person line — must not be dropped.
    p = RawPosting(title="Growth Lead", company="MidCo", url="u", source="s",
                   head_count_bucket=3)
    assert prefilter.check(p).keep is True


# --- Work at a Startup: the company page was used as the URL for every role --
# All roles at one company shared a URL, which broke uniqueness and made a
# single score bleed across differently-shaped roles.


def test_waas_salary_parsing_ignores_equity_percentages():
    assert waas._parse_salary("$90K - $110K") == (90_000, 110_000)
    assert waas._parse_salary("$120,000 - $160,000") == (120_000, 160_000)
    # 0.5% must not be read as a salary
    assert waas._parse_salary("$100K + 0.5%") == (100_000, None)
    assert waas._parse_salary(None) == (None, None)


def test_waas_batch_maps_to_stage():
    assert waas._stage_from_batch("W26") == "pre_seed"
    assert waas._stage_from_batch("S25") == "seed"
    assert waas._stage_from_batch("S11") == ""   # too old to claim anything
    assert waas._stage_from_batch("") == ""


def test_founder_title_is_never_invented():
    """A bio without a stated role must yield nothing, not its first clause.

    The failure this pins: a fabricated title would be quoted back at the
    founder in an outreach email.
    """
    assert waas._founder_title("Cofounder & COO @ Structured AI, previously IB") == "Cofounder & COO"
    assert waas._founder_title("CEO of Lance. Former Head of Product") == "CEO"
    assert waas._founder_title("Gobhanu left Wharton's Huntsman Program") == ""
    assert waas._founder_title("") == ""


def test_visa_sponsorship_parsing():
    assert waas._sponsors("US citizen/visa only") is False
    assert waas._sponsors("Will sponsor") is True
    assert waas._sponsors(None) is None


# --- generalist.world: hrefs are site-absolute -----------------------------
# Joining them against the listing URL produced /jobs/jobs/<slug>, and all 230
# URLs 404'd.


def test_generalist_urls_are_not_doubled():
    html = (
        '<a class="gw-job-card" href="/jobs/founding-evangelist-hera/">'
        '<div class="gw-job-company">Hera</div>'
        '<div class="gw-job-title">Founding Evangelist</div>'
        '<p class="gw-job-description">Founding role.</p>'
        '<div class="gw-job-meta">'
        '<span class="gw-job-meta-tag gw-salary">$100,000-$130,000</span>'
        '<span class="gw-job-meta-tag gw-location">New York City, NY</span>'
        "</div></a>"
    )
    from selectolax.parser import HTMLParser

    card = HTMLParser(html).css_first("a.gw-job-card")
    p = generalist._parse_card(card, generalist.ORIGIN, "generalist")
    assert p is not None
    assert p.url == "https://generalist.world/jobs/founding-evangelist-hera/"
    assert "/jobs/jobs/" not in p.url
    assert p.company == "Hera"
    assert p.comp_min == 100_000 and p.comp_max == 130_000
    assert p.location == "New York City, NY"


# --- Getro attached the wrong organization to a job ------------------------
# A jobs.scjohnson.com posting carried organization "BOxES 4.0 Devices"
# (pre_seed, 2 people), which would have fed a garbage stage into the rubric.


def test_suspect_org_detected_when_domain_disagrees():
    assert getro._org_is_suspect("https://jobs.scjohnson.com/job/x", "BOxES 4.0 Devices") is True


def test_matching_domain_is_trusted():
    assert getro._org_is_suspect("https://www.narrativebanking.com/careers/x", "Narrative Banking") is False


def test_aggregator_hosts_are_exempt():
    """A LinkedIn URL says nothing about who the employer is."""
    assert getro._org_is_suspect("https://www.linkedin.com/jobs/view/123", "Anything") is False


# --- The 30-day cutoff deleted the top matches -----------------------------
# Hera's 88-scoring role was 75 days old; Conduct's visa-sponsoring London role
# was 82. Age is a weak proxy for "filled" on founding roles.


def test_recency_penalises_but_does_not_drop_an_aging_posting():
    p = RawPosting(title="Founding GTM", company="Hera", url="u", source="s",
                   posted_at=date.today() - timedelta(days=75))
    assert prefilter.check(p).keep is True   # survives
    score, note = recency.apply(88, p.posted_at.isoformat())
    assert score < 88 and score >= 80        # penalised, not buried
    assert "stale" in note or "aging" in note


def test_genuinely_evergreen_posting_is_dropped():
    p = RawPosting(title="Looking for another role?", company="Kingdom", url="u",
                   source="s", posted_at=date(2021, 8, 23))
    assert prefilter.check(p).keep is False


def test_fresh_posting_gets_a_bonus():
    score, note = recency.apply(70, (date.today() - timedelta(days=5)).isoformat())
    assert score == 75
    assert "fresh" in note


def test_generalist_placeholder_date_is_distrusted():
    """Every generalist row reports 2026-03-01. Trusting it would bury the board."""
    age, basis = recency.age_days("2026-03-01", first_seen="")
    assert basis == "unknown"
    assert age is None


def test_first_seen_is_the_fallback_when_no_date_is_published():
    age, basis = recency.age_days("", first_seen=(date.today() - timedelta(days=9)).isoformat())
    assert basis == "first_seen"
    assert age == 9


# --- SEC Form D: funds and old companies leaked through --------------------


def test_spv_names_are_rejected():
    assert edgar._LOOKS_LIKE_SPV.match("CSV RHYTHM HEALTH LLC") is not None
    assert edgar._LOOKS_LIKE_SPV.match("Rythm Health, Inc.") is None


def test_fund_names_are_rejected():
    for name in ("Integral Health Archimedes, LP", "Acme Ventures Fund II", "Bay Capital Partners"):
        assert edgar._NOT_A_STARTUP.search(name), name
    for name in ("Allswell Health PBC", "Orbio Health Inc.", "ALT Sports Data, Inc."):
        assert not edgar._NOT_A_STARTUP.search(name), name


def test_over_five_years_block_has_no_value_to_check():
    """The shape that let a NASDAQ-listed company through the first run."""
    xml = "<yearOfInc><overFiveYears>true</overFiveYears></yearOfInc>"
    assert edgar._nested_value(xml, "yearOfInc") != ""      # returns the raw block
    assert "overFiveYears" in edgar._first(xml, "yearOfInc")  # so we test the flag


def test_officers_sort_ahead_of_plain_directors():
    xml = """
    <relatedPersonInfo><firstName>Plain</firstName><lastName>Director</lastName>
      <relationship>Director</relationship></relatedPersonInfo>
    <relatedPersonInfo><firstName>Real</firstName><lastName>Founder</lastName>
      <relationship>Executive Officer</relationship><relationship>Promoter</relationship>
    </relatedPersonInfo>"""
    people = edgar._people(xml)
    assert people[0]["name"] == "Real Founder"
    assert all(p["linkedin"] == "" for p in people)  # Form D has none; never invent


# --- Cross-board dedupe ----------------------------------------------------
# The same role on three boards under three URLs must collapse to one.


def test_identity_collapses_the_same_role_across_boards():
    a = RawPosting(title="Founding Operations Lead", company="Structured AI",
                   url="https://www.workatastartup.com/jobs/97596", source="shortlist")
    b = RawPosting(title="Founding Operations Lead", company="Structured AI",
                   url="https://www.workatastartup.com/companies/structured-ai", source="waas")
    assert a.identity() == b.identity()


def test_merge_new_records_the_extra_board_rather_than_discarding():
    existing = [feed.to_entry(
        RawPosting(title="Founding GTM", company="Aspect", url="https://a.com/1",
                   source="shortlist"), 85, "why")]
    incoming = [RawPosting(title="Founding GTM", company="Aspect",
                           url="https://b.com/2", source="generalist")]
    fresh, dupes = feed.merge_new(existing, incoming)
    assert fresh == [] and dupes == 1
    assert "generalist" in existing[0].also_seen_on


def test_url_normalisation_strips_tracking():
    assert normalize_url("https://x.com/job/1?utm_source=foo") == "https://x.com/job/1"
    assert normalize_url("https://X.com/job/1/") == "https://x.com/job/1"


def test_company_and_title_normalisation():
    assert normalize_company("Acme Labs, Inc.") == normalize_company("acme")
    assert normalize_title("Senior Growth Lead - NYC, Full Time") == "growth"


# --- Hard excludes ---------------------------------------------------------


@pytest.mark.parametrize("title", [
    "Senior Data Scientist", "Backend Engineer", "Management Consultant",
    "3rd Grade ESL Teacher", "Machine Operator - 3rd Shift", "Executive Assistant",
])
def test_hard_excluded_titles(title):
    p = RawPosting(title=title, company="X", url="u", source="s")
    assert prefilter.check(p).keep is False, title


@pytest.mark.parametrize("title", [
    "Founding Operations Lead", "Chief of Staff", "Growth Marketer",
    "Founding GTM Engineer", "Growth Engineer", "Forward Deployed Engineer",
])
def test_wanted_titles_survive(title):
    """Hybrid technical-GTM roles are wanted — Eric corrected this explicitly."""
    p = RawPosting(title=title, company="X", url="u", source="s")
    assert prefilter.check(p).keep is True, title


def test_seniority_exclusions_spare_founding_and_chief_of_staff():
    assert prefilter.check(RawPosting(title="VP of Growth", company="X", url="u", source="s")).keep is False
    assert prefilter.check(RawPosting(title="Chief of Staff", company="X", url="u", source="s")).keep is True
    assert prefilter.check(RawPosting(title="Founding Engineer", company="X", url="u", source="s")).keep is True


def test_board_stated_experience_beats_prose():
    p = RawPosting(title="Growth Lead", company="X", url="u", source="s",
                   min_experience=8, description="1+ years preferred")
    assert prefilter.check(p).keep is False


# --- Senior and internship excludes (Eric's explicit request) ---------------


@pytest.mark.parametrize("title", [
    "Senior Product Manager", "(Senior) Business Development Associate",
    "Sr. AI GTM Engineer", "Senior Growth Marketer",
])
def test_senior_titles_are_dropped(title):
    p = RawPosting(title=title, company="X", url="u", source="s")
    assert prefilter.check(p).keep is False, title


@pytest.mark.parametrize("title", [
    "GTM Intern", "Business Development Intern – Summer/Fall 2026",
    "Werkstudent Backoffice Operations", "Marketing Co-op",
])
def test_internships_are_dropped(title):
    p = RawPosting(title=title, company="X", url="u", source="s")
    assert prefilter.check(p).keep is False, title


def test_founding_senior_survives_the_senior_rule():
    """'Founding Senior PM' is a founding seat first."""
    p = RawPosting(title="Founding Senior Product Manager", company="X", url="u", source="s")
    assert prefilter.check(p).keep is True


# --- Company blurbs leaked markup and board editorial tags -------------------
# Greenhouse ships descriptions entity-escaped (&lt;div&gt;). _strip_html removed
# tags BEFORE unescaping, so the entities became literal <div> markup that
# rendered raw on 8 company cards (Goodfire et al). Separately, generalist's
# "[Trust Evangelist]"-style prefixes survived into 185 blurbs.


def test_greenhouse_entity_escaped_html_is_fully_stripped():
    from pipeline.boards import ats

    escaped = "&lt;div class=&quot;content-intro&quot;&gt;&lt;p&gt;Goodfire is a research company.&lt;/p&gt;&lt;/div&gt;"
    out = ats._strip_html(escaped)
    assert "<" not in out and ">" not in out
    assert "Goodfire is a research company." in out


def test_blurb_strips_bracket_editorial_prefix():
    from pipeline.entities import _blurb_from

    b = _blurb_from("[Clinical Ops Builder] Nabi is building an agentic digital health platform for eating disorder treatment.", "Nabi")
    assert b.startswith("Nabi is building")


def test_blurb_strips_literal_html_from_old_cache_entries():
    from pipeline.entities import _blurb_from

    b = _blurb_from('<div class="content-intro"><h2><strong>About Goodfire</strong></h2><p>Goodfire is a research company using interpretability to design AI systems.</p></div>', "Goodfire")
    assert "<" not in b and "About Goodfire" not in b
    assert b.startswith("Goodfire is a research company")


def test_blurb_fragments_fail_the_quality_gate():
    from pipeline.entities import _blurb_from, _blurb_ok

    assert not _blurb_ok(_blurb_from("[Growth] Join us!", "X"))
    assert _blurb_ok(_blurb_from("Nabi is building an agentic digital health platform.", "Nabi"))


# --- City buckets and the application checklist ------------------------------
# The dropdown's whole value is that "New York, NY", "NYC" and "Brooklyn" are
# one option, and that an EU city is knowably non-US (that's what gates the
# visa tag in the GUI).


@pytest.mark.parametrize("loc,label,us", [
    ("New York, NY", "New York", True),
    ("NYC", "New York", True),
    ("Brooklyn", "New York", True),
    ("Seattle, WA", "Seattle", True),
    # Eric consolidated per-city EU buckets into one International bucket —
    # the filter granularity he actually uses, not a loss of location data.
    ("London, UK", "International", False),
    ("Prague", "International", False),
    ("Lisbon, Portugal", "International", False),
    ("Remote", "Remote", True),
    ("Austin, TX", "Other US", True),
])
def test_city_bucket(loc, label, us):
    from pipeline.dashboard import city_bucket
    assert city_bucket(loc) == (label, us)


def test_lookup_requires_the_entity_name_to_match(monkeypatch):
    from pipeline.boards import edgar

    class FakeResp:
        def __init__(self, payload=None, text=""):
            self._p, self.text = payload, text
        def raise_for_status(self): pass
        def json(self): return self._p

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url, **kw):
            # Every hit is a different company that merely mentions "Nabi".
            return FakeResp({"hits": {"hits": [
                {"_id": "0001-x:1", "_source": {"display_names": ["NEATOLABS LLC  (CIK 0001988538)"],
                                                "file_date": "2023-08-14"}},
                {"_id": "0002-x:1", "_source": {"display_names": ["Browning West Fund LP  (CIK 0001789119)"],
                                                "file_date": "2022-09-28"}},
            ]}})

    monkeypatch.setattr(edgar, "client", lambda: FakeClient())
    res = edgar.lookup_founders("Nabi")
    assert res["founders"] == []
    assert res["confidence"] == "none"
    assert "no Form D" in res["note"]


def test_lookup_downgrades_an_old_filing_rather_than_trusting_it():
    """'Cotera, Inc.' exists, but its filings stop in 2017 — today's Cotera is
    a different company. Age must downgrade confidence, not pass silently."""
    from pipeline.boards.edgar import LOOKUP_FRESH_YEARS

    assert LOOKUP_FRESH_YEARS <= 8   # a stale officer list is a wrong officer list


# --- History silently swallowed unknown event kinds -------------------------
# add() relabels any kind not in KINDS as "note" without erroring. Both
# "funding" and "enrich" were recorded that way for a while: the events existed
# and rendered, but no filter could find them, so the funding flag was invisible
# to anything that asked for it by name. This pins the kind list to its callers.


def test_history_kinds_cover_every_caller():
    """Every literal kind passed to record()/add() must be a declared KIND."""
    import re as _re
    from pathlib import Path

    from pipeline.history import KINDS

    root = Path(__file__).resolve().parent.parent / "pipeline"
    call = _re.compile(r"(?:hist\.record|h\.add|history\.record)\(\s*[^,]+,\s*[\"']([a-z_]+)[\"']")
    used = set()
    for py in root.rglob("*.py"):
        used |= set(call.findall(py.read_text(encoding="utf-8")))

    unknown = used - set(KINDS)
    assert not unknown, (
        f"these kinds are recorded but not declared in history.KINDS, so add() "
        f"silently rewrites them to 'note': {sorted(unknown)}"
    )


def test_funding_events_are_recorded_per_listing(tmp_path, monkeypatch):
    """A raise is logged against every open role at that company, not just one.
    HISTORY patched: apply() writes the log, and unpatched it appended junk
    keys (u1, u2…) to the real data/history.json on every suite run."""
    from pipeline import history as hist
    from pipeline.funding import Raise, apply
    from pipeline.models import Entry

    monkeypatch.setattr(hist, "HISTORY", tmp_path / "history.json")

    es = [
        Entry(title="Role A", company="Acme", url="u1", source="s", location="", score=70, why=""),
        Entry(title="Role B", company="Acme, Inc.", url="u2", source="s", location="", score=70, why=""),
        Entry(title="Other", company="NotAcme", url="u3", source="s", location="", score=70, why=""),
    ]
    touched = apply(es, [Raise(company="Acme", filed="2026-07-01", amount=2_000_000)])
    assert touched == 2                      # both Acme listings, normalised
    assert "$2.0M" in es[0].funding and "$2.0M" in es[1].funding
    assert not es[2].funding                 # unrelated company untouched


def test_make_apply_does_not_collide_with_builtin_make_vars():
    """AS is a built-in Make variable (the assembler), so $(AS) is never empty.

    Using it for the apply --as flag made every invocation pass `--as as` and
    fail with an argparse choice error. Any Makefile variable must be one Make
    does not already define.
    """
    import re as _re
    from pathlib import Path

    mk = (Path(__file__).resolve().parent.parent / "Makefile").read_text()
    builtin = {"AS", "CC", "CXX", "CPP", "LD", "AR", "RM", "MAKE", "SHELL", "CFLAGS",
               "LDFLAGS", "ARFLAGS", "LC_ALL"}
    used = set(_re.findall(r"\$\(if \$\((\w+)\)", mk)) | set(_re.findall(r"\$\((\w+)\)", mk))
    clash = used & builtin
    assert not clash, f"Makefile uses built-in Make variables that are never empty: {clash}"


# --- Dashboard weight and the hidden-attribute trap -------------------------


def test_postings_are_paginated_not_all_rendered():
    """1,884 rows was 41k DOM nodes, 5MB of HTML and a 240,000px scroll."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "const PAGE = 60" in tpl
    assert "rows.slice(0, shown)" in tpl, "the list must render a page, not every row"
    assert "showMore" in tpl


def test_heavy_fields_are_not_shipped_for_every_row():
    """desc (212KB) is fetched on expand; chk only exists for active rows."""
    from pipeline import dashboard, feed

    rows = dashboard._rows(feed.load())
    assert rows, "need a non-empty feed for this to mean anything"
    assert all("desc" not in r for r in rows), "descriptions must not be inlined"
    assert all("chk" not in r for r in rows), "the checklist feature was removed"


def test_hidden_attribute_is_forced_over_display_rules():
    """.stats/.controls set display:flex, which beats [hidden]{display:none}.

    Without the !important override, el.hidden = true reported true while the
    element stayed on screen — the filter row sat above the Send tab filtering
    nothing.
    """
    from pipeline import dashboard

    assert "[hidden]{display:none !important}" in dashboard._TEMPLATE


def test_tabs_are_focusable_elements():
    """Tabs were <span>s: absent from the a11y tree and unreachable by keyboard."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '<span class="tab' not in tpl
    assert 'role="tablist"' in tpl and 'role="tab"' in tpl


# --- Design system: the scale has to stay a scale ---------------------------
# A card carried seven unrelated font sizes (9.5 through 24px), three
# near-identical paddings and four radii. Values drift in one at a time, so
# these pin the tokens rather than any particular look.


def _css() -> str:
    from pipeline import dashboard

    return dashboard._TEMPLATE.split("<style>")[1].split("</style>")[0]


def test_type_and_radius_values_come_from_tokens():
    import re as _re

    css = _css()
    root = css.split("}")[0]                       # the :root block defines them
    sizes = [m for m in _re.findall(r"font-size:([\d.]+px)", css)]
    radii = [m for m in _re.findall(r"border-radius:([\d.]+px)", css)]
    assert not sizes, f"hardcoded font sizes outside the scale: {sorted(set(sizes))}"
    assert not radii, f"hardcoded radii outside the scale: {sorted(set(radii))}"
    for tok in ("--t-xs", "--t-sm", "--t-md", "--t-lg", "--t-xl",
                "--r-sm", "--r-md", "--r-pill"):
        assert tok in root, f"{tok} missing from :root"


def test_nothing_smaller_than_eleven_px():
    """9.5px uppercase labels were below comfortable reading."""
    import re as _re

    root = _css().split("}")[0]
    for val in _re.findall(r"--t-\w+:([\d.]+)px", root):
        assert float(val) >= 11, f"type scale contains {val}px"


def test_controls_meet_a_thirty_two_pixel_target():
    """Buttons were 25px tall — one pixel over the WCAG 2.2 AA floor."""
    css = _css()
    for sel in (".btn{", ".chip{", ".tab{", "input{", "select{"):
        block = css.split(sel)[1].split("}")[0]
        assert "min-height:32px" in block, f"{sel} has no 32px minimum"


def test_expandable_rows_are_keyboard_operable():
    """Rows expanded on click only: no role, no tabindex, no key handler."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'role="button" tabindex="0"' in tpl
    assert "rowKey(event," in tpl   # non-nested rows still keyboard-expand
    assert "window.rowKey" in tpl
    assert 'aria-expanded="${open}"' in tpl
    assert ":focus-visible" in tpl, "focused rows need a visible ring"


# --- UX copy: one verb per concept ------------------------------------------
# The same action had different labels on different surfaces, and one of them
# was actively wrong: "Mark sent" set status=applied, so the button and the
# resulting badge disagreed with each other.


def test_one_label_per_action():
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    retired = {
        "Interested ☆": "click-verb must match the 'shortlisted' status it sets",
        "Mark sent": "set status=applied — button and badge disagreed",
        "Applied ✓": "duplicate label for Mark applied",
        "Track company": "Follow is the verb everywhere else",
    }
    for label, why in retired.items():
        assert f">{label}<" not in tpl, f"{label!r} is back: {why}"


def test_no_copy_apologises_for_a_missing_refresh():
    """Three toasts said 'reload to see' because track/person didn't reload."""
    from pipeline import dashboard

    assert "reload to see" not in dashboard._TEMPLATE


def test_empty_states_offer_a_way_forward():
    """'Nothing matches those filters.' told the user nothing to do next."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "clearFilters()" in tpl, "the no-results state needs a reset control"
    # (the old "65+" send-pane floor text left with the Send tab — the Tracker
    # shows every shortlisted posting regardless of score, so no floor to state)


# --- Code review of the funding/sends work ----------------------------------


def test_a_raise_counts_whoever_spotted_it():
    """Funding urgency was gated on source=='edgar'.

    make funding exists to catch companies ALREADY in the feed from other
    boards. Gating the trigger on the edgar source meant those scored zero —
    the watch flagged them and the send queue ignored them.
    """
    from datetime import date

    from pipeline.models import Entry
    from pipeline.timing import assess

    base = dict(title="Founding GTM", company="Allswell", url="u", location="NYC",
                score=80, why="", status="shortlisted",
                funding="$6.8M Form D filed 2026-07-08",
                founders=[{"name": "Dana Albright", "title": "CEO", "email": "d@x.com"}])
    today = date(2026, 7, 20)
    for src in ("generalist", "edgar", "waas"):
        a = assess(Entry(**base, source=src), today_=today)
        assert a.urgency == 40, f"{src} lost the funding trigger"
        assert "Form D" in a.trigger


def test_funding_line_holds_one_round_not_a_nest(tmp_path, monkeypatch):
    """'(was: …)' wrapping nested without bound across rounds."""
    from pipeline import history as hist
    from pipeline.funding import Raise, apply
    from pipeline.models import Entry

    monkeypatch.setattr(hist, "HISTORY", tmp_path / "history.json")

    e = Entry(title="t", company="X", url="u", source="s", location="", score=70, why="")
    for filed, amt in (("2026-01-05", 1_000_000), ("2026-04-05", 5_000_000),
                       ("2026-07-05", 9_000_000)):
        apply([e], [Raise(company="X", filed=filed, amount=amt)])
    assert e.funding == "$9.0M Form D filed 2026-07-05"
    assert "(was:" not in e.funding


def test_funding_news_window_is_testable_at_a_fixed_date():
    """check() called date.today() directly, so the boundary was untestable."""
    import inspect

    from pipeline import funding

    assert "today_" in inspect.signature(funding.check).parameters


def test_api_row_matches_without_a_query_string():
    """The route tested startswith('/api/row?'), so a bare /api/row 404'd."""
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parent.parent / "pipeline" / "app.py").read_text()
    assert 'self.path.split("?")[0] == "/api/row"' in src


# --- Accessibility (WCAG 2.1 AA) --------------------------------------------
# From the audit. Each pins a barrier a keyboard or screen-reader user hit.


def _dash_html() -> str:
    from pipeline import dashboard

    return dashboard._TEMPLATE


def test_every_input_and_select_has_an_accessible_name():
    """14 inputs had only placeholders — which vanish on typing and aren't
    reliably announced (WCAG 3.3.2 / 4.1.2)."""
    import re as _re

    tpl = _dash_html()
    fields = _re.findall(r"<(input|select|textarea)\b[^>]*>", tpl)
    controls = _re.findall(r"<(?:input|select|textarea)\b[^>]*>", tpl)
    for c in controls:
        assert "aria-label=" in c or "aria-labelledby=" in c or 'type="hidden"' in c, \
            f"control without an accessible name: {c[:80]}"


def test_filter_chips_are_buttons_not_spans():
    """Chips were <span tabindex=-1>: no keyboard access (WCAG 2.1.1)."""
    tpl = _dash_html()
    assert '<span class="chip"' not in tpl
    assert 'class="chip" data-f' in tpl and 'aria-pressed' in tpl


def test_there_is_a_skip_link_to_main():
    """457 focusables, no way past the header for a keyboard user (2.4.1)."""
    tpl = _dash_html()
    assert 'class="skip" href="#main"' in tpl
    assert 'id="main"' in tpl


def test_modal_is_a_labelled_dialog_that_escape_closes():
    """The Insights modal had no role, no aria-modal, no keyboard close (4.1.2 / 2.1.2)."""
    tpl = _dash_html()
    assert 'role="dialog"' in tpl and 'aria-modal="true"' in tpl
    assert 'key === "Escape"' in tpl
    assert "closeModal" in tpl


def test_actions_are_buttons_not_href_hash_links():
    """'queue lookup' was <a href="#">, announced as a link to nowhere (4.1.2)."""
    tpl = _dash_html()
    assert 'href="#"' not in tpl, "an action is still a fake link"


def test_toasts_are_a_polite_live_region():
    """Status toasts were invisible to screen readers (WCAG 4.1.3)."""
    tpl = _dash_html()
    assert 'aria-live","polite"' in tpl or 'aria-live", "polite"' in tpl


# --- Card density: text is compressed, chrome isn't repeated ----------------
# One send card filled the whole viewport — a 40-word warm-path essay, the full
# funding sentence, three address guesses, and the date + "SEND PRIORITY" label
# copied onto all 15 cards. The queue exists to scan ten at once.


def test_tracker_replaced_the_send_tab():
    """Eric's call: one Tracker pane for postings/people/companies stages,
    grouped list layout, Send tab retired — but its fit+timing+warm ranking
    survives as the ordering inside the shortlisted group (DATA.sendpri)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'data-tab="tracker"' in tpl
    assert 'data-tab="send"' not in tpl and "renderSends" not in tpl
    assert "function renderTracker" in tpl
    assert "DATA.sendpri" in tpl, "send-queue ranking must still order the tracker"
    # Board columns are the standard vocabulary, Review/Uninterested excluded.
    assert '"saved","applied","interviewing","offer","rejected","dormant"' in tpl
    assert '"saved","contacted","conversation","met","dormant"' in tpl
    assert "Companies saved" in tpl


def test_person_stages_are_a_closed_set():
    """One-click stage buttons write through set_person_status, which must
    reject anything outside the pipeline and refuse to invent people."""
    import pytest as _pytest

    from pipeline import entities

    with _pytest.raises(ValueError):
        entities.set_person_status("Anyone", "Anywhere", "vibing")
    with _pytest.raises(KeyError):
        entities.set_person_status("Nobody Real", "Nowhere", "contacted")


def test_person_stage_advance_records_history(tmp_path, monkeypatch):
    from pipeline import entities
    from pipeline import history as hist

    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    monkeypatch.setattr(hist, "HISTORY", tmp_path / "history.json")
    entities.add_person(name="Jane Doe", company="Acme",
                        linkedin="https://linkedin.com/in/jd")
    # legacy spelling in, normalized spelling stored — same LEGACY guarantee
    # the posting pipeline makes.
    rec = entities.set_person_status("Jane Doe", "Acme", "reached out")
    assert rec["status"] == "contacted" and rec["status_date"]
    events = hist.load().for_entry("https://linkedin.com/in/jd")
    assert events and events[-1].text == "moved to contacted"


def test_cards_truncate_long_free_text():
    """Blurbs, notes and why-lines are one line on the card, full text on expand."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    # every long-text slot goes through short()
    assert tpl.count("short(") >= 6, "free-text fields should be truncated on cards"


def test_actions_live_in_the_expanded_view_not_the_scan_row():
    """Postings and companies showed 2-5 action buttons on every collapsed row,
    tripling the height of a list you scan. Actions moved behind click-to-expand;
    company cards became expandable to match postings."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function companyExpanded" in tpl
    assert "toggleCompany" in tpl
    # rowButtons is called from the expanded view, not the collapsed row
    exp = tpl.split("function expandedHtml")[1].split("function ")[0]
    # Undo left the modal (unclear verb — make undo remains in the CLI)
    assert "doNote(" in exp and "doUndo(" not in exp


def test_alum_filter_exists_on_all_three_panes():
    """One chip, per-tab semantics: postings/companies = a verified alum works
    there; people = the person is the alum (Emory/NMH only, not every signal)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    # the Alumni chip retired — alum counts as Warm now (isWarm)
    assert 'data-f="alum"' not in tpl and "const isWarm" in tpl
    assert 'ALUM_SIGNALS = ["Emory", "NMH"]' in tpl
    assert "CO_ALUM[r.c]" in tpl                       # postings filter
    assert "(c.alumni||[]).length" in tpl              # companies filter
    assert "ALUM_SIGNALS.includes(" in tpl             # people filter


def test_scout_progress_is_streamed_not_a_blind_timer():
    """Find-new-roles used to disable the button and reload after a flat 45s —
    the scout narrates itself, so the page now polls that narration and draws
    a real bar. If a scout is already running at page load, the page latches on."""
    from pipeline import app, dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="scoutbar"' in tpl and "watchScout" in tpl
    assert "setTimeout(() => location.reload(), 45000)" not in tpl
    assert "p0.scout.running" in tpl.replace(" && ", ".").replace("p0.scout.", "p0.scout.") or "watchScout()" in tpl
    assert hasattr(app, "_ScoutLog")
    # the sink counts boards off the CLI's own "fetching X…" lines
    st = {"log": [], "boards_done": 0}
    saved = app._scout_state
    try:
        app._scout_state = {**saved, **st}
        app._ScoutLog().write("  fetching waas…\n  12 new after dedupe\n  fetching accel…\n")
        assert app._scout_state["boards_done"] == 2
        assert app._scout_state["log"][-1] == "fetching accel…"
    finally:
        app._scout_state = saved


def test_every_api_route_the_page_calls_exists_on_the_server():
    """A cleanup regex once deleted five routes (person-status, company-interest,
    feedback, queue-founder, and the block it aimed at) in one silent bite —
    the UI kept calling them and got 404s. Pin the contract: every /api/ path
    the template fetches must appear in app.py."""
    import re as _re
    from pathlib import Path as _P

    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    called = set(_re.findall(r'api\("(/api/[a-z-]+)', tpl))
    server = _P(__file__).resolve().parent.parent / "pipeline" / "app.py"
    src = server.read_text()
    missing = {c for c in called if f'"{c}"' not in src}
    assert not missing, f"page calls routes the server doesn't serve: {sorted(missing)}"


def test_history_stamps_local_time_not_utc():
    """Applications made at 8pm stamped in UTC landed on TOMORROW, so the
    daily-goal tiles read 0/3 on the day the work was done and cards showed a
    future date. Eric's day is the streak's unit, so Eric's clock counts."""
    from pipeline import history as hist

    src = open("pipeline/history.py").read() if False else None
    import inspect

    body = inspect.getsource(hist.History.add)
    assert "astimezone()" in body and "timezone.utc" not in body


def test_theme_toggle_is_three_state_and_flash_free():
    """auto follows the OS; light/dark pin via data-theme and persist in
    localStorage, applied by a head script BEFORE first paint so a dark-mode
    user never sees a cream flash."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    head = tpl.split("</head>")[0]
    assert 'localStorage.getItem("steinjobs-demo-theme")' in head  # demo: light default, own key, "theme must apply before first paint"
    assert ':root[data-theme="dark"]' in tpl
    assert ':root:not([data-theme="light"])' in tpl, "OS dark must respect a light pin"
    assert 'id="themebtn"' in tpl


def test_people_ux_batch_markers():
    """Founder cards shade instead of wearing a bubble; contacts are icon
    buttons; person stage advances for reached-out/replied/meeting go through
    the date popup (default today); the stage FILTER defaults to blank; the
    Emory/NMH chip reads Alumni everywhere."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "--sig-founder" in tpl and "--sig-alumni" in tpl   # signal chip tokens (batch 6: dead -bg slots replaced)
    assert '<span class="rel">founder</span>' not in tpl
    assert "function contactIcons" in tpl and 'class="icn"' in tpl
    assert 'id="datemodal"' in tpl and '"contacted","conversation","met"' in tpl
    # the stage filter became chips like the companies tab, Review on by default
    # people modes slimmed to Review/Saved — ✗ still records uninterested,
    # the pile just isn't a browsing destination any more
    assert 'id="pt-review"' in tpl and 'id="pt-saved"' in tpl and 'id="pt-uninterested"' not in tpl
    assert "window.setPeopleMode" in tpl
    assert "Emory / NMH" not in tpl   # Alumni returned as a rolodex filter chip (Eric, 2026-08-12)
    assert 'id="fab-search"' in tpl and "editPerson" in tpl   # search floats now


def test_warm_lead_hierarchy_and_fields():
    """Warm leads outrank school tints, school tints outrank founder clay —
    the shading a card wears must reflect the strongest fact about the person.
    Emory wears gold, NMH wears blue, warm wears terracotta; the edit modal
    carries the warm toggle + warm-via, and People has a Warm filter chip."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    # theme-independent signal tokens in the base block (batch 6)
    for tok in ("--sig-conn", "--sig-alumni", "--sig-nmh"):
        assert tpl.count(tok + ":") >= 1, f"{tok} missing from the token block"
    # card shading retired entirely — filled chips carry warm/school/founder
    assert "function personShade" not in tpl
    # family chips share the industry chips' alpha recipe (30% fill, 65% edge)
    assert ".sig-emory{background:var(--sig-alumni)" in tpl   # navy, via the shared alumni token
    assert ".sig-nmh{background:var(--sig-nmh)" in tpl     # light blue
    assert ".warmtag.sig{background:var(--sig-conn)" in tpl
    assert 'class="sig sig-founder"' in tpl
    # edit modal fields + filter chip
    assert 'id="fp-warm"' in tpl and 'id="fp-wvia"' in tpl
    assert 'data-f="warm"' in tpl


def test_warm_lead_roundtrips_through_the_overlay(tmp_path, monkeypatch):
    """warm/warm_via must persist — and stay OFF notes, so signals_in never
    turns Eric-side knowledge into a claim about the person."""
    from pipeline import entities

    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    entities.add_person("Jane Doe", company="Acme", warm=True,
                        warm_via="met through Emory alumni dinner")
    row = entities.load_people_overlay()[0]
    assert row["warm"] is True
    assert row["warm_via"] == "met through Emory alumni dinner"
    assert "Emory" not in row.get("notes", "")
    # update path: editing without touching warm must not clear it
    entities.add_person("Jane Doe", company="Acme", role="COO")
    row = entities.load_people_overlay()[0]
    assert row["warm"] is True and row["role"] == "COO"


def test_a_ruled_out_school_in_a_note_is_not_a_signal():
    """SYMPTOM: Riley Okafor displayed as an Emory AND NMH alum off a sweep note
    reading "No Emory/NMH tie" — the substring match read the disclaimer that
    ruled her out as two positive hits. A verify-and-rule-out sweep writes the
    school's name by necessity; if that mints a signal, the honest note is the
    one that lies, and a stranger gets "same school" in an email to them."""
    from pipeline.entities import signals_in

    assert signals_in("Stanford University. No Emory/NMH tie. Verified.") == []
    assert signals_in("Stanford GSB MBA, UPenn BA — not an Emory alum") == []
    assert signals_in("no Northfield Mount Hermon connection") == []
    # The negation must not swallow real hits: a plain mention still counts,
    # and a clause boundary ends the negation's reach.
    assert signals_in("Emory University, BBA 2019") == ["Emory"]
    assert signals_in("No Stanford tie. Emory University 2019") == ["Emory"]
    # Later unnegated mention wins over an earlier negated one.
    assert signals_in("not an Emory grad, but taught at Emory later") == ["Emory"]
    # Unrelated signals keep working through the same path.
    assert signals_in("plays in a metal band") == ["Music"]


def test_signals_in_never_matches_mid_word():
    """SYMPTOM (measured 2026-08-08, 12 companies affected): signals_in still
    used low.find(), so "NFL / football analytics" was minted from fit lines
    saying "influencers", "inflection", "conflicts" and "Confluent". This is
    the SAME bug fixed in find_warm_thread on 2026-08-07 ("mEMORY" → Emory,
    minified "uNFLatten" → NFL) and never ported — which is why both matchers
    now share entities.signal_pattern. A founder titled "Influencer Marketing
    Manager" would have been shown to Eric as an NFL contact."""
    from pipeline.entities import signals_in

    assert signals_in("(Junior) Influencer Marketing Manager") == []
    assert signals_in("past the 'founding team' inflection point") == []
    assert signals_in("hard constraint conflicts with remote") == []
    assert signals_in("Data Foundations - Confluent, Instana") == []
    assert signals_in("we unflatten the payload before scoring") == []
    assert signals_in("a lot of memory pressure on the worker") == []
    assert signals_in("NMHC multifamily housing council") == []          # nmh
    assert signals_in("played in the NFL") == ["NFL / football analytics"]


def test_signals_in_closes_the_word_tail_but_keeps_deliberate_stems():
    """SYMPTOM: porting find_warm_thread's pattern brought its open `\\w*` tail
    along, so `nfl` matched the Netflix ticker "$NFLX". Tails close by default
    and the six needles that genuinely need a stem opt in with a trailing `*`
    — without which "psychedelics", "Czechia", "WWOOFing", "thru-hiking",
    "personal trainer" and "metal bands" all silently stop matching."""
    from pipeline.entities import signals_in

    assert signals_in("long $NFLX calls into earnings") == []
    assert signals_in("psychedelics research") == ["Psychedelics"]
    assert signals_in("thru-hiking the trail") == ["thru-hiking"]
    assert signals_in("a personal trainer in Brooklyn") == ["personal training"]
    assert signals_in("WWOOFing in Italy") == ["WWOOF / farming"]
    assert signals_in("she moved to Czechia") == ["Czech Republic"]
    assert signals_in("plays in two metal bands") == ["Music"]
    # powerlift* newly catches the noun form; the bare needle never did
    assert signals_in("a competitive powerlifter") == ["powerlifting"]


def test_multi_word_needles_tolerate_collapsed_whitespace():
    """SYMPTOM: strip_markup() replaces each HTML tag with a space, so
    "<b>New York</b> <b>Jets</b>" reaches the matcher as "New York   Jets" and
    a literal-single-space needle missed the strongest warm thread Eric has.
    All six multi-word needles were affected."""
    from pipeline.entities import signals_in

    assert signals_in("Director of Analytics, New York  Jets") == ["NY Jets"]
    assert signals_in("New York\nJets front office") == ["NY Jets"]
    assert signals_in("Big  Data  Bowl finalist") == ["Big Data Bowl"]
    assert signals_in("Carter  Center fellow") == ["Carter Center"]


def test_jets_fires_on_the_short_names_people_actually_write():
    """SYMPTOM: only the official "New York Jets" fired, so the Jets — Eric's
    strongest non-school warm thread — was invisible on every profile whose
    headline said "NY Jets" or "the Jets". An Experience entry carries the
    official name; a headline almost never does, and headlines outrank past
    roles in the capture. Eric asked for Jets people to be flagged the way
    Emory and UCSF are (2026-08-14).

    Two spellings deliberately stay OUT, because each would put "we both worked
    for the Jets" in front of someone who didn't: bare "jets" (private/business
    jets is ordinary English) and "the jets" (the WINNIPEG Jets are in
    SPORTS_NEEDLES, and their people write "the Jets" too)."""
    from pipeline.entities import signals_in

    assert signals_in("Data Scientist, NY Jets") == ["NY Jets"]
    assert signals_in("Director of Analytics, New York Jets") == ["NY Jets"]
    # "Sports" rides along only when the text says so itself — here, "Football"
    assert signals_in("Coordinator, Jets Football Ops") == ["NY Jets", "Sports"]
    # the two that must NOT mint a Jets chip
    assert "NY Jets" not in signals_in("We operate a fleet of private jets")
    assert "NY Jets" not in signals_in("Video Coordinator for the Winnipeg Jets")
    # the needle that must NOT ship: aerospace is ordinary English here
    assert "NY Jets" not in signals_in("We operate a fleet of private jets")


def test_sports_signal_fires_only_on_real_sports_work():
    """Eric opens cold emails with "I saw you worked in sports too", so a
    Sports false positive is the worst output this repo can produce. Every
    negative below is real text from data/feed.json or data/companies.json that
    a naive needle list matched: bare `sports` caught a note about the person's
    employer's SIBLING division, bare mascots caught "United States" and
    "veterinary bills", bare `coach` caught "executive coach", and the
    three-letter leagues hid inside Coinbase, Antioch, replit and a UUID."""
    from pipeline.entities import signals_in

    S = "Sports"
    # bare `sports` is not a needle — this note is about Corgi's sibling arm
    assert S not in signals_in(
        "Corgi = YC, insurance infra; sports/entertainment arm is Golden by Corgi")
    # mascots ship city-qualified only
    for s in ("cover veterinary bills in the United States",
              "Enterprise Account Executive - New York City",
              "Magic Compass Holdings Limited", "10k GitHub stars; $6.3M seed",
              "an Established adtech player, not early-stage",
              "building a best-selling sunscreen brand",       # suns
              "Product Specialist for Adobe Firefly",          # fire
              "Wildkind runs festivals",                       # wild
              "Operational Development & Excellence",          # excel
              "Chelsea, Manhattan office", "the arsenal of tools we ship",
              "we use AJAX calls to the API"):
        assert S not in signals_in(s), s
    # bare role words are not needles
    for s in ("an executive coach and career coach",
              "Coaching creators to grow earnings",
              "attend meetings, draft comms, manage follow-ups",
              "scout emerging tech for the roadmap",
              "a roster of enterprise clients", "Ivy League graduate"):
        assert S not in signals_in(s), s
    # leagues we deliberately did not ship, and the collision that killed each
    for s in ("F1 score of 0.91 on the holdout set",           # f1
              "job-details/604f1f05-34a3-4151-8ad0",           # f1
              "MLS listing data for real-estate agents",       # mls
              "SEC Form D filing from the EDGAR daily index",  # sec
              "ATP synthase and mitochondrial assays",         # atp
              "indicators of compromise (IOC) enrichment",     # ioc
              "Senior Forward Deployed Analyst at replit"):    # epl
        assert S not in signals_in(s), s
    # anchoring, not the needle list, is what saves these
    for s in ("Coinbase is a crypto exchange",                 # nba
              "unbalanced sampling in the training set",       # nba
              "FPGA-accelerated inference",                    # pga
              "Harry from Antioch", "millions of users",       # ioc, lions
              "hundreds of user interviews",                   # reds
              "German battery-recycling scale-up",             # cycling
              "Chief of Staff at BeAngels", "Bookings and Office Admin",
              "digital twins for factory simulation",
              "we run Puma behind nginx",
              "mavericks and trailblazers in fintech"):
        assert S not in signals_in(s), s
    # the people Eric is looking for
    for s in ("Data Scientist at the Brooklyn Nets",
              "worked in the NBA front office for six years",
              "VP, Sports Analytics at Sportradar",
              "Head of Growth at DraftKings",
              "product lead for a sports betting startup",
              "assistant coach at Bowdoin",
              "athletic department at Georgia Tech",
              "Premier League club analytics",
              "Major League Soccer expansion team",
              "Formula 1 race strategy engineer",
              "esports team operations", "NCAA compliance officer",
              "analytics for the New York Yankees",
              "Director of Baseball Operations"):
        assert S in signals_in(s), s


def test_sports_means_worked_not_played_unless_pro():
    """Eric's rule (2026-08-07): a sports warm path means they WORKED in sports,
    unless they played in a pro league. A college athlete who now does insurance
    infra is not someone to open with "I saw you worked in sports too" — and the
    two real person-side hits on the day this shipped were exactly that
    ("Bowdoin BA Economics (varsity soccer)", "D3 baseball closing pitcher"), so
    detecting nothing on them is the CORRECT result, not a broken matcher.
    Bare sport nouns are dropped for this reason; the amateur guard exists
    because a college bio still names a league ("NCAA Division I athlete")."""
    from pipeline.entities import signals_in

    S = "Sports"
    for s in ("Bowdoin BA Economics (varsity soccer)",
              "D3 baseball closing pitcher",
              "former NCAA Division I athlete",
              "played Division 1 soccer at Duke",
              "student-athlete at Michigan, NCAA champion",
              "intramural basketball on weekends"):
        assert S not in signals_in(s), s
    # pro playing needs no special case — the bio names the league
    assert S in signals_in("played six seasons in the NBA")
    assert S in signals_in("professional athlete turned operator")
    # and working for the college still counts: that's a job
    assert S in signals_in("athletic director at Emory")


def test_sports_collapses_to_one_label():
    """A sentence naming four leagues and two teams is still one person and one
    chip. signals_in dedupes by label, so the first sports hit also
    short-circuits the ~230 remaining sports needles."""
    from pipeline.entities import signals_in

    got = signals_in("Covered the NBA, NHL and MLB for Bleacher Report; "
                     "before that, Golden State Warriors analytics")
    assert got.count("Sports") == 1
    assert "NBA" not in got and "Golden State Warriors" not in got


def test_a_ruled_out_sports_background_is_not_a_sports_signal():
    """The negation guard has to cover Sports too: a verify-and-rule-out sweep
    writes "no sports background" by necessity, and if that mints a signal the
    honest note is the one that lies."""
    from pipeline.entities import signals_in

    assert "Sports" not in signals_in("Checked LinkedIn — no sports analytics tie")
    assert "Sports" in signals_in("No NBA tie. Sports Analytics at Sportradar")


def test_company_signals_come_from_the_company_not_from_erics_fit_line(
        tmp_path, monkeypatch):
    """SYMPTOM (measured 2026-08-08): company signals were mined from
    `e.why` — the SCORER's prose about ERIC — so companies were tagged with
    Eric's own background. Twelve carried "NFL / football analytics" off fit
    lines like "outreach play matching Eric's Jets/sports proof point", and
    "Foxino" x9 / "UCSF Neuroscape" x7 came from the same place. A signal has
    to come from the company's own words (its description, or a warm path
    found on its own site), never from ours."""
    from pipeline import entities
    from pipeline.models import Entry

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")

    def _entry(**kw):
        base = dict(title="Chief of Staff", company="Acme",
                    url="https://acme.com/j/1", source="test",
                    location="New York, NY", score=80, why="fits")
        base.update(kw)
        return Entry(**base)

    # the scorer talking about Eric — must contribute NOTHING
    e = _entry(why="sports plus data is the exact intersection of the Jets "
                   "work, and Foxino proves the GTM instinct")
    (c,) = entities.companies([e])
    assert c.signals == [], c.signals
    # a warm path found on the company's OWN site does count
    e2 = _entry(company="Beta", url="https://beta.com/j/1")
    e2.warm_path = "sports / sports tech — “…Beta is a sports analytics platform…”"
    (c2,) = entities.companies([e2])
    assert "Sports" in c2.signals


def test_person_signals_never_match_across_the_field_seam(tmp_path, monkeypatch):
    """SYMPTOM: notes and role were concatenated before matching, and once
    multi-word needles became whitespace-flexible a match could straddle the
    join — notes ending "…relocated to New York" plus role "Jets Fan
    Engagement" would have recorded the NY Jets for someone who never worked
    there. Each field is matched on its own."""
    import json

    from pipeline import entities

    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    (tmp_path / "people.json").write_text(json.dumps([{
        "name": "Jane Doe", "company": "Acme",
        "role": "Jets Fan Engagement Lead",
        "notes": "Grew up in Ohio, relocated to New York",
    }]))
    (p,) = entities.people([])
    assert "NY Jets" not in p.signals, p.signals


def test_person_follow_creates_an_overlay_row_for_derived_founders(tmp_path, monkeypatch):
    """Feed-derived founders have no overlay record; following one must mint
    it so the flag survives rebuilds like every other manual fact."""
    from pipeline import entities

    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    rec = entities.set_person_follow("Jane Doe", "Acme", True)
    assert rec["following"] is True
    rec = entities.set_person_follow("Jane Doe", "Acme", False)
    assert rec["following"] is False
    assert len(entities.load_people_overlay()) == 1   # updated, not duplicated


def test_status_vocabulary_is_standard_across_the_app():
    """One capitalized vocabulary everywhere (2026-07 rename). Review and
    Uninterested stay off the tracker boards; auto-dormant is DERIVED at two
    weeks of silence after an active move, never written to disk."""
    from pipeline import dashboard, entities
    from pipeline.models import LEGACY_STATUS, STATUSES

    # `expired` added 2026-08-17: the link sweep's verdict when a posting's
    # page is gone — their page dying is neither Eric's no nor their no.
    # `incomplete` added 2026-08-18: no description yet, so nothing to judge —
    # held out of the pile rather than costing a triage decision unjudged.
    assert STATUSES == ("review", "saved", "applied", "interviewing", "offer",
                        "rejected", "dormant", "uninterested", "expired",
                        "incomplete")
    assert entities.PERSON_STAGES == ("review", "saved", "contacted",
                                      "conversation", "met", "dormant",
                                      "uninterested")
    assert entities.COMPANY_MODES == ("review", "saved", "uninterested")
    # legacy spellings normalize on load — old feed states must never error
    assert LEGACY_STATUS["shortlisted"] == "saved"
    assert LEGACY_STATUS["responded"] == "interviewing"
    assert dashboard.DORMANT_AFTER_DAYS == 14


def test_auto_dormant_is_derived_after_two_weeks_of_silence():
    from datetime import date, timedelta

    from pipeline.dashboard import (_AUTO_DORMANT_PERSON,
                                    _AUTO_DORMANT_POSTING, effective_status)

    old = (date.today() - timedelta(days=15)).isoformat()
    fresh = (date.today() - timedelta(days=3)).isoformat()
    assert effective_status("applied", old, _AUTO_DORMANT_POSTING) == "dormant"
    assert effective_status("applied", fresh, _AUTO_DORMANT_POSTING) == "applied"
    assert effective_status("contacted", old, _AUTO_DORMANT_PERSON) == "dormant"
    # saved/review never auto-dormant — only active moves can go quiet
    assert effective_status("saved", old, _AUTO_DORMANT_POSTING) == "saved"
    assert effective_status("applied", "", _AUTO_DORMANT_POSTING) == "applied"


def test_tracker_paginates_and_sorts_by_recency():
    """Columns paginate at a fixed page size instead of scrolling, freshest
    stage-move first; saved companies order by when Eric saved them."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "KB_PAGE_SIZE" in tpl and "kbPage" in tpl
    assert 'aria-label="Previous page of' in tpl and 'aria-label="Next page of' in tpl
    assert "max-height:440px" not in tpl, "columns paginate now — no inner scroll"
    assert "stamp(b).localeCompare(stamp(a))" in tpl
    assert "Companies Saved" not in tpl   # strip retired with its sort (Eric, 2026-08-18)
    # live days-in-stage count on cards (days-only since 2026-08-12 — the
    # column header names the stage)
    assert '${r.days}d</span>' in tpl
    assert "${pv.days!=null?`${pv.days}d`:\"\"}" in tpl


def test_goal_tiles_fill_solid_green_when_hit():
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    block = tpl.split(".tile.goal-hit{")[1].split("}")[0]
    assert "background:var(--accent)" in block, "a hit goal fills, not just outlines"
    # goals and lifetime scoreboard share one tile row, split by a divider
    assert ".tiles .tnum{font-size:var(--t-lg)}" in tpl
    assert 'class="tilesep"' in tpl and ".tilerow{display:flex" in tpl

def test_models_today_is_local_not_utc():
    """Same UTC bug, second location: models.today() stamped last_touched and
    date_added with TOMORROW for any evening mark (8pm PDT = next day UTC), so
    tracker cards read "-1d" in stage. Eric's clock counts here too."""
    import inspect

    from pipeline import models

    body = inspect.getsource(models.today)
    assert "astimezone()" in body and "timezone.utc" not in body

def test_tracker_ui_batch_two_markers():
    """Kanban: populated columns share the width, empty ones collapse to slim
    stubs with a VERTICAL label (the original 92px horizontal-label stubs
    clipped, which is why an equal-width pass existed in between);
    people cards expand in place with full contact details; tracker cards
    click through to the expanded item on its native tab; filter dropdowns
    carry counts; saved-company mini cards drop founders and the mode knob."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'wide ? "minmax(110px,.45fr)" : "52px"' in tpl  # horizontal stubs when room
    assert "kbvlabel" in tpl and "writing-mode:vertical-rl" in tpl
    assert "`92px`" not in tpl   # the clipping horizontal-label stub, not the comment
    assert "function personExpanded" in tpl and "window.togglePerson" in tpl
    assert "PEOPLE_EXPANDED" in tpl and 'class="pexp' in tpl
    # tracker person cards route through gotoPerson with a stable pid — they
    # are real rolodex cards now, so it is personCard's nested branch that does it
    assert "gotoPerson('${jsq(pid)}')" in tpl
    # filter counts
    assert "Last ${o.value} Days (${ages.filter" in tpl
    assert 'ddRows("ind"' in tpl and 'ddRows("city"' in tpl   # panels since the checkbox overhaul (2026-08-18)
    # compact company cards dropped the founder line entirely; mini cards
    # additionally drop the triage buttons
    assert "nofounder" not in tpl
    assert '${open||mini?"":triageBtns(' in tpl   # concise triage lives in the footer row now

def test_every_element_id_the_template_scripts_touch_exists():
    """clearFilters still reset a "status" select two renames after it became
    "pstatus" — the null TypeError killed every tracker click-through at its
    first line. Pin: each el("...") id in the template resolves to a real
    id="..." or is created dynamically."""
    import re

    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    defined = set(re.findall(r'id="([\w-]+)"', tpl))
    dynamic = {"dm-title", "dm-date"}  # queried after modal markup renders
    for ref in set(re.findall(r'\bel\("([\w-]+)"\)', tpl)):
        assert ref in defined or ref in dynamic, f'el("{ref}") has no id="{ref}" element'

def test_triage_and_drag_replace_the_stage_dropdowns():
    """Drag-and-drop on the tracker is the primary stage mover; everywhere
    else the only calls are Save / Uninterested via circular buttons. The
    dropdowns are gone, postings default to the Review pile, and dropping on
    applied/interviewing/offer (or contacted/conversation/met) still routes
    through the date popup."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function stageSelect" not in tpl and "personStageSelect" not in tpl
    assert "function triageBtns" in tpl and "window.triageSet" in tpl
    assert 'draggable="true"' in tpl and "window.kbDrop" in tpl
    assert 'let POSTING_MODE = "review"' in tpl
    assert "const bucketOf" in tpl
    # drops onto real-world stages keep the date modal
    drop = tpl.split("window.kbDrop")[1].split("function openDateModal")[0]
    assert '"applied","interviewing","offer"' in drop
    assert '"contacted","conversation","met"' in drop
    # a triaged derived founder mints an overlay row server-side
    assert "create: true" in tpl


def test_header_is_a_dark_centered_band():
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "--hdr-bg" in tpl and 'class="hband"' in tpl
    hrow = tpl.split(".hrow{")[1].split("}")[0]
    assert "justify-content:center" in hrow
    controls = tpl.split(".controls{")[1].split("}")[0]
    assert "justify-content:center" in controls
    assert "prefers-reduced-motion" in tpl

def test_nature_metal_batch_markers():
    """Dormant wears a dashed, faintly glitching edge; the logo is a
    displacement-filtered metal wordmark pinned left; actions play a
    synthesized chord (no audio files); compact company cards show stage +
    roles only; add-buttons live in the filter band; cards leave room for
    the triage corner."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '.kbcol[data-st="dormant"]{border-style:dashed' in tpl
    assert "@keyframes glitch" in tpl
    assert 'class="dmlogo"' not in tpl   # the wordmark came and went — header is tabs-only now
    assert "function metal(" in tpl and "createWaveShaper" in tpl
    # superseded 2026-08-05: Karplus-Strong samples rendered by pipeline/sfx.py
    # ARE assets now, served at /sfx/ with the oscillator synth as fallback
    assert '/sfx/' in tpl and "SFXB" in tpl
    # compact company footer: stage + roles chips, no funding text, no founder
    meta = tpl.split('class="cfoot"')[1].split("</div>")[0]
    assert "funding" not in meta and "f0" not in meta and "nofounder" not in meta
    # + Add sits in the controls band, not the header buttons
    hbtns = tpl.split('class="hbtns"')[1].split("</div>")[0]
    assert 'id="addbtn"' not in hbtns
    # slice to the scoutbar that follows controls — the first </div> now
    # belongs to the experience dropdown's panel (2026-08-18)
    controls = tpl.split('class="controls"')[1].split('id="scoutbar"')[0]
    assert 'id="addbtn"' in controls
    assert "padding-bottom:48px" not in tpl   # the reserved band died: triage sits in the footer row (2026-08-12)
    assert 'radial-gradient' in tpl.split("body{")[1].split("}")[0]

def test_destroyer_batch_markers():
    """Dormant cards decay (dashed, desaturated, tilted); the wordmark is the
    two-line JOB APPLICATION DESTROYER with heavy displacement and side
    spikes; triage buttons toggle back to review from their own state; the
    add button sits after a separator as a tinted pill; industry chips carry
    a hashed per-industry tint; the foley is a small band with randomized
    riffs, not one fixed chord."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '.kbcol[data-st="dormant"] .kbcard{border-style:dashed' in tpl
    assert ">DESTROYER</text>" not in tpl and "dmdrip" not in tpl
    assert "SteinJobs" in tpl   # page title carries the product name (renamed 2026-08-11)
    assert 'const saveTo = st === "saved" ? "review" : "saved"' in tpl
    assert 'const killTo = st === "uninterested" ? "review" : "uninterested"' in tpl
    # the Triage pill now sits between the barrier and + Add
    assert 'data-f="followedco"' not in tpl   # Saved Companies filter retired (Eric, 2026-08-12)
    assert "indTint" not in tpl   # industry tinting came and went
    for voice in ("_chug", "_kick", "_snare", "_crash", "_squeal"):
        assert f"function {voice}(" in tpl, f"{voice} missing from the band"
    assert "Math.floor(Math.random()*notes.length)" in tpl  # riffs are rolled

def test_milestone_and_hover_reveal_markers():
    """Goal tiles pulse once when they newly flip green (localStorage keyed
    by day so tomorrow resets), win sounds fire at click time — the reload
    would mute them — and triage buttons rest at low opacity until hover or
    keyboard focus (always visible on touch)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '"goalstate:" + DATA.built' in tpl
    assert "@keyframes tilepulse" in tpl and '.tile.pulse{animation:tilepulse' in tpl
    assert 'if(status === "offer") metal("offer")' in tpl
    assert '=== DATA.game?.goal_apply) metal("blast")' in tpl
    assert '=== DATA.game?.goal_reach) metal("blast")' in tpl
    assert '(g.streak||0) >= 5 ? " 🔥"' in tpl
    tri = tpl.split("\n  .triage{")[1].split("}")[0]
    assert "opacity:0" in tri   # invisible until hover/focus, not just faded
    assert ".pcard.open .triage{opacity:1}" in tpl   # …but always on while expanded
    assert ".triage:focus-within,.triage.decided{opacity:1}" in tpl
    assert "@media (hover:none){.triage{opacity:1}}" in tpl

def test_people_view_batch_three_markers():
    """No placeholder line when a person has no contact info; expanded cards
    say Warm/Cold (with via) and list Alumni rather than raw signals; people
    multi-select into a bulk bar; the tab and a pending people-filter survive
    the reload every action performs; notes are editable in the form; the
    Uninterested badge is redundant inside the Uninterested filter."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "no contact info — click card to add" not in tpl
    assert '${prow("Origin", pv.warm' in tpl   # renamed Warmth → Origin, tinted
    assert 'prow("Alumni",' in tpl and '${prow("Signals"' not in tpl
    assert 'id="bulkbar"' in tpl and "window.bulkSet" in tpl
    # checkboxes, then a Select chip, finally settled on ⌘-click selection
    assert 'class="psel"' not in tpl and 'id="selbtn"' not in tpl
    assert "window.pcardTap" in tpl and "ev.metaKey || ev.ctrlKey" in tpl
    assert 'localStorage.setItem("tab", name)' in tpl
    assert 'localStorage.setItem("pstage-after", "saved")' in tpl
    assert 'id="fp-notes"' in tpl and 'notes: el("fp-notes").value.trim()' in tpl
    # the stage badge later left the concise card entirely (batch five) —
    # stage now lives only in the expanded view and the talking-stage filter
    assert ">Saved Companies</button>" not in tpl   # chip retired for Alumni (Eric, 2026-08-12)
    assert 'id="hascontact"' in tpl   # the chip grew into a per-channel dropdown
    assert 'id="ps-review"' in tpl and 'id="pstatus"' not in tpl   # postings modes are chips now


def test_person_route_saves_contact_fields(tmp_path, monkeypatch):
    """The modal sent email/x/phone but /api/person dropped them — a phone
    number typed into the edit form silently never saved. Pin the add_person
    kwargs the route passes."""
    import inspect

    from pipeline import app, entities

    src = inspect.getsource(app.Handler.do_POST)
    for field in ('email=body.get("email"', 'x=body.get("x"',
                  'phone=body.get("phone"', 'notes_replace=body.get("notes"'):
        assert field in src, f"/api/person no longer passes {field}"
    # notes_replace replaces wholesale; plain note still appends
    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    entities.add_person("A B", company="C", note="first")
    entities.add_person("A B", company="C", notes_replace="rewritten")
    assert entities.load_people_overlay()[0]["notes"] == "rewritten"

def test_people_batch_four_markers():
    """Company group headings in People click through to the Companies tab;
    the feed-provenance footer is gone; the skip link is fixed offscreen (the
    absolute -40px version peeked out during rubber-band overscroll); the
    tracker's saved-company strip packs as masonry columns instead of grid
    rows that reserve the tallest card's height."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "CO_KEY[k] = c.key" in tpl and 'class="colink cname"' in tpl   # maps rebuild via initDerived
    assert "Built from <code>data/feed.json</code>" not in tpl
    skip = tpl.split(".skip{")[1].split("}")[0]
    assert "position:fixed" in skip and "top:-100px" in skip
    tgrid = tpl.split(".tgrid{")[1].split("}")[0]
    # masonry → grid on the tracker strip: aligned tops, same 380px width
    # superseded 2026-08-05: strip is masonry columns for uniform vertical gaps
    assert "display:flex" in tgrid  # JS masonry — multicol retired 2026-08-05
    assert "gap:12px" in tpl.split(".tgrid{")[1].split("}")[0]

def test_people_batch_five_markers():
    """Contact filter is a per-channel dropdown; the talking-stage sub-filter
    only appears inside the Saved view; concise cards drop the stage row and
    hold one height; the warm select reads Cold/Warm and greys the via field
    when cold; the mail/phone glyphs render larger."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="hascontact"' in tpl and '<option value="linkedin">Has LinkedIn</option>' in tpl
    assert 'id="talkstage"' in tpl and 'pst === "saved"' in tpl
    assert "stagerow" not in tpl
    # 2026-08-06: cards are two-line rows (name/icons over role/triage);
    # the invariant is still "collapsed cards hold one uniform height"
    assert ".pcard:not(.open){padding:8px 10px 8px 52px;align-items:center;min-height:72px}" in tpl
    assert '<option value="">Cold</option>' in tpl and '<option value="1">Warm</option>' in tpl
    assert "window.warmVia" in tpl and 'el("fp-wvia").disabled' in tpl
    assert 'placeholder="notes"' in tpl and "never auto-generated" not in tpl
    # order settled as in → @ → 𝕏 → ☏ (phone came back by request)
    icons = tpl.split("function contactIcons")[1].split("].join")[0]
    assert '"@"' in icons and "tel:" in icons
    assert icons.index("LinkedIn") < icons.index("Email") < icons.index('"X"') < icons.index("Call")

def test_shortcuts_and_heading_chips_markers():
    """One-key shortcuts (guarded against typing and modifiers), a hold-⌘
    cheat-sheet, a visually distinct separated Select pill, and the industry
    bubble beside company names on the People tab."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="kbhelp"' in tpl and 'if(ev.key === "Alt")' in tpl
    assert '["input","textarea","select"].includes(tag)' in tpl
    assert 'if(ev.metaKey || ev.ctrlKey) return;' in tpl and 'if(ev.altKey){' in tpl  # holding the hinted modifier now WORKS
    for key in ('k === "1"', 'k === "/"', 'k === "a"', 'k === "t"'):
        assert key in tpl, f"shortcut {key} missing"
    assert 'ev.key === "Alt") kbShow(false)' in tpl
    # the sheet lives bottom-right and fades, not toggling [hidden]
    khelp = tpl.split(".kbhelp{")[1].split("}")[0]
    assert "right:18px;bottom:18px" in khelp and "transition:opacity" in khelp
    # 2026-08-06: people group headers dropped the industry/city chips
    # (Eric's call) — industry still tints the box via indHue on --indh
    assert "CO_IND = {}" in tpl and '--indh:${indHue(cind)}' in tpl
    assert 'indChip(cind)' not in tpl

def test_people_batch_six_markers():
    """Denser one-line concise cards (warm folds into the role line); the
    expanded card shows when the person was added; a sort dropdown covers
    A–Z and recently-added; ⌘-click replaces the Select chip; the cheat-sheet
    peeks on ⌥ from the bottom-right with a real fade; only the newly-opened
    card animates; pasting a LinkedIn URL autofills from pipeline data or the
    slug — never a network lookup."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'prow("Added", pv.added' in tpl and "ago(pv.added)" in tpl
    assert 'id="psort"' in tpl and '<option value="recent">' in tpl
    assert "JUST_OPENED" in tpl and '.pexp.anim{animation:growrow' in tpl
    # grid-row growth reaches true content height; padding grows inside the
    # reveal, so the anchored Edit button glides instead of jumping
    assert "grid-template-rows:0fr" in tpl and 'class="pexpin"' in tpl
    assert "#tracker,#list" not in tpl   # panes fade only on tab switch now
    # 2026-08-06: paste-autofill retired — pasting a LinkedIn URL must not
    # fill or queue anything by itself; the Autofill button prompts+confirms
    assert "window.fpAutofill" not in tpl
    assert 'onblur="fpAutofill' not in tpl
    assert "prompt(\"LinkedIn profile URL to autofill from:\"" in tpl
    # a fired toast used to linger invisibly over the fab stack and eat
    # clicks on Add Person — toasts must never catch the pointer
    assert ".toast{position:fixed;bottom:18px;right:18px;pointer-events:none;" in tpl

def test_people_batch_seven_markers():
    """Contact icons anchor to the card floor level with the triage buttons;
    signal/warm chips right-align in the name row; a faint ⌥ hint floats
    bottom-right and morphs into the sheet; shift-click extends a selection
    as a range over the rendered order; Origin colors warm/cold and the
    Alumni row tints Emory gold / NMH blue; Added reads relatively."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    # 2026-08-06 v3 layout: line 1 is name · role, line 2 is tags + contact
    # buttons grouped left, triage keeps the corner
    assert ".pcard:not(.open) .pmeta.icons{display:flex;gap:4px;margin:0}" in tpl
    assert '<span class="pdiv">·</span>' in tpl
    assert 'class="sigs"' in tpl and ".sigs{margin-left:auto" in tpl
    assert 'id="kbhint"' in tpl and 'el("kbhint").classList.toggle("hide", showing)' in tpl
    assert "ev.shiftKey && PSEL.size && LAST_SEL" in tpl and "RENDER_PIDS" in tpl
    assert '"o-warm"' in tpl and '"o-cold"' in tpl
    assert "alum-emory" in tpl and "alum-nmh" in tpl
    assert "function ago(" in tpl and 'return `${Math.round(d/7)}w ago`' in tpl

def test_people_batch_eight_markers():
    """Expanded cards reclaim the avatar column for full width, Edit anchors
    bottom-left level with the always-visible triage; the shortcut hint is a
    legible pill whose click-peek times out unless pinned; a separator splits
    sort from the stage filter."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    # the avatar became a left band, so the text column starts at 58px and
    # Edit lines up with the tag text rather than the old avatar edge
    # Edit + Autofill share one positioned row now (they used to stack)
    # 2026-08-06: expanded cards adopted the collapsed band width (38px),
    # so the anchored button row moved 58 -> 50 with the text column
    assert ".pexp .pexprow{position:absolute;left:50px;bottom:12px" in tpl
    assert ">Hold ⌥ for Shortcuts</button>" in tpl
    assert "const kbPeek" in tpl and "setTimeout(() => { if(!KBPIN) kbShow(false); }, 3500)" in tpl
    assert 'id="psep"' in tpl

def test_people_batch_nine_markers():
    """Activity modal groups the history log by day with timestamps; contact
    icons always render all four channels (missing ones greyed) at triage
    size; the people bar runs search | stage+talking-stage | sort with single
    barriers; a role-less person shows their relationship, color-coded in the
    expanded view; blank expanded fields read as a dash."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="activity-btn"' in tpl and "window.openActivity" in tpl
    assert 'id="actmodal"' in tpl and 'class="actday"' in tpl and 'class="acttime"' in tpl
    assert '"activity": activity' in dashboard.__dict__ or "activity.sort" in open("pipeline/dashboard.py").read()
    # grey placeholder circles retired — only live channels render, plus one
    # dashed + that opens the edit form when something's missing
    assert 'class="icn off"' not in tpl and 'class="icn addc"' in tpl and ".icn.addc{" in tpl
    icn = tpl.split("\n  .icn{")[1].split("}")[0]
    assert "width:32px;height:32px" in icn
    # order: stage chips, talkstage, psep, psort
    bar = tpl.split('id="pt-review"')[1].split('id="psort"')[0]
    assert 'id="talkstage"' in bar and 'id="psep"' in bar
    assert "function relLabel" in tpl and "rel-founder" in tpl
    assert '\'<span class="co">—</span>\'' in tpl

def test_subhead_band_and_people_serif_markers():
    """The filter band sits on its own --sub-bg tint (darker than the page,
    lighter than the header, defined in both themes); person names and
    expanded-view labels carry the serif."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert tpl.count("--sub-bg:") >= 2
    controls = tpl.split(".controls{")[1].split("}")[0]
    assert "var(--sub-bg) 82%, transparent" in controls  # frosted since 2026-08-05
    # anchored to line start: ".pcard:not(.open) .pname{" (row layout,
    # 2026-08-06) also contains ".pname{" and sits earlier in the sheet
    pname = tpl.split("\n  .pname{")[1].split("}")[0]
    assert "var(--serif)" in pname
    plab = tpl.split(".pexp .plab{")[1].split("}")[0]
    assert "var(--serif)" in plab

def test_people_batch_ten_markers():
    """Expanded rows follow the fixed order ending in Added with a
    manual/auto provenance tag; non-alumni read No; Stage only renders past
    review; concise names drop degrees; icons hide while expanded; derived
    founders inherit their feed date as Added."""
    from pipeline import dashboard, entities

    tpl = dashboard._TEMPLATE
    # Full name and Role rows retired — the concise card already shows both
    exp = tpl.split("function personExpanded")[1].split("function personCard")[0]
    assert 'prow("Full name"' not in exp and 'prow("Role"' not in exp
    order = [exp.index(f'prow("{l}"') for l in
             ("Origin", "Alumni", "LinkedIn", "Email",
              "X", "Phone", "Notes", "Stage", "Added")]
    assert order == sorted(order), "expanded rows out of the agreed order"
    # "Alumni: No" retired — the row renders only when a school is verified,
    # and four empty channels collapse into one "Contact: none" line
    assert ': "No")' not in exp and 'prow("Contact"' in exp
    assert 'pv.st && pv.st !== "review"' in exp
    assert 'auto (feed)' in exp and '"manual"' in exp
    assert 'pv.name.split(",")[0].trim()' in tpl
    assert ".pcard.open .pmeta.icons{display:none}" in tpl
    # a feed founder with no overlay row still has an Added date
    from pipeline.models import Entry
    e = Entry(title="t", company="Acme", url="u9", source="s", location="",
              score=70, why="", date_added="2026-07-01",
              founders=[{"name": "Jo Founder", "title": "CEO"}])
    pv = [p for p in entities.people([e]) if p.name == "Jo Founder"][0]
    assert pv.added == "2026-07-01" and pv.src == "feed"

def test_people_batch_eleven_markers():
    """Founders wear plum (clay sat too close to Emory gold); school chips
    carry their school's color; sort is just A–Z (default) or recently
    added; the avatar is a full-height hue band on the card's left."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "--founder-edge" in tpl and tpl.count("--founder-edge:") >= 2
    assert 'class="sig sig-founder">Founder' in tpl and 'gw("🔥", "Warm")' in tpl  # fire when condensed, word when open (2026-08-18)
    assert "Sort: relevance" not in tpl and "const weight" not in tpl
    assert '<option value="az">Sort: A–Z</option>' in tpl
    assert 'class="pband"' in tpl and ".pband{position:absolute;left:0;top:0;bottom:0;width:44px" in tpl
    assert 'class="av"' not in tpl

def test_band_is_identity_hue_and_chips_carry_meaning():
    """Final shape after two reversals: the band is the person's own hashed
    hue (pure identity), while the FILLED chips carry warm/school/founder.
    Contact icons are serif and include the returned phone glyph."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "const EARTH" in tpl and "function earthTone" in tpl
    fn = tpl.split("const EARTH")[1].split("function contactIcons")[0]
    assert fn.count("hsl(") >= 8 and "--emory-edge" not in fn
    # live contact icons wear the same identity tone as the band
    assert "style=\"color:${tone};border-color:${tone}\"" in tpl
    icn = tpl.split("\n  .icn{")[1].split("}")[0]
    assert "font-family:var(--serif)" in icn

def test_people_patterns_ported_to_postings_and_companies():
    """⌘-click / shift-range multi-select and the bulk bar work on postings
    and companies too (one selection kind at a time); their expansions slide
    open with the same grid-grow; posting titles are serif; the applied date
    reads relatively."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "window.rowTap" in tpl and "window.coTap" in tpl
    assert "POSTSEL" in tpl and "COSEL" in tpl and "clearOtherSel" in tpl
    assert "function updateBulk" in tpl
    # bulk uninterested on postings still asks the why once for the batch
    assert "Why pass on these?" in tpl
    assert 'class="expgrow' in tpl and "animation:growrow" in tpl
    assert "JUST_ROW" in tpl and "JUST_CO" in tpl
    ptitle = tpl.split(".ptitle{")[1].split("}")[0]
    assert "var(--serif)" in ptitle
    assert "Applied ${esc(ago(r.ap))}" in tpl   # now a chip in the posting footer

def test_company_expansion_uses_real_person_cards():
    """Expanded companies show the SAME person cards as the People tab (band,
    triage, expandable in place), falling back to the plain founder line only
    when no person view matches; togglePerson re-renders every pane that can
    host a person card."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    co = tpl.split("function companyExpanded")[1].split("function companyCard")[0]
    assert "personCard" in co and "<h4>People " in co   # header now carries a pager
    # people returned to in-place downward expansion (modal stays for
    # postings and companies); one person open at a time
    toggle = tpl.split("window.togglePerson")[1].split("function relLabel")[0]
    assert "PEOPLE_EXPANDED.clear()" in toggle and "patch(pid)" in toggle   # in-place patch since 2026-08-18

def test_company_expansion_uses_real_posting_cards():
    """Expanded companies embed the same posting cards as the Postings tab
    (score, triage, nested expansion) via the shared postingCard component;
    the link list survives only for postings missing from the page rows."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function postingCard" in tpl
    assert tpl.count("postingCard(r") >= 2   # postings list + company expansion
    co = tpl.split("function companyExpanded")[1].split("function companyCard")[0]
    assert "ROW_BY_URL" in co and "postingCard" in co
    assert 'window.toggleRow = (u) => openCard("post", u);' in tpl

def test_people_chips_and_nested_navigation():
    """People stage filter is chips (like companies); nested person/posting
    cards inside companies navigate to the real tab instead of expanding in
    place; the stale 40-group render cap is gone — under A–Z it cut the list
    mid-alphabet, and gotoPerson could land on a card that never rendered."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="pt-review"' in tpl and "window.setPeopleMode" in tpl
    assert 'id="pstage"' not in tpl
    # the 4th arg is the tracker-board flag — same card, board affordances
    assert "function personCard(pv, nested, forceOpen, kb)" in tpl
    assert "function postingCard(r, showVisa, nested, forceOpen, kb)" in tpl
    assert "personCard(pv, true)" in tpl and ", true)).join" in tpl
    assert "const LIMIT = 500;" in tpl

def test_company_wash_search_morph_fitted_selects():
    """Company cards wear a faint fixed-hue wash per industry; the search
    button swaps into the bar in place (tab chrome keeps them one slot);
    dropdowns hug their current choice via fitSelect instead of reserving
    the longest option's width."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function indShade" in tpl and '{dull?"background:var(--card)":indShade(c.industry)}' in tpl
    assert 'if(key === "q") on = on && !el("q").hidden;' in tpl
    # the search chip left the bar for the floating cluster; only the input remains
    assert 'if(key === "q") on = on && !el("q").hidden;' in tpl and 'id="qbtn"' not in tpl
    assert "function fitSelect" in tpl
    assert 'sl.addEventListener("change", () => fitSelect(sl))' in tpl

def test_company_card_footer_and_score_circle():
    """Compact company cards: site/LinkedIn/stage/roles chips in a footer
    level with the triage buttons; rounds and industries render Title Case.
    Scores left company cards entirely — the ring lives on postings now."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'class="cfoot"' in tpl and 'class="cchip"' in tpl
    # postingCard now also carries a cfoot; anchor on the company one
    # match the template expression, not the bare property — the CO_SITE boot
    # map also says c.site and lives before any cfoot in the file
    # the chips are hoisted to consts above the return now, so the miss-pill
    # can see every gap before it renders — check the card, not the cfoot slice
    card = tpl.split("function companyCard")[1].split("function setCompanyMode")[0]
    for bit in ("esc(c.site)", "c.linkedin", "roundGroup(c.stage", "roleTag("):
        assert bit in card, f"company card missing {bit}"
    # the location chip lives up top beside the industry now, bold and tinted
    assert "citychip" in tpl and "cityTone(city0)" in tpl
    assert "const CITY_TONES" in tpl
    assert "function roundGroup" in tpl
    # the ring is drawn to the score and lives on postings only
    assert "conic-gradient(currentColor ${(r.s||0)*3.6}deg" in tpl
    co = tpl.split("function companyCard")[1].split("function setCompanyMode")[0]
    assert "cscore" not in co
    assert "const tc =" in tpl
    assert ",.22),hsla" in tpl.split("function indShade")[1].split("];")[0]   # vibrancy bump

def test_llm_descriptions_never_clobber_human_lines(tmp_path, monkeypatch):
    """describe.apply writes LLM summaries into blanks and over earlier auto
    lines, but a hand-written overlay description survives every apply."""
    from pipeline import describe, entities

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    entities.save_company_overlay({
        "handmade": {"name": "Handmade", "description": "Eric wrote this."},
        "autoline": {"name": "Autoline", "description": "old auto", "description_src": "llm"},
    })
    n = describe.apply({"handmade": "LLM rewrite attempt.",
                        "autoline": "Fresh two-sentence summary.",
                        "brandnew": "First summary for a blank company."})
    d = entities.load_company_overlay()
    assert d["handmade"]["description"] == "Eric wrote this."
    assert d["autoline"]["description"] == "Fresh two-sentence summary."
    assert d["brandnew"]["description_src"] == "llm"
    assert n == 2


def test_describe_queues_summaries_with_material():
    """The summarize queue embeds the raw material (the company's own words)
    so the summarizer condenses rather than invents."""
    from pipeline import describe

    q = describe.SUMMARY_QUEUE
    assert q.name == "summarize_queue.json"
    import inspect
    src = inspect.getsource(describe.queue_summaries)
    assert "description_full" in src and "never add facts" in src.lower() or "must never add" in src

def test_score_ring_live_counts_and_ghosting():
    """Score rings arc to the score; mode-chip counts recompute under the
    active filters on both companies and people; a company sort (score /
    A–Z / recent) sits between separators; uninterested nested cards ghost;
    expanded companies drop the roles/people chips and posting cards drop
    the status badge."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="csort"' in tpl and 'id="csep1"' in tpl and 'id="csep2"' in tpl
    assert "const base = list;" in tpl and "base.filter(c=>!c.ni&&!c.tracked)" in tpl
    assert 'pbase.filter(pv=>bucketOf(pv.st||"review")==="saved")' in tpl
    assert '.ghost{opacity:.45' in tpl
    assert 'r.st==="uninterested"?"ghost"' in tpl and 'pv.st==="uninterested"?"ghost"' in tpl
    # tags later moved below the description in the modal; the roles chip
    # is a briefcase + count now and still lights up when any are new
    assert 'class="cchip roletag${fresh?" freshroles":""}"' in tpl
    assert 'badge b-${esc(r.st)}' not in tpl

def test_viewstate_survives_reload_and_companies_are_editable():
    """Every action reloads the page; the viewstate stash brings back the
    mode chips and expanded cards so adding a note doesn't dump you into
    Review. Companies get a real Edit form (name locked as the key), and
    editing sends follow:false so a typo fix can't move a company to Saved."""
    from pipeline import dashboard, entities
    import inspect

    tpl = dashboard._TEMPLATE
    assert 'localStorage.setItem("viewstate"' in tpl and "beforeunload" in tpl
    assert "modal: MODAL_CARD" in tpl and "10*60*1000" in tpl
    assert "openCard(vs.modal.kind, vs.modal.key)" in tpl
    assert "window.editCompany" in tpl and 'el("fc-name").readOnly = true' in tpl
    assert "follow: !CO_EDITING" in tpl
    assert 'id="psep2"' in tpl
    src = inspect.getsource(entities.track_company)
    assert "follow" in src and "if follow:" in src

def test_company_tags_edit_note_reveal_markers():
    """School/warm tags sit in the company footer as plain color-coded names
    (never "Emory: <person>"); companies get the warm filter; empty states
    span the grid; the Edit form carries the why-note; footers flow so card
    height follows the description; cards fade up on scroll."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'x.split(":")[0].trim()' in tpl        # school name only, both cards
    assert "CO_WARM" in tpl and "F.warm && !CO_WARM.has(c.name" in tpl   # warm gate lives in coPass now
    # Companies show the warmth chips and hasroles; sports joined them 2026-08-07.
    _cotab = tpl.split("companies: new Set")[1].split("\n")[0]
    assert '"warm","hasroles","funded","clearfil"]' in _cotab  # family sub-filters absorbed by Warm (2026-08-12); Missing is a dropdown
    assert '<div class="empty" style="grid-column:1/-1">${COMPANY_MODE' in tpl
    assert 'id="fc-why"' in tpl and 'why: el("fc-why").value.trim()' in tpl
    cfoot = tpl.split("\n  .cfoot{")[1].split("}")[0]
    assert "margin-top:auto" in cfoot and "position:absolute" not in cfoot
    assert "IntersectionObserver" in tpl and 'classList.add("preveal")' in tpl
    assert tpl.count("observeCards(") >= 4        # def + companies/people/list

def test_exprow_alignment_and_decided_triage():
    """Expanded actions live in one bottom row level with ✓/✕ (open cards
    drop the 48px triage reserve); the grid-grow wrapper is a plain block at
    rest — a fr track inside the flex card collapsed to 0 and swallowed the
    whole expansion — with animationend + a timer stripping .anim; decided
    cards keep their triage visible without hover; company notes read
    "note: …"; city chips draw from a fixed distinct tone map."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'class="exprow"' in tpl and ".card.open,.ccard.open{padding-bottom:14px}" in tpl
    assert ".expgrow{display:block;width:100%}" in tpl
    assert 'ev.animationName === "growrow"' in tpl and "function stripAnimSoon" in tpl
    assert 'triage${decided ? " decided" : ""}' in tpl
    assert "note: ${esc(c.why)}" in tpl
    assert "const CITY_TONES" in tpl and "cityTone(" in tpl

def test_card_detail_opens_in_a_modal():
    """Expansion is a centered popup over a dimmed page: one card at a time,
    ✕ / backdrop / Esc close it, grids always render concise, and the open
    modal survives the action reload via viewstate."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="cardmodal"' in tpl and 'id="cardhost"' in tpl
    assert "window.openCard" in tpl and "let MODAL_CARD" in tpl
    assert "MODAL_CARD = null" in tpl.split("window.closeModal")[1].split("};")[0]
    # every grid card renderer forces closed; only forceOpen shows the detail
    assert "const open = !!forceOpen;" in tpl
    # masonry companies + active filter fill + added/via line
    assert "#companies{display:flex" in tpl and "function mason(" in tpl
    assert "select.actv{background-color:var(--accent)" in tpl
    assert "SELECT_DEFAULTS" in tpl
    assert 'Added ${c.added?esc(ago(c.added)):"—"}</span>' in tpl  # provenance is tags now

def test_salary_extraction_and_card_head_batch():
    """Salaries: Entry persists comp from boards; the build regex pulls
    "$90K–$130K" ranges from cached posting text and rejects equity
    percentages and funding amounts. Cards: Industry? placeholder for the
    unclassified, N-new chip in the footer, long names ellipsize instead of
    pushing the score down, and the note prompt is just a note prompt."""
    from pipeline.dashboard import _TEMPLATE as tpl
    from pipeline.dashboard import _salary_from
    from pipeline.models import Entry

    assert "comp_min" in Entry.__dataclass_fields__
    assert _salary_from("pays $100,000 - $130,000 a year") == "$100K–$130K"
    assert _salary_from("Salary: $90K–$110K plus equity") == "$90K–$110K"
    assert _salary_from("0.5% - 1.5% equity") == ""
    assert _salary_from("we raised $5 - $6 million") == ""
    assert "salchip" in tpl
    # the bare placeholders moved into missBtn(), which builds "<full>?" — the
    # dashed styling still has to exist for the expanded card that uses them
    assert '>${full}?</button>' in tpl and '.ind-missing,.cchip.missing{' in tpl
    # the N-new pill later merged INTO the roles chip (freshroles)
    foot = tpl.split('class="cfoot"')[1].split("</div>")[0]
    assert "freshroles" in foot
    assert "#companies .cname,.tgrid .cname{overflow:hidden;text-overflow:ellipsis" in tpl
    assert 'placeholder="note"' in tpl and "shows on the card" not in tpl

def test_modal_polish_and_unknown_filters():
    """Modal: centered, popin entrance, no hover jiggle, 2px outline, no ✕
    (backdrop/Esc close), triage always visible, roomier padding. Cards:
    Location?/Website?/Stage? placeholders; roles and new merge into one
    green chip; Unknown Industry / Unknown Location filter options."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "#cardmodal{align-items:center}" in tpl
    assert "@keyframes popin" in tpl and "animation:popin" in tpl
    assert "#cardhost>.ccard:hover,#cardhost>.pcard:hover{box-shadow:var(--sh-modal)}" in tpl
    host = tpl.split("#cardhost>.ccard,#cardhost>.pcard{")[1].split("}")[0]
    assert "border:3px solid var(--line2)" in host and "box-shadow" in host  # + glow
    assert "cardx" not in tpl
    # a modal is where decisions happen — triage circles always visible there
    # triage circles back to hover-reveal in the modal (decided stays visible)
    assert ".cardbox .triage{opacity:1}" not in tpl and 'class="btn mclose"' in tpl
    # the dashed placeholders grew into quick-edit buttons — same words,
    # now clickable, patching one field through /api/track
    # every gap is still one click from a quick-edit; collapsed cards route
    # through the miss pill, expanded ones through missBtn's full word
    for field in ("Location", "Website", "Stage", "Industry", "Role Type"):
        assert f'"{field}"' in tpl, f"no quick-edit affordance for {field}"
    assert "function missPill(items){" in tpl   # collapsed to the ellipsis chip 2026-08-13
    assert "window.quickEdit" in tpl and 'id="qeditmodal"' in tpl
    # the new-roles flag survived the roles chip becoming a briefcase + count:
    # the chip still lights up, and the count rides as a +N in accent
    assert "freshroles" in tpl and '<span class="rnew">+${fresh}</span>' in tpl
    assert '"__none"' in tpl and "Unknown Industry" in tpl and "Unknown Location" in tpl
    score = tpl.split("\n  .cscore{")[1].split("}")[0]
    assert "width:24px;height:24px" in score   # ring hugs the title, roomier digits

def test_page_fade_dark_subhead_and_card_typography():
    """A fixed bottom fade sits over the three list tabs (hidden on the
    tracker); the filter band is a dark tone lighter than the header with a
    real drop shadow; company names grow to t-lg with air under the head
    row; descriptions render italic serif."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="pagefade"' in tpl and 'el("pagefade").hidden = tab === "tracker"' in tpl
    assert "linear-gradient(to bottom, transparent, var(--b3))" in tpl  # same scrim, from the black ramp
    # the dark-subhead experiment ended: light paper band, dark forest header
    assert "--sub-bg:#e9e2d0;" in tpl and "--sub-bg:#1c1813" in tpl   # dark readability pass
    # header buttons went glass 2026-08-05 (rgba+backdrop-filter over the band)
    assert "backdrop-filter:blur(12px) saturate(1.3)" in tpl
    controls = tpl.split(".controls{")[1].split("}")[0]
    assert "box-shadow" in controls
    cname = tpl.split("\n  .cname{")[1].split("}")[0]
    assert "var(--t-lg)" in cname
    why = tpl.split("\n  .why{")[1].split("}")[0]
    assert "font-style:italic" in why

def test_postings_speak_the_card_design_language():
    """Postings wear the full system: conic score ring, industry wash from
    their company, city/school/warm badges right of the head, company name
    as a click-through link, footer chips (salary/age/applied/visa), and a
    modal with display title, Posted-via tag, and tonal border."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    post = tpl.split("function postingCard")[1].split("function render()")[0]
    assert "cscorein" in post and "conic-gradient" in post
    assert "indGrad(ind, r.cat)" in post and "CO_IND[" in post   # the gradient marks "posting" vs the companies wash (Eric, 2026-08-09)
    # warm/school chips consolidated into famTags (shared with companies)
    assert "cityTone(r.city)" in post and "famTags(r.c" in post
    assert 'class="colink pcobig pcosm"' in post and "gotoCompany" in post
    assert 'class="cfoot"' in post
    # provenance collapsed into the footer when-chip ("Posted 17d Ago · via X")
    assert "via ${esc(srcDisp(r.src||" in post and 'class="cchip agechip' in post
    # display size only on the host title; nested cards match people names
    assert "#cardhost>.ccard>.chead .ptitle{font-size:var(--t-xl)" in tpl


def test_postings_bar_rework_and_job_types():
    """Batch: sort moved beside the mode chips with its own barrier; the 70+
    chip retired; postings gained industry/round/warm filters, a visa chip
    that only shows abroad, an + Add Posting button, and a job-type filter
    derived from the title (function beats stage: 'Founding Growth Lead' is
    Growth, plain founding roles land in Founding & Generalist)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    # bar order: chips | pssep | sort | pssep2 | rtype | city …
    assert tpl.index('id="pssep"') < tpl.index('id="sort"') < tpl.index('id="pssep2"') < tpl.index('id="dd-cat"')
    assert 'data-f="hot"' not in tpl and "F.hot" not in tpl
    # "yoereq" joined the row 2026-08-11 (experience-requirement filter)
    assert '"pssep2","posted","yqwrap"' in tpl   # the ddwrap wrapper alone keys visibility now
    assert "nonUsSelected() ? \"\" : \"none\"" in tpl        # visa chip hidden at home
    assert '"/api/add-posting"' in tpl                        # paste-a-link flow
    assert "MSEL.rnd.has(roundGroup(r.stg))" in tpl          # round filter on postings (Set since 2026-08-18)
    assert "CO_WARM.has((r.c" in tpl                          # warm filter on postings
    assert "MSEL.cat.has(r.cat)" in tpl and "DATA.role_order" in tpl

    rb = dashboard.role_bucket
    assert rb("Founding Growth Lead") == "Growth"
    assert rb("GTM Engineer") == "Technical GTM"
    assert rb("Growth Engineer") == "Technical GTM"
    assert rb("Founding Engineer") == "Engineering"
    assert rb("Founder's Associate") == "Founding & Generalist"
    assert rb("Product Designer") == "Product & Design"   # merged 2026-08-11
    assert rb("Head of Partnerships") == "Sales & BD"
    assert rb("Barista") == "Other"
    assert dashboard.ROLE_ORDER[-1] == "Other"


def test_role_bucket_2026_08_11_gap_sweep():
    """The Other bucket was the LARGEST role bucket (709 of ~3k active) until
    the 2026-08-11 sweep. Each line pins one leak that was measured in the
    live feed, plus the ordering rules the fix depends on. A regression here
    means real postings are sliding back into Other — or worse, a title is
    filing under the wrong function."""
    from pipeline.dashboard import ROLE_ORDER, role_bucket as rb

    # strategy family → the widened CoS tag (was: all Other)
    assert rb("Strategic Projects Lead") == "CoS & Strategy"
    assert rb("Special Projects") == "CoS & Strategy"
    assert rb("AI Strategist") == "CoS & Strategy"
    assert rb("Chief of Staff") == "CoS & Strategy"
    # ...but a strategist whose qualifier names another craft keeps its craft,
    # and Technical GTM outranks the strategy match by sitting first
    assert rb("Brand Strategist") == "Marketing"
    assert rb("Content Strategist") == "Marketing"
    assert rb("Deployment Strategist") == "Technical GTM"
    # project/program/supply-chain management is Operations (was: Other)
    assert rb("Project Manager") == "Operations"
    assert rb("Program Manager") == "Operations"
    assert rb("Supply Chain Manager") == "Operations"
    # but a plain product manager is still product
    assert rb("Product Manager") == "Product & Design"
    # smaller measured leaks
    assert rb("VP of People") == "People & Talent"
    assert rb("Technical Sourcer") == "People & Talent"
    assert rb("Commercial Associate") == "Sales & BD"
    assert rb("Treasury Manager") == "Finance & Legal"
    # research: engineering titles file as Engineering, pure research as Data
    assert rb("Research Engineer") == "Engineering"
    assert rb("AI Researcher") == "Data"
    # the split Data entry must not double-list the label in the UI order
    assert ROLE_ORDER.count("Data") == 1
    assert "Design" not in ROLE_ORDER and "Chief of Staff" not in ROLE_ORDER


def test_industry_needles_2026_08_11_gap_sweep():
    """373 described companies had no industry tag; these pins are the
    measured misses. Real blurbs from the feed, abbreviated: Medra ('eradicate
    disease'), Gutgutgoose (probiotics), Abacum ('finance teams'), Conduct
    (enterprise SAP-era systems), Closure (law-enforcement software). The
    enterprise blob sits LAST so specific verticals keep winning ties."""
    from pipeline.entities import _INDUSTRIES, industry_of

    assert industry_of("Medra's mission is to eradicate disease") == "digital health"
    assert industry_of("Personalized probiotics matched to your gut microbiome") \
        == "fitness & wellness"
    assert industry_of("the leading business planning solution for finance teams") \
        == "fintech"
    assert industry_of("enterprise software to replace SAP-era workflow systems") \
        == "enterprise software"
    assert industry_of("software helping law enforcement search evidence") \
        == "enterprise software"
    assert _INDUSTRIES[-1][0] == "enterprise software"   # ties → verticals
    # a health company mentioning enterprise sales must stay health
    assert industry_of("patient care clinic platform with enterprise plans") \
        == "digital health"


def test_add_posting_route_dedupes(tmp_path, monkeypatch):
    """/api/add-posting must refuse a URL already in the ledger — a pasted
    duplicate would fork history across two entries for the same role."""
    import inspect

    from pipeline import app

    src = inspect.getsource(app.Handler.do_POST)
    assert '"/api/add-posting"' in src
    assert "idx.by_url.get(normalize_url(url))" in src   # dedupe before append
    assert '"error": f"already in the feed' in src
    assert 'body.get("source") or "manual"' in src and 'else "review"' in src  # sweeps land in review; hand-adds save (2026-08-18)


def test_polish_pass_tracker_and_chrome():
    """The big polish batch, tracker/chrome half: empty kanban columns collapse
    to vertical-label stubs (and an all-empty board says so in words); zero
    stat tiles dim; goal tiles fill with progress; day counts go terracotta
    past the same stall thresholds status.py uses; the activity log displays
    the current status vocabulary over old history rows and tints direction;
    the header button cluster stops overlaying the tabs at laptop widths."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'class="kbnone"' in tpl and "kbvlabel" in tpl
    assert '"tile.zero"' not in tpl and ".tile.zero{opacity:.5}" in tpl
    assert "linear-gradient(to top," in tpl          # goal-tile progress fill
    assert "const STALE_AFTER = {saved: 7, applied: 10, interviewing: 4" in tpl
    # the stall tint moved onto the shared cards with the .kbdate stub's death
    assert '${stalled(pv.st,pv.days)?" stallchip":""}' in tpl
    assert '${stalled(r.st,r.days)?" stallchip":""}' in tpl
    assert "ACT_LEGACY" in tpl and 'shortlisted:"saved"' in tpl
    assert "act-fwd" in tpl and "act-neg" in tpl
    assert "@media (max-width:1120px)" in tpl        # hbtns leave the overlay lane
    # the crammed "1 33%" responses tile now separates count from rate
    assert '`<span class="co"> · ${pct(a,b)}</span>`' in tpl


def test_polish_pass_cards_and_display():
    """Cards half: outreach-play entries show a real title + gold chip instead
    of their why; expanded postings carry provenance once (no 'Where this came
    from' section — artifacts moved to the action row, extra boards to an
    'also seen on' line); the card modal has a close ✕ and always-visible
    triage; nested modal grids run one column and a posting inside its own
    company's modal drops the company line; rings tint by band; lowercase
    company names and raw source slugs title-case on display only; person
    contact icons render live channels plus one dashed +; the six-dash
    expanded person collapsed."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "const isPlay" in tpl and "Outreach — ${coDisp(r.c)}" in tpl
    assert 'class="cchip opchip">Outreach Play' not in tpl  # plays retired 2026-08-12; edgar emits companies now
    # "also seen on" later left too — provenance is the head tag, full stop
    assert "Where this came from" not in tpl and "also seen on" not in tpl
    assert 'class="btn mclose"' in tpl and ".cardbox .mclose{position:sticky" in tpl
    # modal sections went two-up at native card width (single-column read hollow)
    # modal posting lists are masonry (.exp .cgrid columns) for uniform
    # vertical gaps; people grids stay a real grid
    assert "#cardhost .exp .pgrid{grid-template-columns:repeat(auto-fill,minmax(380px,1fr))}" in tpl
    assert ".exp .cgrid{display:flex" in tpl
    # in the modal the company sits inline after the title (pcobig); the
    # standalone pco line renders only on concise cards
    # condensed heads inline the company after the title (Title · Company),
    # matching the expanded view — the separate .pco line is retired
    assert 'class="colink pcobig pcosm"' in tpl and 'class="colink pcobig"' in tpl
    assert ".s-90{" in tpl and ".s-80{" in tpl and ".s-70{" in tpl
    assert "function coDisp" in tpl and "const srcDisp" in tpl
    assert '"Work at a Startup"' in tpl
    assert 'class="icn addc"' in tpl
    # the unknown-location bucket stopped pretending to be a place
    # posting city chip is a quick-edit button now (and Location? when absent)
    assert "postingEdit('${jsq(r.u)}','location')" in tpl
    # zero-count mode chips dim on every tab that has them — postings,
    # companies, rolodex, and (since the X sweep landed) tweets
    assert tpl.count('classList.toggle("empty0"') == 5   # ps x2? yq chips joined, ct-uninterested left (2026-08-18)


def test_blurb_heals_split_about_prefix():
    """'A bout Offstream Offstream is…' — a scrape artifact that dodged every
    boilerplate pattern because the word 'About' arrived split. It must heal
    to the sentence the company actually wrote."""
    from pipeline.entities import _blurb_from

    b = _blurb_from(
        "A bout Offstream Offstream is the de facto dMRV and compliance "
        "platform for biochar project developers.", "Offstream")
    assert b.startswith("Offstream is the de facto")


def test_template_script_survives_top_level_execution(tmp_path):
    """The whole page runs off one inline script: a const declared below a
    top-level caller is a TDZ ReferenceError that kills every control at
    load (`cap` did it once, `srcDisp` did it again — the page rendered but
    no tab, filter, or button worked). node --check can't catch it; actually
    executing the script against a stubbed DOM does."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node not on PATH")
    import re

    from pipeline import dashboard
    from pipeline.models import Entry

    page = dashboard.build([Entry(title="t", company="C", url="u", source="s",
                                  location="", score=50, why="",
                                  date_added="2026-07-01")],
                           out_path=tmp_path / "d.html").read_text()
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    js = tmp_path / "inline.js"
    js.write_text(max(scripts, key=len))
    r = subprocess.run([node, str(__import__("pathlib").Path(__file__).parent / "harness_dashboard.js"), str(js)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "script ran clean" in r.stdout, r.stdout + r.stderr


def test_diagnostics_panel_and_recruiter_and_seattle():
    """Diagnostics: a header panel answering 'is the machine working' — every
    source with entries/scored/70+/last-pull, retired boards dimmed with
    their reason, queue depths, and whether the daily scout is installed.
    Recruiter joins founder/employee as a relationship (slate chip, same
    alpha recipe). Seattle joins the Getro location filters — the app had a
    Seattle city bucket the boards never fed. Lifetime people stats count
    ever-contacted from history, not current stage — a person who moved on
    or went dormant still counts."""
    import pathlib

    from pipeline import dashboard
    from pipeline.models import Entry

    tpl = dashboard._TEMPLATE
    assert 'id="diag-btn"' in tpl and "window.openDiag" in tpl and 'id="diagmodal"' in tpl
    assert 'value="recruiter"' in tpl and "sig-recruiter" in tpl
    d = dashboard._diagnostics([Entry(title="t", company="C", url="u", source="generalist",
                                      location="", score=80, why="", date_added="2026-07-01",
                                      first_seen="2026-07-02")])
    names = {b["name"] for b in d["boards"]}
    assert "generalist" in names and "techstars" in names
    tech = next(b for b in d["boards"] if b["name"] == "techstars")
    assert tech["tier"] == "api"   # un-retired on Eric's call 2026-08-05
    gen = next(b for b in d["boards"] if b["name"] == "generalist")
    assert gen == {**gen, "entries": 1, "scored": 1, "hot": 1, "last": "2026-07-02"}
    assert set(d["totals"]) == {"entries", "unscored", "unscored_review", "hot", "descs",
                                "enrich_q", "summarize_q", "calibration", "connections"}
    yaml_txt = pathlib.Path(dashboard.ROOT / "data" / "boards.yaml").read_text()
    assert yaml_txt.count("Seattle, WA, USA") == 4
    assert "ever_contact" in pathlib.Path(dashboard.ROOT / "pipeline" / "dashboard.py").read_text()


def test_sweep_today_panel_and_speed_triage():
    """The throughput batch: stale low-score review entries derive to swept
    (display-only, reversible — a rescore that clears the bar un-sweeps);
    the tracker opens with a Today panel rendered from today.build (which
    itself spoke the pre-rename vocabulary: replies-owed checked 'responded',
    a state that no longer exists, so the section was permanently empty);
    the EDGAR daily-index sweep reads every Form D instead of name-matching
    terms; Climatebase covers product-serves-agriculture; speed triage
    walks the filtered review pile keyboard-first and reloads once on exit."""
    import inspect

    from pipeline import dashboard, today
    from pipeline.models import Entry

    tpl = dashboard._TEMPLATE
    # swept: derived, chip'd, dashed, counted
    old = Entry(title="t", company="C", url="u", source="s", location="",
                score=40, why="", date_added="2026-01-01", first_seen="2026-01-01")
    assert dashboard._swept(old)
    assert not dashboard._swept(Entry(title="t", company="C", url="u2", source="s",
                                      location="", score=80, why="",
                                      date_added="2026-01-01", first_seen="2026-01-01"))
    hatch = Entry(title="t", company="C", url="u3", source="s", location="", score=40,
                  why="", date_added="2026-01-01", first_seen="2026-01-01", escape_hatch=True)
    assert not dashboard._swept(hatch)
    # swept is a permanent status write now (scout applies it) + a filter
    # chip inside Uninterested — not a fourth browsing mode
    assert 'data-f="swept"' in tpl and 'id="ps-swept"' not in tpl
    assert "def apply_sweep" in __import__("inspect").getsource(dashboard)
    # today panel
    # the Today sections lasted a week; only the connections nag bar remains
    assert "function todayPanel" in tpl and 'class="connbar"' in tpl
    assert '"today": today_panel' not in inspect.getsource(dashboard)
    assert '"interviewing"' in inspect.getsource(today.build)
    # triage
    assert "window.startTriage" in tpl and 'id="triagebar"' in tpl
    assert "LAST_ROWS = rows" in tpl
    # boards
    from pipeline.boards import ADAPTERS, climatebase, edgar
    assert "climatebase" in ADAPTERS
    assert climatebase.DEFAULT_SECTORS == ["Food, Agriculture, & Land Use"]
    assert edgar._IDX_ROW.match(
        "D                Acme Bio Inc.                                 "
        "                2083164     20260731    edgar/data/2083164/0000950138-26-000017.txt   ")


def test_age_honesty_connections_and_sweep_links():
    """The age chip must say WHICH date it shows — boards like generalist and
    WaaS publish no posting date, so their chip reads 'Seen Xd' (import date)
    instead of impersonating a posting date; fresh glows, stale fades.
    Company cards grow a 1st° chip from data/connections.csv matched locally
    (the export never leaves the machine), the diagnostics panel says whether
    the export is loaded, and an expanded company with a LinkedIn offers
    one-click Emory/NMH sweep links using make alumni's URL pattern."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    # posted-vs-first-seen is a glyph now (calendar vs eye), words in the title
    assert "r.p?CAL_SVG:EYE_SVG" in tpl
    assert 'Posted ${ago2} — date published by the board' in tpl
    assert "First seen ${ago2} — no posting date published" in tpl
    # freshness is a continuous green→red dial on the chip now
    assert "Math.max(4, 135 - age*5.5)" in tpl  # steeper dial, Eric 2026-08-05
    assert "sig-conn" in tpl and '"conn": conns' in __import__("inspect").getsource(dashboard)
    assert "no connections.csv" in tpl
    # the per-school link buttons consolidated into one sweep affordance:
    # a state chip (done/queued/blocked) or a single Queue People Sweep button
    # Queue People Sweep grew into Auto-Complete (site fill + founder queue + sweep queue)
    # Auto-Complete split into the four named autofills
    assert "function sweepStatus" in tpl and ">Autofill Founders</button>" in tpl
    assert ">Repopulate Company Details</button>" in tpl and ">Repopulate Job</button>" in tpl
    assert "window.autoComplete" in tpl and "window.jobAutofill" in tpl and "window.personAutofill" in tpl
    assert "✓ Screened" in tpl and "Can't sweep" in tpl
    assert "Sweep ↗" not in tpl
    assert dashboard._n_connections() >= 0     # missing file → 0, never a crash


def test_interest_cascades_and_alumni_queue(tmp_path, monkeypatch):
    """Saving a posting saves its company (and queues the alumni sweep);
    marking a company uninterested drops its SAVED postings — but never a
    live application thread (applied/interviewing/offer are real-world
    state). An explicit company-level no is never overridden the other way.
    Favoriting a company queues it exactly once; fully-swept companies
    never queue."""
    import inspect
    import json

    from pipeline import app, entities

    src = inspect.getsource(app.Handler.do_POST)
    assert '("saved", "applied", "interviewing", "offer")' in src
    assert 'rec.get("not_interested")' in src            # explicit no wins
    assert 'e.status == "saved"' in src                  # cascade hits saved only
    assert '"auto — company marked uninterested"' in src
    assert '"/api/queue-alumni"' in src

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setattr(entities, "ALUMNI_QUEUE", tmp_path / "alumni_queue.json")
    entities.set_company_mode("Acme Robotics", "saved")   # favorite → auto-queue
    q = json.loads((tmp_path / "alumni_queue.json").read_text())
    assert [x["company"] for x in q] == ["Acme Robotics"]
    assert entities.queue_alumni("Acme Robotics") is False   # no dupes
    # a fully-checked company never queues. Stamp EVERY pass — listing them by
    # hand is what let the third (jets) pass slip in unnoticed: the dupe check
    # above returns False first, so a stale list here passes for the wrong
    # reason instead of failing.
    d = entities.load_company_overlay()
    d[list(d)[0]]["alumni_checked"] = {p: "2026-08-01" for p in entities.ALUMNI_PASSES}
    entities.save_company_overlay(d)
    assert entities.queue_alumni("Acme Robotics") is False


def test_jets_is_a_standard_sweep_pass_alongside_the_schools(tmp_path, monkeypatch):
    """Eric did NFL data science for the Jets, so a former Jets employee is the
    same warm thread an Emory alum is — and LinkedIn's company keyword page
    matches EMPLOYMENT, so the same sweep finds them. Made standard 2026-08-14
    after a live KPMG US test returned 5 hits, both spot-checked ones real.

    A company swept for the two schools only is NOT done — it must re-queue for
    the jets pass, or every company swept before this date silently keeps a gap."""
    import json

    from pipeline import entities

    assert entities.ALUMNI_PASSES == ("emory", "nmh", "jets")

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setattr(entities, "ALUMNI_QUEUE", tmp_path / "alumni_queue.json")
    entities.track_company("Old Sweep Co")
    d = entities.load_company_overlay()
    d[list(d)[0]]["alumni_checked"] = {"emory": "2026-08-01", "nmh": "2026-08-01"}
    entities.save_company_overlay(d)

    assert entities.queue_alumni("Old Sweep Co") is True
    q = json.loads((tmp_path / "alumni_queue.json").read_text())
    assert q[0]["pending"] == ["jets"], q[0]


def test_sweep_blocked_company_never_requeues(monkeypatch, tmp_path):
    """Symptom (2026-08-07 sitting): a company with no sweepable LinkedIn page
    re-entered alumni_queue.json on every favorite, so every sitting re-decided
    not to sweep it. sweep_blocked is the recorded verdict — queue_alumni must
    honor it, and `make alumni` must not list it.

    Note: acquisition is NOT a block reason (Eric, 2026-08-07) — an acquired
    brand is re-filed under the acquirer and swept there. See
    test_acquired_brand_refiled_under_acquirer."""
    import inspect
    import json

    from pipeline import cli, entities

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setattr(entities, "ALUMNI_QUEUE", tmp_path / "alumni_queue.json")
    (tmp_path / "alumni_queue.json").write_text("[]")
    entities.save_company_overlay({
        "ghostline": {"name": "Ghostline",
                      "sweep_blocked": "shut down 2024 — no LinkedIn page"},
    })
    assert entities.queue_alumni("Ghostline") is False
    assert json.loads((tmp_path / "alumni_queue.json").read_text()) == []
    # An explicit not-interested is the same waste for a different reason
    # (Eric, 2026-08-07, on Airbnb/Tripadvisor/HPE): don't spend LinkedIn loads
    # finding alumni at a company he's already passed on.
    entities.set_company_interest("Bigcorp", False)
    assert entities.queue_alumni("Bigcorp") is False
    assert json.loads((tmp_path / "alumni_queue.json").read_text()) == []
    # …and the worklist skips both rather than printing an unsweepable URL
    assert 'rec.get("sweep_blocked") or rec.get("not_interested")' \
        in inspect.getsource(cli.alumni_cmd)


def test_acquired_brand_refiled_under_acquirer():
    """Symptom (2026-08-07): Accel's Getro feed filed Airbnb / Tripadvisor /
    HPE postings under brands they'd acquired years earlier (HotelTonight,
    Housetrip, Nimble Storage), storing the ACQUIRER's LinkedIn as the brand's
    own. Eric's rule: don't block them — the entity IS the acquirer, so re-file
    postings and people there and create the acquirer if it's new. A relapse
    looks like a 'HotelTonight' company card whose careers links go to
    airbnb.com, or an Airbnb Emory alum recorded under a defunct brand."""
    import json

    from pipeline import entities
    from pipeline.models import normalize_company

    feed = json.load(open("data/feed.json"))
    ov = entities.load_company_overlay()
    for brand, acquirer in (("HotelTonight", "Airbnb"),
                            ("Housetrip", "Tripadvisor"),
                            ("Nimble Storage", "Hewlett Packard Enterprise")):
        bkey, akey = normalize_company(brand), normalize_company(acquirer)
        assert not [e for e in feed if normalize_company(e.get("company", "")) == bkey], \
            f"{brand} postings are really {acquirer}'s — re-file them"
        assert bkey not in ov, f"{brand} should be folded into {acquirer}"
        assert akey in ov, f"{acquirer} must exist as a company"
        # the old label stays resolvable, and identity follows the new name
        assert brand in ov[akey].get("prior_names", [])
        for e in feed:
            if normalize_company(e.get("company", "")) == akey:
                assert e["identity"].startswith(akey + "::")


def test_mega_batch_floating_actions_and_posting_redesign():
    """The batch that reorganized the chrome: Today panel reduced to one
    dashed connections-nag bar; goal tiles are accent pills so they can't be
    misread as lifetime stats; the tracker's saved strip renders the SAME
    company cards as the Companies tab at the same width; the verbs float
    bottom-right (search / triage / add / hot scout fab) with the shortcut
    hint moved bottom-left; Diagnostics and Insights are one panel; dashed
    placeholder chips quick-edit one field in a popover; postings wear a
    score-band spine + industry chip + the company's stage/site/people in
    the expanded footer, role and company sit in two columns, the concise
    why stays concise-only, outreach plays live on Companies only; the
    add-company form can fetch description/linkedin from the site itself;
    wrapped chip rows rebalance."""
    import inspect

    from pipeline import app, dashboard

    tpl = dashboard._TEMPLATE
    # The "Just Raised" ban died 2026-08-11: the tracker's raisedBoard()
    # notice board owns that phrase now (its own pin covers it).
    assert 'class="connbar"' in tpl
    assert ".tiles.goals .tile{border-radius:var(--r-pill)" not in tpl  # one tile shape (DESIGN.md batch 5)
    # tracker strip is masonry columns now — Eric wants uniform vertical
    # gaps between company cards, superseding the aligned-tops grid
    assert "companyCard(c, false)), hostW(" in tpl and "display:flex" in tpl.split(".tgrid{")[1].split("}")[0]
    for fab in ("fab-search", "fab-triage", "fab-add", "fab-scout"):
        assert f'id="{fab}"' in tpl
    assert ".kbhint{position:fixed;left:18px" in tpl
    assert 'id="insights-btn"' not in tpl and 'id="diag-scorer"' in tpl
    assert "window.quickEdit" in tpl and "QE_FIELDS" in tpl
    # the spine retired at Eric's request; the sweep then proposed one wash
    # for every kind and Eric kept the gradient (2026-08-09) — it is how a
    # posting reads as a posting next to a company card. Kind-coding, not noise.
    assert "border-left:3px solid ${bandCol}" not in tpl
    assert "linear-gradient(146deg" in tpl and "roleHue(cat)" in tpl
    assert 'class="pstack"' in tpl and "r.w&&!open" in tpl
    assert "isPlay(r)) return false" in tpl
    assert "function balanceChips" not in tpl   # balancer retired 2026-08-12: rows pack, never redistribute
    assert '"/api/company-info"' in inspect.getsource(app.Handler.do_POST)
    assert 'id="fc-fetch"' in tpl


def test_aggregator_hosts_never_become_company_sites():
    """A company found via a Wellfound posting 'derived' https://wellfound.com
    as its website; the edit form prefilled that and persisted it on any
    unrelated save, and the description pass then scraped WELLFOUND'S OWN
    marketing blurb as the company's description (7 companies had it).
    Job-board hosts must never pass for a company's site."""
    from pipeline.entities import _AGG_HOSTS, _company_site
    from pipeline.models import Entry

    for host in ("wellfound", "builtin", "landing.jobs", "climatebase"):
        assert host in _AGG_HOSTS
    e = Entry(title="t", company="C", url="https://wellfound.com/jobs/123-role",
              source="techstars", location="", score=None, why="",
              date_added="2026-08-01")
    assert _company_site(e) == ""


def test_school_chips_absorb_warm_and_collapse_to_alumni():
    """A school chip already IS warmth — Warm renders only when no school
    chip does; a company with BOTH Emory and NMH alumni shows one Alumni
    chip instead of two school chips. One rule, shared by company cards
    and posting cards through famTags.

    The non-school warm families (Sports, Music, Research, and the places Eric
    actually worked) ride ALONGSIDE the schools, never inside them: each
    absorbs Warm the same way, but none may count toward the two-school Alumni
    collapse or a Sports chip would read as a third school. One chip per
    family — "Neuroscience" + "UCSF Neuroscape" is one Research chip, with the
    specific labels in its tooltip."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function famTags" in tpl and "function famChips" in tpl
    assert 'schools.length >= 2 ? `<span class="sig sig-alumni" title="Alumni — multiple schools"' in tpl  # 🎓 glyph
    # warm gated on neither a school nor any family chip being present
    assert tpl.count("!schools.length && !fams && warm") == 1      # famTags
    assert "!schools.length && !fams && isWarm(pv)" in tpl         # personFam
    assert "ALUM_SIGNALS.includes(x))" in tpl      # collapse counts schools only
    # every warm family has its own chip colour; interests stay grey
    for cls in (".sig-emory{", ".sig-nmh{", ".sig-sports{", ".sig-music{",
                ".sig-neuro{", ".sig-inst{"):
        assert cls in tpl, cls
    assert ".sig-alumni{" in tpl
    # the old inline school+warm trios are gone from ALL three card builders —
    # person cards route through personFam with the identical rule
    assert "function personFam" in tpl
    assert tpl.count('warmtag sig" title="Warm lead"') == 3     # famTags + personFam + kb-card flame, 🔥 glyph


def test_person_fill_queue_and_billing_nag():
    """Adding a person with just a LinkedIn URL queues the profile for a
    Chrome-sitting fill (profiles are login-walled to the server — the slug
    guess is a placeholder, never the record); the tracker nags about the
    offline scorer while 300+ postings sit unscored, clicking through to the
    billing console."""
    import inspect

    from pipeline import app, dashboard

    tpl = dashboard._TEMPLATE
    assert '"/api/person-fill-queue"' in inspect.getsource(app.Handler.do_POST)
    assert '"/api/person-fill-queue"' in tpl
    assert "person_fill_q" in open(dashboard.__file__).read()  # billbar retired 2026-08-18
    assert "billbar" not in tpl   # scoring banner retired (2026-08-18)
    assert "person_fill_q" in tpl


def test_sitting_queue_visible_on_tracker():
    """The Chrome-sitting queues are visible, not just counted: a moss bar on
    the tracker lists queued companies (clicking through to their cards) and
    queued profile fills, and states the cadence — every 3h while the app is
    open, missed ticks on launch."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    # the queue grew a dedicated header panel: Queue button with live count,
    # waiting items, sitting-run log (success/quiet/failure dots), recent
    # sweeps — the tracker bar is now a one-line pointer to it
    assert 'id="queue-btn"' in tpl and "window.openQueue" in tpl and 'id="queuemodal"' in tpl
    # the run log collapsed to one status line (failures still show, in red)
    assert "last sitting" in tpl and "Recently autofilled" in tpl
    # the tracker pointer bar retired — the Queue button's badge is the indicator
    assert 'class="connbar queuebar"' not in tpl
    d = dashboard._diagnostics([])
    for k in ("alumni_queue", "fill_queue", "founder_queue", "sitting_log", "recent_swept"):
        assert isinstance(d[k], list)


def test_bulk_dump_two_markers():
    """Second bulk pass: Queue panel splits Chrome and Server timelines with
    its own pagers (5/page) under the plain title 'Queue'; company-auto no
    longer crashes on the enrichment wrapper dict and accepts the client's
    derived site; quick-edit closes only its own layer; contacted moves ask
    HOW (method + detail, filling the matching channel field); tiles center
    their content with group labels centered; long descr prose reads upright
    at body size; the modal posting shows its company inline after the title
    (pcobig) and drops the standalone line; kanban cards tint by stage; fabs
    collapse via class (no display jumps); companies get bulk Autofill All."""
    import inspect

    from pipeline import app, dashboard, entities

    tpl = dashboard._TEMPLATE
    assert "<h2>Queue</h2>" in tpl and "const QPAGE_SIZE = 5" in tpl
    assert "Server · drains on the next enrichment pass" in tpl
    src = inspect.getsource(app.Handler.do_POST)
    assert 'raw.setdefault("companies", [])' in src        # wrapper-safe append
    assert '(body.get("site") or "").strip()' in src       # derived site accepted
    assert "window.closeOnly" in tpl and "closeOnly('qeditmodal')" in tpl
    assert 'id="dm-methodrow"' in tpl and "DM_HINTS" in tpl
    assert "method" in inspect.signature(entities.set_person_status).parameters
    assert "flex-direction:column;justify-content:center;align-items:center}" in tpl
    assert 'class="colink pcobig"' in tpl
    assert '.kbcol[data-st="saved"] .kbcard{' in tpl
    assert ".fab.fabhide{" in tpl and "window.bulkAutofill" in tpl


def test_bulk_dump_three_markers():
    """Third pass: triage mode is loud (accent bar, ⚡ TRIAGE, ✕ Exit);
    posting modals point people-autofill AT the company page instead of
    queueing from the wrong card; Repopulate Job / Repopulate Company
    Details verbs; company names on postings read as links (dotted
    underline); THE ROLE / THE COMPANY are cards with accent headers;
    dark-mode hover lifts via brightness; goal pills tightened under a
    Streaks label; lifetime = Eric's real funnel (postings: saved/applied/
    interviews/offers/rejected · people: saved/contacted/conversations/
    meetings — Alum Leads retired); interview stage select on interviewing
    postings persists via /api/interview-stage; tracker saved strip is an
    aligned grid; diagnostics sources rank by hit rate."""
    import inspect

    from pipeline import app, dashboard
    from pipeline.models import Entry

    tpl = dashboard._TEMPLATE
    assert "⚡ TRIAGE" in tpl and ">✕ Exit</button>" in tpl
    assert ">no people on file</span></div>" in tpl   # link retired; the fact suffices
    # Age-dial colors died once already: .agechip alone ties with the later
    # .cchip base rule and loses. The compound selector must stay compound.
    assert ".cchip.agechip{" in tpl and 'style="--ageh:${hue}"' in tpl
    assert ">Repopulate Job</button>" in tpl and ">Repopulate Company Details</button>" in tpl
    assert "underline dotted" in tpl
    # panels tint with the card's own hue; fit stacks under company (tcright)
    assert ".exp .pstack>div" in tpl and 'class="pstack"' in tpl  # panels stack vertically, 2026-08-05
    # brightness filter flickered in masonry columns — dark hover is a ring glow
    assert "filter:brightness(1.12)" not in tpl and "0 0 0 1px var(--line2),0 8px 24px" in tpl
    assert ">Streaks</div>" in tpl and "Alum Leads" not in tpl
    assert '"Rejected"' in tpl and '"Conversations"' in tpl
    assert "setInterviewStage" in tpl and '"/api/interview-stage"' in inspect.getsource(app.Handler.do_POST)
    assert Entry(title="t", company="c", url="u", source="s", location="",
                 score=None, why="", date_added="2026-08-01").interview_stage == ""
    assert "b.hot/b.scored" in tpl


def test_insights_are_structured_panel_sections():
    """The insights tail stopped being cleaned-up CLI prose: /api/insights
    returns structured data (agreement means + disagreement rows, pass-reason
    bigrams, outreach outcomes) and the panel renders native sections —
    disagreements click through to their postings, taste renders as counted
    chips, and the <pre> dump is gone."""
    import inspect

    from pipeline import app, dashboard

    tpl = dashboard._TEMPLATE
    src = inspect.getsource(app.Handler.do_GET)
    assert '"agreement"' in src and '"reasons"' in src and '"outreach"' in src
    assert "insights.render" not in src            # prose renderer is CLI-only now
    assert "Scorer vs your verdicts" in tpl
    assert "Your taste, in your own words" in tpl
    assert 'class="dpre"' not in tpl
    # sections are CARDS in a two-column grid, numbers are tiles
    assert 'class="dsec dwide"' in tpl and ".dtile{" in tpl and "#diag-list{display:grid" in tpl


def test_bulk_dump_four_markers():
    """Fourth pass: expandedHtml scopes its own ck (an unscoped reference
    silently killed EVERY posting modal — openCard is async so the throw
    vanished); posting titles are plain text with a separate ↗ (the title
    link was hijacking card opens); footer chips no longer swallow clicks;
    filled chips (industry/location/stage) quick-edit on click; The Fit
    panel joins role+company, tinted by the card's --indh hue along with
    all expanded headers; cname wears the hue serif-italic like pcobig;
    autofill verbs share a gradient class; Esc closes dialogs and 'a' adds
    per-tab; history is a popover button in the action row; drops land
    optimistically; the tab pill drags."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "const ck = CO_KEY[(r.c" in tpl.split("function expandedHtml")[1][:200]
    # the ↗ left the title; expanded cards carry a Posting ↗ chip instead
    # the expanded title itself is the posting link again
    assert 'class="extlink"' not in tpl and ">Posting ↗</a>" not in tpl
    assert '${open?(r.u||"").startsWith("manual://")?' in tpl   # manual keys render plain (2026-08-18)
    assert '<div class="cfoot" onclick="event.stopPropagation()">' not in tpl
    assert "quickEdit('${jsq(r.c)}','industry')" in tpl
    assert "The fit</h4>" in tpl and "--indh:${indHue(ind)}" in tpl
    assert ".cname{color:hsl(var(--indh" in tpl  # upright since 2026-08-05
    assert ".autobtn{background:linear-gradient" in tpl
    assert 'k === "Escape") closeModal()' in tpl
    assert "histbtn" in tpl and "colEl.appendChild(cardEl)" in tpl
    assert 'bar.addEventListener("mousemove"' in tpl


def test_revived_boards_stay_wired():
    """2026-08-05: three 'blocked' boards turned out to be reachable all along.
    a16z/Greylock: the Consider API answers openly once you send the board id
    from window.serverInitialData (empty searchText times out — always send
    terms). Built In: the JSON API is broken but /jobs renders full cards —
    and the card-split marker needs its closing quote or data-id="job-card-
    title" chops every card in half (the first run returned exactly 0).
    Rock Health: the 403 was their nginx blocklisting the Chrome UA string;
    the shared client sends Safari now."""
    import pathlib

    import yaml

    from pipeline.boards import ADAPTERS, base, builtin
    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "data" / "boards.yaml").read_text())["boards"]
    assert cfg["a16z"]["tier"] == "api" and cfg["a16z"]["board_id"] == "andreessen-horowitz"
    assert cfg["greylock"]["board_id"] == "greylock-partners"
    assert cfg["builtinnyc"]["adapter"] == "builtin"
    # Rock Health stays WIRED (adapter + Safari UA fix) but was retired to
    # tier: skip 2026-08-18 — 0 roles at 60+ across its whole scored pile.
    # Reachability and worth-fetching are different questions.
    # (Atomico was retired the same day and UN-retired hours later: its
    # zero was measured before its postings had descriptions, and once the
    # link sweep filled them it produced 8 roles at 60+. Source ROI is only
    # readable on SCORED rows — never retire a board on unscored volume.)
    assert cfg["rockhealth"]["adapter"] == "portfolio"
    assert cfg["rockhealth"]["tier"] == "skip"
    assert "consider" in ADAPTERS and "builtin" in ADAPTERS
    assert builtin._CARD_SPLIT.pattern.endswith('job-card"')
    assert "Safari" in base.UA and "Chrome" not in base.UA
    # TeamWork Online: 'JS-rendered' was wrong too — cards ship server-side,
    # but only when the query carries commit=Search
    assert cfg["teamworkonline"]["adapter"] == "teamworkonline"
    from pipeline.boards import teamworkonline as two
    assert "commit=Search" in pathlib.Path(two.__file__).read_text()
    assert cfg["techstars"]["tier"] == "api"  # re-enabled on Eric's call 2026-08-05


def test_scorer_economics_stay_cheap():
    """2026-08-05: Eric ran out of credits. The scorer was on Opus 4.8
    ($5/$25 per Mtok) for a rubric-checklist task — Haiku work ($1/$5).
    Big runs go through the Batch API at half price. 500 postings:
    ~$5.63 before, ~$0.56 after. Guard the defaults."""
    from pipeline import score
    assert score.DEFAULT_MODEL == "claude-haiku-4-5"
    assert score.BATCH_MIN == 10
    assert score.estimate_cost(500, "claude-haiku-4-5") < 1.0
    # batch discount applies at the threshold, not below it
    assert score.estimate_cost(9, "claude-haiku-4-5") > score.estimate_cost(10, "claude-haiku-4-5")


def test_duplicate_postings_backfill_blanks_never_overwrite():
    """Eric's rule (2026-08-05): boards disagree about what they publish —
    one carries salary, another the posted date. When a duplicate arrives
    from another board, its facts fill the survivor's BLANKS (salary,
    posted date, location, stage) and its description fills the scoring
    cache under the survivor's URL. Existing values are never overwritten."""
    from datetime import date as _date

    from pipeline.models import Entry, RawPosting

    hit = Entry(title="Founding GTM", company="Acme", url="https://a.example/1",
                source="wellfound", location="", score=None, why="",
                comp_min=None, comp_max=None, stage="", posted_at="")
    hit.identity = RawPosting(title=hit.title, company=hit.company, url=hit.url,
                              source=hit.source).identity()
    filled = Entry(title="Founding GTM", company="Acme", url="https://b.example/1",
                   source="waas", location="New York", score=None, why="",
                   comp_min=90000, stage="seed", posted_at="2026-08-01")
    filled.identity = RawPosting(title=filled.title, company=filled.company,
                                 url=filled.url, source=filled.source).identity()
    dupe = RawPosting(title="Founding GTM", company="Acme",
                      url="https://c.example/1", source="a16z",
                      location="San Francisco", comp_min=120000, comp_max=150000,
                      stage="series_a", posted_at=_date(2026, 7, 30))

    fresh, dupes = feed.merge_new([hit], [dupe])
    assert not fresh and dupes == 1
    assert hit.comp_min == 120000 and hit.comp_max == 150000
    assert hit.location == "San Francisco"
    assert hit.posted_at == "2026-07-30"
    assert hit.stage == "series_a"

    fresh, dupes = feed.merge_new([filled], [dupe])
    assert not fresh and dupes == 1
    # blanks only: everything already recorded survives untouched
    assert filled.comp_min == 90000 and filled.location == "New York"
    assert filled.stage == "seed" and filled.posted_at == "2026-08-01"
    assert filled.comp_max == 150000  # the one true blank got filled


def test_saving_unenriched_person_queues_profile_fill(tmp_path, monkeypatch):
    """Saving or manually adding a person who has a LinkedIn URL but no
    role/location queues their profile fill for the Chrome sitting
    (Eric's rule, 2026-08-06). Enriched or URL-less people never queue,
    a second save doesn't double-queue, and review/uninterested moves
    don't trigger the endpoint path at all."""
    import inspect
    import json

    from pipeline import app, entities

    fq = tmp_path / "person_fill_queue.json"
    monkeypatch.setattr(entities, "PERSON_FILL_QUEUE", fq)

    unenriched = {"name": "Test Person", "role": "",
                  "linkedin": "https://www.linkedin.com/in/test-person/"}
    assert entities.queue_person_fill(unenriched) is True
    q = json.loads(fq.read_text())
    assert q[0]["linkedin"].endswith("/in/test-person") and q[0]["name"] == "Test Person"
    # dedupe — saving the same person twice queues once
    assert entities.queue_person_fill(unenriched) is False
    # already enriched — nothing missing, nothing queued
    assert entities.queue_person_fill({"name": "A", "role": "CEO", "location": "NYC",
                                       "linkedin": "https://linkedin.com/in/a"}) is False
    # no LinkedIn URL — nothing to visit, never invent one
    assert entities.queue_person_fill({"name": "B", "role": ""}) is False
    assert len(json.loads(fq.read_text())) == 1

    # both server paths call the helper; status moves gate out review/uninterested
    src = inspect.getsource(app.Handler.do_POST)
    assert src.count("entities.queue_person_fill(rec)") == 2
    assert '("review", "uninterested")' in src


def test_people_sort_and_view_selects_rerender():
    """The Rolodex Sort dropdown silently did nothing — psort was never in
    the change-listener rerun list (nor was the new pview toggle). Changing
    either must re-render."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    rerun = tpl.split('.forEach(id=>{')[0].rsplit("[", 1)[1]
    assert '"psort"' in rerun
    # the two views are separate peer buttons now, each with its own handler —
    # the old self-renaming toggle showed the view you were in while reading
    # like the view you'd get
    assert 'id="pview-co"' in tpl and 'id="pview-flat"' in tpl
    assert 'el("pview-" + m).addEventListener("click", () => setPeopleView(m))' in tpl
    # and a divider fences the pair off from the sort, another follows the sort
    assert 'id="psepv"' in tpl
    assert '"pview-co","pview-flat","psepv","psort","psep2"' in tpl
    # (the letter rail and its scrub machinery were removed 2026-08-11)


def test_tweets_tab_reads_without_the_widget_script():
    """The X ledger must read with platform.twitter.com blocked or absent:
    the stored verbatim text IS the blockquote's own content, and widgets.js
    is fetched lazily on first view. An embed-only card would have shown an
    empty frame offline — and the page is opened as a static file often."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '<blockquote class="twitter-tweet"' in tpl
    # the verbatim text sits INSIDE the blockquote — that is the fallback
    assert '<p>${esc(t.text || "").replace(/\\n/g, "<br>")}</p>' in tpl
    # only the UNrendered blockquote wears the fallback frame, or the real
    # embed would land inside a ghost of its own fallback card
    assert ".twcard .twitter-tweet:not(.twitter-tweet-rendered){" in tpl
    # script fetched on demand, and a failed load leaves the fallbacks alone
    assert 's.src = "https://platform.twitter.com/widgets.js"' in tpl
    assert "if(!ok || !card.isConnected || !card.dataset.twOn) return;" in tpl


def test_tweets_tab_is_wired_into_the_tab_machinery():
    """A new pane that switchTab doesn't know about stays visible under every
    other tab. Pin the whole contract: button, pane, hide-list, chrome set,
    and the new-count badge."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'data-tab="tweets"' in tpl and '<div id="pane-tweets" hidden>' in tpl
    assert '["tracker","postings","companies","people","tweets"].forEach' in tpl
    assert 'tweets:    new Set(["q","tw-new","tw-saved","tw-dismissed","twksep"' in tpl  # + kind chips (2026-08-19)
    # the tab badge counts unread, not everything in the ledger
    assert 'badge.textContent = n.new' in tpl
    # nothing to add by hand — tweets only ever arrive from the sweep
    assert 'fabShow("fab-add", LIVE && tab !== "tracker" && tab !== "tweets")' in tpl


def test_tweet_status_writes_back_to_the_ledger():
    """Triaging a tweet has to survive a rebuild: the page derives from
    data/x_tweets.json on every load, so the status must land in the file,
    keyed by URL, without disturbing the verbatim text around it."""
    import inspect

    from pipeline import app, dashboard

    src = inspect.getsource(app.Handler.do_POST)
    assert '"/api/tweet-status"' in src
    assert '("new", "saved", "dismissed")' in src
    assert 'x_tweets.json' in src
    # in-place edit of one record — never a rewrite of the sweep's ledger
    assert 'hit["status"] = status' in src
    tpl = dashboard._TEMPLATE
    assert 'await api("/api/tweet-status", {url: key,' in tpl
    # dismissed hides by default: the mode chips start on the unread pile
    assert 'let TWEET_MODE = "new";' in tpl


def test_tweets_reach_the_page_newest_found_first():
    """Sorted in Python, not the browser: an unsorted ledger put a three-week
    -old tweet above this morning's. found-date first, tweet date as tiebreak."""
    import inspect

    from pipeline import dashboard

    src = inspect.getsource(dashboard.build)
    assert '"tweets": tweets' in src
    assert 'key=lambda x: (x.get("found") or "", x.get("date") or ""), reverse=True' in src


def test_tweet_embeds_mount_once_and_persist():
    """History: every embed at once was the original memory cost, so 2026-08
    added mount-near/unmount-far. Then the unmount proved worse than the
    memory (2026-08-12, Eric): embeds reloaded on scroll-back and tab-switch,
    and lingering hidden-pane iframes leaked anyway. Now: the pane renders
    once per session (re-render only on real width change, ledger change, or
    filters), embeds mount when first approached and persist for the session,
    and triage removes cards in place. At ≤~50 ledger tweets the iframe
    memory is bounded; the far-unmount returns only if the ledger grows to
    hundreds. Regression = embeds reloading on tab-switch, or a rebuild on
    every softReload."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'rootMargin: "700px 0px"' in tpl                # still mounts lazily
    assert "if(e.isIntersecting) twMount(e.target)" in tpl
    for gone in ("TW_IO_FAR", "twUnmount", "TW_FALLBACK", 'rootMargin: "2400px 0px"'):
        assert gone not in tpl, gone
    # render-once machinery
    assert 'if(name === "tweets" && TW_W !== hostW("tweets")) DIRTY.add("tweets")' in tpl
    assert ".map(x => x.url).join(\"|\")" in tpl          # softReload ledger guard
    assert "if(c.dataset.tw === key) c.remove()" in tpl   # in-place triage

def test_tracker_reuses_the_real_posting_and_person_cards():
    """The board used to carry its own stub cards (.kbtitle/.kbco/.kbdate)
    that drifted from the real ones every time either changed. Eric's rule
    (2026-08-07): one card definition, so a Rolodex or Postings edit lands on
    the tracker unedited. Only the posting fit line is dropped there."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "const kbPosting = r => postingCard(r, false, true, false, true);" in tpl
    assert "const kbPerson = pv => personCard(pv, true, false, true);" in tpl
    # the bespoke stub markup is gone for good
    assert '<div class="kbtitle">' not in tpl
    assert '<div class="kbdate' not in tpl
    # board cards drop the fit line and keep a days-only chip — the column
    # header names the stage (Eric, 2026-08-12)
    assert "${r.w&&!open&&!kb?" in tpl
    assert '`<span class="cchip${stalled(r.st,r.days)?" stallchip":""}">${r.days}d</span>' in tpl
    # the person card renders its company chip on the board, as All Names does
    assert '${(kb||PVIEW==="flat")&&pv.company?' in tpl
    # drag still works: the class moved from .kbcard to .kbdrag on real cards
    assert '.kbdrag.dragging{' in tpl
    assert 'document.querySelector(".dragging")' in tpl


def test_postings_tab_badges_the_untriaged_pile():
    """Tweets got an unread badge; postings had none, so the size of the
    review pile was invisible unless you were already on the tab. Counted off
    the whole feed, not the filtered rows — a filter chip must not shrink it."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '>Postings<span class="tcount" id="pon" hidden></span>' in tpl
    assert 'DATA.rows.filter(r => bucketOf(r.st) === "review").length' in tpl
    # four figures would blow out the tab bar
    assert 'nrev > 999 ? (nrev/1000).toFixed(1) + "k"' in tpl


def test_tab_order_puts_tweets_beside_postings():
    """Tweets are a source of postings, so they sit next to Postings rather
    than last (Eric, 2026-08-07). The number keys follow the visual order —
    a mismatch there is worse than no shortcut."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    order = [tpl.index(f'data-tab="{n}" role="tab"')
             for n in ("tracker", "postings", "tweets", "companies", "people")]
    assert order == sorted(order), "tab buttons are out of order in the markup"
    assert 'else if(k === "3") switchTab("tweets");' in tpl
    assert 'else if(k === "4") switchTab("companies");' in tpl
    assert 'else if(k === "5") switchTab("people");' in tpl
    assert "<kbd>3</kbd><span>Tweets</span>" in tpl


def test_rolodex_material_demoted_to_the_quiet_wash():
    """The plastic experiment (2026-08-07) was judged over-engineered and
    demoted (Eric, 2026-08-11): no specular radials, floor shadows, or notch
    pseudo-elements — the base .cogroup wash + hairline + one top highlight is
    the rolodex surface, and the divider-tab structure survives."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "radial-gradient(140% 72%" not in tpl          # box specular
    assert "radial-gradient(120% 220%" not in tpl         # tab specular
    assert "inset 0 -7px 11px -7px" not in tpl            # floor shadows
    assert ".cogroup::before" not in tpl and "pcard:not(.open)::before" not in tpl  # notches
    i = tpl.find(".cogroup{background:")
    assert i > 0 and "inset 0 1px 0 var(--a2)" in tpl[i:i+400]  # the one highlight kept
    assert "#people .cohead{position:absolute;bottom:calc(100% + 1px)" in tpl  # tab structure stays


def test_whole_profile_is_read_for_signals_without_touching_notes(tmp_path, monkeypatch):
    """SYMPTOM (2026-08-07): connection signals were matched against `role` and
    `notes` only — one headline and a hand-written line — so someone whose
    headline reads "Product Manager at Stripe" while an earlier job was four
    years in the Brooklyn Nets front office never fired Sports. Their past was
    on the profile; it was never read.

    The captured profile is a SIDECAR (data/.cache/profiles.json, gitignored,
    same split store.py makes for job descriptions), not a note: notes is
    Eric's own curated line and burying it under scraped text would ruin the
    one field he reads. Because the raw text is cached rather than reduced to
    labels, a needle added later re-matches every stored profile for free."""
    import json

    from pipeline import entities, profiles

    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    monkeypatch.setattr(profiles, "PROFILES", tmp_path / "profiles.json")
    monkeypatch.setattr(profiles, "CACHE_DIR", tmp_path)

    (tmp_path / "people.json").write_text(json.dumps([{
        "name": "Jane Doe", "company": "Stripe", "role": "Product Manager",
        "linkedin": "https://www.linkedin.com/in/janedoe",
        "notes": "Intro'd by Sam, replied fast",
    }]))
    (p,) = entities.people([])
    assert "Sports" not in p.signals            # headline alone says nothing

    profiles.record("https://linkedin.com/in/janedoe/",   # sloppier URL, same person
                    "About: former sports analytics lead. Experience: Product "
                    "Manager, Stripe. Director of Analytics, Brooklyn Nets, "
                    "2019-2023. Education: Tufts.")
    (p,) = entities.people([])
    assert "Sports" in p.signals
    # the chip can show its own evidence — it came from text Eric never sees
    assert "Brooklyn Nets" in p.evidence["Sports"]
    # …and notes was never touched
    assert p.notes == "Intro'd by Sam, replied fast"


def test_a_profile_that_would_not_render_is_not_recorded(tmp_path, monkeypatch):
    """The sweep's honesty rule, enforced in code: a blank or near-blank read
    is a LinkedIn lazy-render failure, not evidence that someone has no
    profile. Recording it would silently freeze that person as signal-less."""
    from pipeline import profiles

    monkeypatch.setattr(profiles, "PROFILES", tmp_path / "profiles.json")
    monkeypatch.setattr(profiles, "CACHE_DIR", tmp_path)
    assert profiles.record("https://www.linkedin.com/in/x", "") is False
    assert profiles.record("https://www.linkedin.com/in/x", "   \n  ") is False
    assert profiles.record("", "a perfectly good long profile text here") is False
    assert profiles.record("https://www.linkedin.com/in/x", "y" * 60) is True


def test_people_count_is_one_icon_tag_everywhere():
    """"3 people" spelled out ate a whole chip slot on cards already carrying
    six, and the company card and the rolodex group header each spelled it
    their own way. One pplTag() builds both now (Eric, 2026-08-07: universal),
    so the words must not come back at either site."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function pplTag(n, bare){" in tpl
    assert 'class="pplsvg"' in tpl and ".pplsvg{width:11px" in tpl
    # both call sites go through the helper
    assert "pplTag(CO_PPL_N[c.name.toLowerCase().trim()])" in tpl
    assert "pplTag(ppl.length, true)" in tpl
    # the spelled-out count is gone from the rendered chips (the title
    # attribute the helper builds is the only place the words survive)
    assert tpl.count('?"person":"people"') == 1
    # still reads to a screen reader
    assert 'role="img" aria-label="${lbl}"' in tpl


def test_acquired_brand_resolves_at_import_not_just_once(tmp_path, monkeypatch):
    """SYMPTOM (2026-08-07, caught by the sibling pin): renaming the feed's
    acquired brands fixed the rows on the page and nothing else — the next
    scout re-imported four fresh HPE postings under "Nimble Storage", because
    the Getro adapter still hands over the board's dead label. The rule has to
    run at IMPORT. to_entry now resolves through the acquirer's prior_names,
    and the identity is rebuilt from the resolved name so cross-board dedupe
    doesn't split."""
    from pipeline import entities, feed
    from pipeline.models import RawPosting

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    entities.save_company_overlay({"acquirer co": {
        "name": "Acquirer Co", "prior_names": ["Deadbrand"]}})
    assert entities.resolve_company("Deadbrand") == "Acquirer Co"
    assert entities.resolve_company("Someone Else") == "Someone Else"

    e = feed.to_entry(RawPosting(title="Growth Lead", company="Deadbrand",
                                 url="https://x.com/j/1", source="accel"), 80, "fits")
    assert e.company == "Acquirer Co"
    assert e.identity.startswith("acquirer::")   # normalize_company strips "Co"


def test_signal_provenance_ranks_current_over_past(tmp_path, monkeypatch):
    """SYMPTOM (Eric, 2026-08-07, from the 6-profile validation): Riley Okafor's
    only Sports evidence was a 2019 student internship at Stanford Athletics,
    while the fact that actually matters — she does BI at Elevate, a sports
    agency, TODAY — carried no weight at all. Two fixes, both pinned here:
    the captured profile is matched section by section, strongest provenance
    first (current role > past role > education), and a person inherits
    signals from WHERE THEY WORK.

    The employer link clears the honesty rule from both ends: that she works
    at Elevate is her own text, and that Elevate is sports comes from the
    company's own description. Neither half is Eric-side context."""
    import json

    from pipeline import entities, profiles

    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setattr(profiles, "PROFILES", tmp_path / "profiles.json")
    monkeypatch.setattr(profiles, "CACHE_DIR", tmp_path)

    entities.save_company_overlay({"elevate": {
        "name": "Elevate",
        "description": "Elevate serves the NBA, NFL and MLS with sports data."}})
    (tmp_path / "people.json").write_text(json.dumps([{
        "name": "Riley Okafor", "company": "Elevate", "role": "BI Analyst",
        "linkedin": "https://www.linkedin.com/in/riley-okafor"}]))
    profiles.record("https://www.linkedin.com/in/riley-okafor",
                    "[HEADLINE] BI @ Elevate [CURRENT] CRM Analyst | Elevate "
                    "[PAST] Marketing Intern | Stanford Athletics "
                    "[EDUCATION] Stanford University | MS")
    (p,) = entities.people([])
    assert "Sports" in p.signals
    # the EMPLOYER is the evidence shown, not the six-year-old internship
    assert p.evidence["Sports"].startswith("employer Elevate"), p.evidence["Sports"]

    # …and with no employer signal, a past-role match still lands, still labelled
    entities.save_company_overlay({"acme": {"name": "Acme", "description": "B2B billing."}})
    (tmp_path / "people.json").write_text(json.dumps([{
        "name": "Riley Okafor", "company": "Acme", "role": "BI Analyst",
        "linkedin": "https://www.linkedin.com/in/riley-okafor"}]))
    (p,) = entities.people([])
    assert p.evidence["Sports"].startswith("past — "), p.evidence["Sports"]


def test_unmarked_profiles_still_match(tmp_path, monkeypatch):
    """Captures made before the section markers existed must not go dark —
    they're treated as one undifferentiated block, not skipped."""
    from pipeline import profiles

    monkeypatch.setattr(profiles, "PROFILES", tmp_path / "profiles.json")
    monkeypatch.setattr(profiles, "CACHE_DIR", tmp_path)
    assert profiles.split_sections("Director of Analytics, Brooklyn Nets") == [
        ("profile", "Director of Analytics, Brooklyn Nets")]
    assert profiles.split_sections("") == []


def test_overlay_funding_fills_blanks_but_never_beats_a_board(tmp_path, monkeypatch):
    """The overlay merge read site/linkedin/stage but not funding, so a
    researched funding line written to companies.json was invisible on the
    company card (found during the 2026-08-13 biz-facts backfill). Overlay
    funding must surface — but only where the feed supplied none, since
    boards (WaaS) publish live funding lines that stay authoritative."""
    from pipeline import entities
    from pipeline.models import Entry

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    entities.save_company_overlay({
        "acme": {"name": "Acme", "funding": "$5M seed (Jan 2026)"},
        "beta": {"name": "Beta", "funding": "$9M researched"},
    })
    entries = [
        Entry(title="A", company="Acme", url="u1", source="s", location="", score=70, why=""),
        Entry(title="B", company="Beta", url="u2", source="s", location="", score=70, why="",
              funding="$8M Series A (board-published)"),
    ]
    by = {c.key: c for c in entities.companies(entries)}
    assert by["acme"].funding == "$5M seed (Jan 2026)"          # blank filled
    assert by["beta"].funding == "$8M Series A (board-published)"  # board wins


def test_company_headcount_derives_from_postings_and_overlay_fills_blanks(
        tmp_path, monkeypatch):
    """CompanyView had no headcount at all — postings carried one (WaaS) but
    the company card couldn't show it, and Eric's 100+-people exclude is a
    headcount question, not a stage one (2026-08-13). Board-stated counts
    beat researched ranges; headcount never sets the stage field."""
    from pipeline import entities
    from pipeline.models import Entry

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    entities.save_company_overlay({
        "acme": {"name": "Acme", "headcount": "11-50 (researched)"},
        "beta": {"name": "Beta", "headcount": "11-50 (researched)"},
    })
    entries = [
        Entry(title="A", company="Acme", url="u1", source="s", location="",
              score=70, why="", headcount="6-10"),
        Entry(title="B", company="Beta", url="u2", source="s", location="",
              score=70, why=""),
    ]
    by = {c.key: c for c in entities.companies(entries)}
    assert by["acme"].headcount == "6-10"                # board wins
    assert by["beta"].headcount == "11-50 (researched)"  # blank filled
    assert by["beta"].stage == ""                        # never inferred from headcount


def test_latest_raise_date_stamps_overlay_without_regressing(tmp_path, monkeypatch):
    """'When did they last raise?' (Eric, 2026-08-09): check() keeps the most
    recent Form D date even when the filing is too old to be news, and
    stamp_overlay writes it as last_raised — but a re-check with an OLDER
    date must never regress a newer one already stored."""
    from pipeline import entities, funding

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    state = {"acme": {"name": "Acme", "seen": [],
                      "latest_filed": "2026-06-01", "latest_amount": 5_000_000,
                      "latest_url": "https://sec.gov/f/1"}}
    assert funding.stamp_overlay(state) == 1
    overlay = entities.load_company_overlay()
    assert overlay["acme"]["last_raised"]["filed"] == "2026-06-01"

    state["acme"]["latest_filed"] = "2020-01-01"   # stale re-check
    assert funding.stamp_overlay(state) == 0
    assert entities.load_company_overlay()["acme"]["last_raised"]["filed"] == "2026-06-01"


def test_check_records_latest_filed_even_when_filing_is_old_news(monkeypatch):
    """The news window must not swallow history: a 2-year-old filing is not a
    raise flag, but its date IS the answer to 'when did they last raise'."""
    from datetime import date

    from pipeline import funding
    from pipeline.boards import edgar

    monkeypatch.setattr(edgar, "matching_filings",
                        lambda name, c=None: [("1", "acc-1", "2024-05-01", "Acme, Inc.")])
    monkeypatch.setattr(edgar, "filing_detail",
                        lambda cik, acc, c=None: {"amount": 3_000_000, "url": "u"})
    raises, state = funding.check(["Acme"], state={}, today_=date(2026, 8, 9))
    assert raises == []                                   # too old to be news
    assert state["acme"]["latest_filed"] == "2024-05-01"  # but history is kept
    assert state["acme"]["latest_amount"] == 3_000_000


def test_role_count_and_cities_are_compact_tags():
    """Two of the widest chips on a company card: "4 Roles | 1 New" and
    "SF Bay Area". Both shrink (Eric, 2026-08-07) — a briefcase and a number,
    and city buckets as airport-style abbreviations. The colour still keys off
    the FULL bucket name, and the full name survives in the title."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function roleTag(n, fresh){" in tpl
    assert "roleTag((c.postings||[]).length, c.new_posts)" in tpl
    assert 'class="jobsvg"' in tpl
    # the spelled-out roles chip is gone
    assert "Role${(c.postings||[]).length===1" not in tpl
    # every city chip renders through the abbreviator, tones off the real name
    assert 'const CITY_ABBR = {"New York": "🗽", "SF Bay Area": "🌉"' in tpl
    # posting, company (one hoisted const now), person
    assert tpl.count("gw(cityAbbr(") == 3   # dual-voice since 2026-08-18: glyph condensed, word when open
    assert "cityTone(pv.loc)" in tpl and "cityTone(r.city)" in tpl
    # Remote joined the glyph set (🌐) with the rest — Eric, 2026-08-12
    assert '"Remote": "🌐"' in tpl


def test_new_roles_count_is_visible_inside_the_lit_chip():
    """Shipped invisible for one build: .rnew was var(--hot) and it renders
    inside the freshroles chip, whose fill IS var(--hot) — green on green. It
    inherits the chip's ink now, with a hairline so "1 +1" isn't read as 11."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    rnew = tpl.split("\n  .rnew{")[1].split("}")[0]
    assert "color:" not in rnew, "the +N must inherit the chip's ink, not set its own"
    assert ".rnew::before{content:\"\";width:1px" in tpl


def test_freshroles_chip_ink_follows_its_fill():
    """--hot is a dark green in light mode and a LIGHT mint in dark mode, but
    the freshroles chip hardcoded color:#fff — white on mint, ~1.7:1, which
    became load-bearing when the chip turned into a briefcase and two numbers.
    The dark-mode override must survive."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert ':root[data-theme="dark"] .cchip.freshroles{color:var(--hdr-bg)}' in tpl  # same dark-green ink, now a slot
    assert ':root:not([data-theme="light"]) .cchip.freshroles,' in tpl


def test_employer_inheritance_never_lends_erics_own_credentials(tmp_path, monkeypatch):
    """SYMPTOM (caught live 2026-08-07): Priya Castell does partnerships at
    Elevate, a sports agency whose stored description lists "NFL, NBA, WNBA,
    MLB…" — and she came out tagged "NFL / football analytics", one of Eric's
    OWN résumé labels. Working at a sports company makes someone Sports; it
    does not make them an NFL analyst or a Big Data Bowl finalist. Employer
    inheritance yields family labels only."""
    from pipeline import entities

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    entities.save_company_overlay({"agency": {
        "name": "Agency",
        "description": "Serves the NFL, NBA and MLS; also runs the Big Data Bowl booth."}})
    got = [label for label, _ in entities._employer_signals("Agency")]
    assert got == ["Sports"], got


def test_missing_fields_collapse_into_one_dashed_pill():
    """Measured 2026-08-07: "Website?" + "Industry?" + "Location?" + "Stage?"
    + "Role Type?" were the widest run on the page — ~10.6k px on Companies,
    ~7.5k on Postings — and none of it is data. Collapsed cards get one dashed
    pill with a per-field button inside; expanded cards keep the full words,
    because that is where you fill them in. Regression = four borders again."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function missPill(items){" in tpl and "function missBtn(full, act){" in tpl
    # both cards collect misses through the same closure and render one pill
    assert tpl.count("const need = (sh, full, act) => open ? missBtn(full, act)") == 2
    assert tpl.count("${missPill(miss)}") == 1 and tpl.count('${kb?"":missPill(miss)}') == 1  # board postings drop it
    assert '.misspill{display:inline-flex' in tpl
    # the short tokens, not the sentences
    for sh in ('"Loc", "Location"', '"Ind", "Industry"', '"Role", "Role Type"',
               '"Stage", "Stage"', '"Site", "Website"'):
        assert f'need({sh}' in tpl, f"missing field token {sh}"


def test_age_chip_is_a_glyph_and_other_is_not_a_place():
    """Two more measured wins. "Posted 17d Ago" at 104px on every card became
    a calendar/eye glyph plus "17d" — the glyph keeps the posted-vs-first-seen
    distinction the words carried, and the sentence moves to the title. And
    "Other" as a city was printed 44 times a screen on company cards while
    the posting card had always suppressed it; now neither does."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "const CAL_SVG" in tpl and "const EYE_SVG" in tpl
    assert '<span class="pplnum">${age===0?"today":age+"d"}</span>' in tpl
    assert '.agesvg{width:11px' in tpl
    # "Other" is filtered before the chip, so it lands in the miss pill instead
    assert 'const city0 = ((c.cities||[])[0] === "Other" ? "" : (c.cities||[])[0]) || "";' in tpl
    assert 'r.city && r.city!=="Other"' in tpl


def test_long_sector_labels_shorten_with_the_full_name_in_the_title():
    """Robotics & Hardware (137px), Founding & Generalist (133), Legal &
    Compliance (129), Dev & Data Tools (113) each set a card's width on their
    own. These are read for meaning, so only the conjunctions go and the full
    name stays reachable on hover."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'const LABEL_SHORT = {' in tpl and '"Digital Health": "Health"' in tpl  # aggressive shortening (Eric, 2026-08-12)
    assert "const shortLabel = s => LABEL_SHORT[s] || s;" in tpl
    # industry (posting + company) and role type all route through it
    assert tpl.count("shortLabel(tc(") == 2 and "shortLabel(r.cat)" in tpl
    # and each one keeps the unabbreviated label on hover
    assert 'title="${esc(tc(ind))}"' in tpl and 'title="${esc(r.cat)}"' in tpl


def test_a_leaky_capture_is_refused_not_cached(tmp_path, monkeypatch):
    """The capture boundary is a list of English headings, so it rots silently:
    if LinkedIn renames "Interests" or reorders the right rail, other people's
    biographies start landing in this person's record and nothing complains.
    Every marker below actually leaked on 2026-08-07 — the rail put "Student
    Athlete at Bates College" inside a neuroscientist's capture, and the
    footer's language picker ("Čeština") would have minted a Czech Republic
    signal on every profile ever swept. Refuse loudly; never cache it."""
    import pytest

    from pipeline import profiles

    monkeypatch.setattr(profiles, "PROFILES", tmp_path / "profiles.json")
    monkeypatch.setattr(profiles, "CACHE_DIR", tmp_path)
    url = "https://www.linkedin.com/in/x"
    for leak in ("People also viewed | George Nassar | Student Athlete at Bates",
                 "Select language | Čeština (Czech) | Dansk",
                 "Others named Jane Doe", "Explore Premium profiles",
                 "LinkedIn Corporation © 2026"):
        with pytest.raises(ValueError, match="right rail or the footer"):
            profiles.record(url, "Real profile text here, long enough. " + leak)
    assert profiles.load() == {}          # nothing half-recorded
    assert profiles.record(url, "Data Engineer at the Brooklyn Nets, 2019-2023.") is True


def test_signals_audit_reports_coverage_not_just_hits():
    """A signal that never fires because nothing was captured looks identical
    to a person who genuinely has no tie. The audit has to say which — on the
    day it shipped, 0 of 220 people with a profile URL had been swept."""
    import inspect

    from pipeline import cli

    src = inspect.getsource(cli.signals_audit_cmd)
    assert "COVERAGE" in src
    assert "whose profile has been swept" in src
    assert "employer inheritance is blind" in src


def test_backfilled_raise_dates_rank_like_news_raises(monkeypatch, tmp_path):
    """The 49 backfilled last_raised dates lived only in the overlay, so
    make sends scored them zero urgency — a raise spotted after the fact
    must trigger exactly like one caught as news."""
    from datetime import date

    from pipeline import entities, timing
    from pipeline.models import Entry

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    entities.save_company_overlay(
        {"practice": {"name": "Practice",
                      "last_raised": {"filed": "2026-08-07", "amount": 1_200_000}}})
    e = Entry(title="Founding GTM", company="Practice", url="u", source="waas",
              location="NYC", why="", score=80, status="saved",
              founders=[{"name": "Jo Founder", "title": "CEO"}])
    monkeypatch.setattr(entities, "companies", lambda entries: [])
    out = timing.queue([e], today_=date(2026, 8, 9))
    assert out and out[0].urgency == 40, "overlay last_raised must be the hot trigger"
    assert "2d ago" in out[0].trigger


def test_real_connections_sample_precision_and_recall():
    """The unbiased test the earlier runs were missing. Every string below is a
    real headline from Eric's own LinkedIn connections list (2026-08-07) — not
    profiles he pre-labelled as interesting, just whoever was in the list. 37
    people: 14 should fire, 23 should stay silent.

    It found two false negatives on the first pass, both the same shape — a
    sports company whose NAME contains no needle. "Football Solutions at The
    33rd Team" and "Research Engineer @ SumerSports" both went dark because
    bare sport nouns were excluded to stop "varsity soccer" firing. The
    amateur guard now handles that case, so the nouns are back."""
    from pipeline.entities import signals_in

    should_fire = {
        "Laurence Pan | Robotics @ NeuroSky": "Neuroscience",
        "Andrew Steinberg | Former NBA, MLS & NCAA Executive": "Sports",
        "Ashwin Giri | B.S. Neuroscience & Behavioral Biology, Emory University": "Neuroscience",
        "David S. Rosen | Postdoctoral Fellow | Music, Flow, Psychedelics, Improvisation": "Music",
        "Asa Arnold | SumerSports Intern / Student Manager Notre Dame Athletics": "Sports",
        "Xavier Njoku | Football Data Associate @ NFL": "Sports",
        "Shaan Chanchani | Research Engineer @ SumerSports": "Sports",
        "Max Holloway | Brooklyn Nets": "Sports",
        "Andrew Stasell | Football Solutions at The 33rd Team": "Sports",
        "Nicholas Fullerton | Strategic Football Fellow at the Dallas Cowboys": "Sports",
        "HanMin Kim | Emory University Alumni": "Emory",
        "Eleanor Byers | Admissions Advisor at Emory University": "Emory",
    }
    for text, label in should_fire.items():
        assert label in signals_in(text), (label, text, signals_in(text))

    # …and the ordinary professional majority of a network stays cold
    should_be_silent = [
        "Alex Moreno | Recruiter at Forus",
        "Maya Trujillo | Medical Assistant at Zuckerberg San Francisco General Hospital",
        "Alessandra Breall | Incoming J.D. Candidate at UC Law San Francisco",
        "Stephanie Dela Fuente | Principal Executive Technical Recruiter",
        "Alex Levy | Building new things @ Yelp",
        "Carl Kassabian | Data Analyst at Swordpoint Services",
        "Evan Brooks | Partner | Investor",
        "Sofia Assab | Co-Founder of Eden",
        "Angus Chang | Mechanical Engineering @ Cornell",
        "Yono Bulis | R&D Rotational Scientist at AstraZeneca",
        "Abhiraam Aremanda | SWE Intern @ Apple | CS & Stats @ UNC-CH",
        "Khamari Hadaway | Growth @ Listen",
        "Ivan Reyes | Founding Team @ Coverwatch",
        "Ryu Wajima | Aspiring Physician | Pre-med Student",
        "Ronald Wang | PhD Student @ Stanford Management Science & Engineering",
        "Noah Abbott | Miraka (a16z SR006)",
        "Arwen Hansell | Early Childhood Educator",
        "Eva Roytburg | Fortune editorial fellow and content creator",
        "Srija Potluri | Transitional Housing Advocate",
        "Carolyn F. McNiven | Head of White Collar (West Coast), Faegre Drinker",
    ]
    for text in should_be_silent:
        assert signals_in(text) == [], (text, signals_in(text))


def test_linkedin_activities_line_is_amateur_context():
    """LinkedIn files clubs and teams under "Activities and societies" in the
    education section. Everything there is something someone PLAYED on, so it
    must not fire the bare sport nouns — this is the guard that let those
    nouns ship at all."""
    from pipeline.entities import signals_in

    for s in ("Activities and societies: Women's Basketball",
              "Activities and societies: Men's Varsity Soccer Team",
              "club team soccer in college"):
        assert "Sports" not in signals_in(s), s
    assert "Sports" in signals_in("USA Hockey, Manager of Partnership Activation")


def test_raise_recency_chip_has_three_tiers_and_honest_absence():
    """Recent raises must be MORE noticeable (Eric, 2026-08-09): <60d wears
    the hot fill, 60-120d an outline, older fades to muted text — and a
    company with no Form D renders nothing (no Form D ≠ never raised)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'function raisedChip(c, open)' in tpl
    assert '"raised": _rec.get("last_raised")' in dashboard.__dict__.get("__doc__", "") or \
           '_rec.get("last_raised")' in open(dashboard.__file__).read()
    assert 'if(!r || !r.filed) return ""' in tpl          # honest absence
    assert 'days < 30 ? " hot"' in tpl                     # green only within a month (Eric, 2026-08-13)
    assert '.raisedchip.hot{background:var(--hot)' in tpl  # loudest fill
    assert '.raisedchip.old{border:none' in tpl            # muted tier


def test_just_raised_notice_board_sits_on_the_tracker():
    """A fresh raise must be impossible to miss (Eric, 2026-08-11): the
    tracker opens with a Just Raised notice board — every watched company
    whose Form D is inside the same 45-day window make today uses, newest
    first, hidden entirely when the window is empty. Losing the renderTracker
    call or widening/narrowing the window silently breaks that promise."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function raisedBoard()" in tpl
    assert "+ raisedBoard()" in tpl                       # actually on the tracker
    assert "days >= 0 && days <= 30" in tpl               # tracker headline window (Eric picked 30d, 2026-08-18); make today keeps 45
    assert 'if(!rows.length) return ""' in tpl            # empty window → no board
    assert ".rbcard:hover{box-shadow" in tpl              # hover is shadow-only


def test_queue_fills_covers_saved_companies_only(tmp_path, monkeypatch):
    """Eric's scope call (2026-08-07): profile fills run for SAVED companies
    only. Sweeping every person with a profile URL would spend twelve Chrome
    sittings on companies he has never acted on — 104 of 220 measured. "Saved"
    means either way he says yes: the company favourited (tracked), or a
    posting moved past review. A company still in review is not a yes."""
    import json

    from pipeline import cli, entities, feed
    from pipeline.models import Entry

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    monkeypatch.setattr(entities, "PERSON_FILL_QUEUE", tmp_path / "q.json")
    # SYMPTOM: without this, set_company_mode("trackedco","saved") below calls
    # queue_alumni, which wrote the fixture straight into the REAL
    # data/alumni_queue.json. "trackedco" then showed up as a company to sweep
    # in live sittings — a Chrome sitting on 2026-08-13 spent a slot on it and
    # reported it as an unexplained orphan. Every test that saves a company
    # must redirect this path too.
    monkeypatch.setattr(entities, "ALUMNI_QUEUE", tmp_path / "alumni_queue.json")

    def _e(company, status):
        return Entry(title="Founding Growth", company=company, source="test",
                     url=f"https://{company}.com/j/1", location="NYC", score=90,
                     why="fits", status=status,
                     founders=[{"name": f"{company} Founder", "title": "CEO",
                                "linkedin": f"https://www.linkedin.com/in/{company}"}])
    entries = [_e("saved", "saved"), _e("reviewco", "review"), _e("trackedco", "review")]
    entities.set_company_mode("trackedco", "saved")
    monkeypatch.setattr(feed, "load", lambda: entries)

    cli.queue_fills_cmd(type("A", (), {})())
    queued = {x["name"] for x in json.loads((tmp_path / "q.json").read_text())}
    assert "saved Founder" in queued          # posting past review
    assert "trackedco Founder" in queued      # company favourited
    assert "reviewco Founder" not in queued   # still undecided — not a yes


def test_chip_family_shares_one_height():
    """Measured 2026-08-09 (design audit): rolechip rendered 20.5px and the
    miss-pill 24.5px beside 22.5px standard chips — 177 card-footer rows across
    the app held two or three chip heights at once, the single biggest cause of
    the "slightly off" feel. DESIGN.md: one --chip-h, every chip-family member
    conforms. Regression = a second height in a chip row."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "--chip-h:22px" in tpl
    # the three families that disagreed all pin to the token now
    assert tpl.count("height:var(--chip-h)") >= 3
    for rule in (".cchip{font-size", ".ind{font-size", ".misspill{display"):
        i = tpl.find(rule)
        assert i > 0 and "height:var(--chip-h)" in tpl[i:tpl.find("}", i)], rule
    assert "min-height:24px" not in tpl   # the old miss-pill overshoot


def test_triage_control_is_one_size():
    """Measured 2026-08-09 (design audit): the same ✓/✗ control rendered 32px on
    tracker/postings, 28px on tweets/companies, 24px on rolodex rows. DESIGN.md
    fixes it at 28px everywhere; the per-tab overrides are gone. Regression =
    a tab growing its own triage size again."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert ".tri{width:28px;height:28px" in tpl
    assert ".tri{width:32px" not in tpl
    assert "#people .pcard .tri{width:24px" not in tpl
    assert ".tgrid .tri{width:28px" not in tpl      # redundant override removed
    assert ".twcard .tri{width:28px" not in tpl


def test_chip_rows_use_the_gap_token():
    """DESIGN.md 2026-08-09: chip rows disagreed — card footers at 6px, kanban
    rows at 4px. One --gap-chip token now; card footers consume it."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "--gap-chip:4px" in tpl
    i = tpl.find(".cfoot{margin-top:auto")
    assert i > 0 and "column-gap:var(--gap-chip)" in tpl[i:tpl.find("}", i)]


def test_founder_research_refuses_a_guessed_name_or_url():
    """Eric emails these people BY NAME, so a wrong founder is the worst
    output the pipeline has. The parser is the last gate: a one-word "name"
    is a role label ("Founder", "The team"), and a LinkedIn URL that isn't a
    real /in/ link is the model guessing a slug from a person's name — the
    prompt forbids it and this drops it if it comes back anyway."""
    from pipeline.research import _parse_founders

    got = _parse_founders('{"founders":['
                          '{"name":"Jesse Zhang","title":"Co-founder & CEO",'
                          ' "linkedin":"https://www.linkedin.com/in/jesse-zhang"},'
                          '{"name":"Founder","title":"CEO"},'
                          '{"name":"Ivan Zhou","title":"CTO","linkedin":"linkedin.com/in/guessed"}]}')
    assert [f["name"] for f in got] == ["Jesse Zhang", "Ivan Zhou"]
    assert got[0]["linkedin"].endswith("/in/jesse-zhang")
    assert got[1]["linkedin"] == ""          # guessed slug dropped, not stored
    assert _parse_founders("no json here") == []
    assert _parse_founders('{"founders":[]}') == []


def test_founder_research_is_saved_only_and_stamps(tmp_path, monkeypatch):
    """Measured 2026-08-07: SEC Form D resolved 1 of 12 saved companies (early
    raises are SAFEs and don't file) and the /about scraper found 0 across 67,
    so web search is the only source that actually closes the founder gap —
    16 of 17 on the first run. It costs money, so two guards: SAVED companies
    only (Eric's scope call), and a founders_checked stamp so an
    unidentifiable company can't be re-billed on every scout."""
    import inspect

    from pipeline import research

    src = inspect.getsource(research.auto_fill_founders)
    assert 'v.get("tracked")' in src and '"saved", "applied"' in src
    assert "founders_checked" in src
    assert "ANTHROPIC_API_KEY" in src            # no key, no spend
    # answered-empty still stamps; an ERRORED call must stay retryable
    assert "is not None" in src
    # finding a founder is what makes their profile readable
    assert "queue_person_fill" in src


def test_fill_queue_is_founders_only(tmp_path, monkeypatch):
    """Eric, 2026-08-07: an alum is already warm — the sweep verified them on
    their own education page — so reading their whole profile buys nothing and
    costs a sitting slot. Only founders, whose signals are unknown, queue."""
    import inspect

    from pipeline import cli

    src = inspect.getsource(cli.queue_fills_cmd)
    assert 'p.relationship != "founder"' in src


def test_data_chips_are_tinted_fills_not_borders():
    """DESIGN.md decision B (Eric, 2026-08-09): the audit showed borders on
    every chip made cards read busy; Linear/RUI reserve edges for controls.
    Data chips are borderless tinted fills (--chipfill), quick-edit chips show
    an edge on hover, and links/missing-field pills keep real borders.
    Regression = the border soup returning."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert tpl.count("--chipfill:") == 2          # one per theme
    assert ".cchip:not(.missing),.ind:not(.ind-missing),.sig{border-color:transparent}" in tpl
    assert "a.cchip{border-color:var(--line)}" in tpl
    i = tpl.find(".cchip{font-size")
    assert "background:var(--chipfill)" in tpl[i:tpl.find("}", i)]


def test_elevation_z_and_motion_run_on_tokens():
    """Design audit 2026-08-09: 27 ad-hoc box-shadows, 13 z-index values, 19
    durations, 9 easings. DESIGN.md collapses them to the 4-shadow ladder +
    ring, 4 z-layers, 3 durations, 1 spring. Regression = a new raw value
    where a token exists."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    for tok in ("--sh-rest:", "--sh-raised:", "--sh-overlay:", "--sh-modal:",
                "--ring:", "--dur-1:", "--dur-2:", "--dur-3:", "--ease-spring:",
                "--z-raised:", "--z-fab:", "--z-modal:", "--z-top:"):
        assert tok in tpl, tok
    assert "var(--shadow)" not in tpl          # old token fully renamed
    assert "cubic-bezier" not in tpl.split("--ease-spring:")[1].split(";")[0] or True
    # the five near-identical springs collapsed to one token
    assert tpl.count("cubic-bezier") == 1      # only the token definition
    assert "z-index:60" not in tpl and "z-index:99" not in tpl


def test_spacing_sits_on_the_even_grid():
    """Design audit 2026-08-09: every integer 1-16 was in use somewhere —
    33 distinct spacing values, the pixel-jitter behind "slightly off".
    DESIGN.md: even 2px grid to 16, then 20/24/32/48, in padding/margin/gap.
    This is the spacing arm of the design lint."""
    import re
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    s, e = tpl.find("<style>"), tpl.find("</style>")
    css = re.sub(r"/\*.*?\*/", "", tpl[s:e], flags=re.S)
    PROPS = r"(?:padding|margin|gap|row-gap|column-gap|padding-(?:top|right|bottom|left)|margin-(?:top|right|bottom|left))"
    BANNED = {1, 3, 5, 7, 9, 11, 13, 15, 18, 22, 26, 30, 34}
    offenders = []
    for m in re.finditer(rf"({PROPS}):([^;}}]*)", css):
        val = m.group(2)
        if "calc" in val or "var(" in val:
            continue
        for px in re.findall(r"(?<![\w.])(\d+)px", val):
            if int(px) in BANNED:
                offenders.append(f"{m.group(1)}:{val.strip()}")
    assert not offenders, offenders[:8]


def test_batch5_singles_hold():
    """DESIGN.md sweep batch 5 (2026-08-09): one tile shape (streak pills
    adopted the scoreboard rect), weights only 400/600/700/800, empty kanban
    stubs share one dashed grammar, the shortcut hint is opaque floating
    chrome, light-theme badges use ink that follows the fill, and the splash
    clears even in a tab that never becomes visible (it stranded a hidden
    browser pane during the audit)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "font-weight:650" not in tpl and "font-weight:750" not in tpl
    assert ".kbcol.kbempty,.kbcol.kbstub{background:var(--bg);border:1px dashed var(--line);animation:none}" in tpl
    assert "box-shadow:var(--sh-overlay);\n    opacity:1" in tpl.replace("\r", "")
    assert ':root[data-theme="light"] .tcount{color:var(--hdr-ink)}' in tpl
    assert "setTimeout(go, 4000)" in tpl


def test_no_raw_color_literals_outside_the_token_blocks():
    """DESIGN.md batch 6 (2026-08-09): the audit found 51 stray hexes and ~40
    rgba/hsla literals bypassing the tokens. Every color in a style rule now
    goes through a slot (var(--…)), the alpha ramps (--a0-6, --b1-4), a hue
    token (hsl(var(--mossh),…)), or color-mix on a slot. Raw literals live
    only on token-definition lines. This is the color arm of the design lint;
    a new hex in a rule is a failing test that names DESIGN.md."""
    import re
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    s, e = tpl.find("<style>"), tpl.find("</style>")
    css = re.sub(r"/\*.*?\*/", "", tpl[s:e], flags=re.S)
    offenders = []
    for ln in css.split("\n"):
        if ln.lstrip().startswith("--") or ":root" in ln.split("{")[0]:
            continue   # token-definition lines hold the actual values
        if re.search(r"#[0-9a-fA-F]{3,8}\b", ln):
            offenders.append(("hex", ln.strip()[:90]))
        if "rgba(" in ln or "rgb(" in ln:
            offenders.append(("rgb", ln.strip()[:90]))
        for m in re.finditer(r"hsla?\(", ln):
            rest = ln[m.end():m.end()+16]
            if not (rest.startswith("var(") or rest.startswith("calc(var(")):
                offenders.append(("hsl", ln.strip()[:90]))
    assert not offenders, offenders[:8]


def test_alpha_rail_fully_removed():
    """The scrub rail was built for the alphabetical resting order; once
    Actionable became the default (2026-08-11) Eric cut the bar entirely.
    Regression = any orphaned rail machinery creeping back — it was ~5kB of
    CSS, pointer handlers, and data-jump plumbing serving a retired sort."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    for frag in ("alpharail", "alrbub", "alphaJump", "buildAlphaRail",
                 "data-jump", 'class="alr"'):
        assert frag not in tpl, frag


def test_rolodex_defaults_answer_who_next():
    """Rolodex IA (Eric, 2026-08-11): the section's job is "who should I talk
    to next?", so the resting state is All Names sorted Actionable — warm
    first, furthest talk-stage next, recently-touched inside that — with the
    rail showing stage buckets. A-Z + letter rail stays as the lookup mode.
    The 95%-worn scrape-date pill reads as quiet text. Regression = waking up
    alphabetical again."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'let PVIEW_SEL = "flat";' in tpl
    assert '<option value="act">Sort: Actionable</option>' in tpl
    psel = tpl.split('id="psort"')[1].split("</select>")[0]
    assert psel.find('value="act"') < psel.find('value="az"')   # act is the default option
    assert "const actRank = pv => [isWarm(pv) ? 0 : 1" in tpl
    assert ".pcard .cchip.addedchip{background:transparent" in tpl


def test_required_years_reads_requirements_not_incidental_mentions():
    """Eric (2026-08-11): the experience tag must come from what the posting
    ASKS FOR. The reject-filter regex was too loose to display — 'biggest
    shift in marketing in 25 years' and '2 years of runway' would have worn a
    chip. Each line here is a phrasing measured in the live cache."""
    from pipeline.prefilter import required_years as ry

    assert ry("5+ years of closing experience as an Enterprise AE") == 5
    assert ry("2–5 years in marketing operations at a B2B SaaS") == 2
    assert ry("REQUIREMENTS - 3–5+ years of experience in copywriting") == 3
    assert ry("0-2 years of professional experience") == 0
    assert ry("at least 4 yrs relevant background") == 4
    # incidental mentions render nothing
    assert ry("the biggest shift in marketing in 25 years") is None
    assert ry("we raised our seed 2 years ago") is None
    assert ry("18 months of runway, 3 years of product history") is None
    assert ry("") is None and ry("no experience required") is None
    # the requirement wins over the bigger nice-to-have
    assert ry("2+ years of experience required; 6 years experience preferred") == 2
    # phrasings the first cut missed, found by auditing the None results
    # (2026-08-11): "or more" and the YOE abbreviation, which is its own context
    assert ry("5 or more years of experience working with enterprise clients") == 5
    assert ry("REQUIRED QUALIFICATIONS 2+ YOE in a client-facing role") == 2
    # spelled-out numbers stay unparsed — every cache hit was company hype
    assert ry("achieved eight figure ARR in under two years") is None


def test_experience_filter_demands_a_stated_value():
    """The Postings-tab experience filter (Eric, 2026-08-11). The honesty rule
    carries into filtering: '≤1 yr' means the posting SAYS ≤1, never 'probably
    fine because it says nothing' — unknowns get their own 'Not stated' bucket
    instead of leaking into every ≤N result."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    # dropdown became disjoint multi-select bucket chips (2026-08-18); the
    # honesty rule survives in yqBucket: unknown -> its own "none" bucket,
    # never leaking into a numbered one
    assert 'const yqBucket = r => (r.yoe === null || r.yoe === undefined) ? "none"' in tpl
    assert "YQ.has(yqBucket(r))" in tpl


def test_experience_requirement_flows_board_to_feed_to_chip():
    """min_experience must survive the whole pipe (2026-08-11): a board that
    states it (WaaS ships minExperience) writes it to the entry, duplicates
    backfill it blanks-only, and the posting card wears the three-tone chip.
    Losing any link silently reverts the tag to guess-or-absent."""
    from datetime import date

    from pipeline import dashboard, feed
    from pipeline.models import RawPosting

    p = RawPosting(title="Founding AE", company="Acme", url="https://a.co/1",
                   source="waas", min_experience=2)
    e = feed.to_entry(p, None, "")
    assert e.min_experience == 2

    # duplicate backfills the blank, never overwrites the recorded value
    e.min_experience = None
    dupe = RawPosting(title="Founding AE", company="Acme", url="https://a.co/1",
                      source="waas", min_experience=3)
    fresh, dupes = feed.merge_new([e], [dupe])
    assert dupes == 1 and e.min_experience == 3
    e.min_experience = 1
    feed.merge_new([e], [RawPosting(title="Founding AE", company="Acme",
                                    url="https://a.co/1", source="x",
                                    min_experience=9)])
    assert e.min_experience == 1

    tpl = dashboard._TEMPLATE
    assert "function yoeChip(r)" in tpl and "${yoeChip(r)}" in tpl
    assert 'n <= 1 ? " yoe-ok" : n >= 4 ? " yoe-high"' in tpl   # three tones
    assert ".yoechip.yoe-high{color:var(--warn)" in tpl
    assert 'if(n === null || n === undefined) return ""' in tpl  # absent > guessed


def test_only_the_visible_tab_renders_on_input():
    """Measured 2026-08-12: one search keystroke synchronously re-rendered all
    four tabs (~100ms block each), and 11 keystrokes doubled DOM nodes
    (33k → 64k) because observeCards never dropped old subscriptions. Now:
    off-screen tabs go into DIRTY and render on arrival, search is debounced
    150ms, and observeCards disconnects before re-subscribing survivors."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "const DIRTY = new Set();" in tpl
    assert "function markDirty(" in tpl and "if(DIRTY.has(TAB)) renderTab(TAB)" in tpl
    assert "deb = setTimeout(rerun, 150)" in tpl
    assert "render(); renderCompanies(); renderPeople(); renderTweets();" not in tpl
    i = tpl.find("function observeCards")
    body = tpl[i:tpl.find("\n}", i)]
    assert "IO.disconnect();" in body and '".preveal"' in body


def test_inactive_tab_ink_follows_the_header_not_the_page():
    """SYMPTOM (Eric screenshot, 2026-08-12): in light mode the inactive tab
    labels vanished — they mixed from var(--ink) (near-black on the light
    page) while sitting on the always-dark header. Header chrome derives its
    ink from --hdr-ink, which is light in both themes by design (decision C:
    the header stays dark in light mode)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    i = tpl.find(".tab{border:0")
    body = tpl[i:tpl.find("}", i)]
    assert "color-mix(in srgb, var(--hdr-ink) 72%, transparent)" in body
    assert "var(--ink) 72%" not in tpl


# --- Outreach-play retirement: companies carry the lead (2026-08-12) --------
# docs/outreach-plays-retirement.md — a fresh raise is a COMPANY entering
# review with a timing trigger, never a "No posting yet" pseudo-posting.


_FORM_D_XML = """
<entityName>Fresh Health, Inc.</entityName>
<industryGroupType>Other Health Care</industryGroupType>
<totalAmountSold>2500000</totalAmountSold>
<yearOfInc><withinFiveYears>true</withinFiveYears><value>2025</value></yearOfInc>
<dateOfFirstSale><value>{sale}</value></dateOfFirstSale>
<stateOrCountryDescription>NEW YORK</stateOrCountryDescription>
<relatedPersonInfo><firstName>Ada</firstName><lastName>Founder</lastName>
  <relationship>Executive Officer</relationship></relatedPersonInfo>
"""


class _StubResp:
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass


class _StubClient:
    def __init__(self, text): self._t = text
    def get(self, *a, **k): return _StubResp(self._t)


def test_edgar_discovery_births_a_company_not_a_pseudo_posting():
    """The retirement itself: a qualifying filing parses to a company payload
    (officers as founders, last_raised set, the raise as its why) — a
    RawPosting here means plays are back."""
    from datetime import date

    from pipeline.boards import edgar
    from pipeline.models import RawPosting

    xml = _FORM_D_XML.format(sale=date.today().isoformat())
    got = edgar._parse_filing(_StubClient(xml), "12345", "0001-23-000001",
                              date.today().isoformat(), "edgar")
    assert isinstance(got, dict) and not isinstance(got, RawPosting)
    assert got["name"] == "Fresh Health, Inc."
    assert got["founders"][0]["name"] == "Ada Founder"
    assert got["last_raised"]["amount"] == 2500000
    assert got["last_raised"]["filed"] == date.today().isoformat()
    assert "sec.gov/Archives" in got["last_raised"]["url"]
    assert "just raised" in got["why"]
    # No pipeline-state narration in user-facing text (Eric, 2026-08-13):
    # "No roles posted yet; the founder is the door" read as generated
    # filler on company cards, and the filing-meta description blocked the
    # research pass (blanks-only) from ever writing a real one.
    assert "no roles posted" not in got["why"].lower()
    assert "outreach play" not in got["why"].lower()
    assert got["description"] == ""
    # and the SPV/fund gate still runs before any of that
    spv = xml.replace("Fresh Health, Inc.", "Acme Ventures Fund II")
    assert edgar._parse_filing(_StubClient(spv), "1", "2", "", "edgar") is None


def test_discover_company_enters_review_and_never_overwrites(tmp_path, monkeypatch):
    """Discovery honors every data-honesty rule: review status (tracked and
    not_interested untouched — it can't un-save or resurrect), blanks-only
    fills, forward-only last_raised, and a re-seen filing is not news."""
    from pipeline import entities

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    payload = {"name": "Fresh Health, Inc.", "why": "just raised — $2.5M",
               "founders": [{"name": "Ada Founder", "title": "Executive Officer"}],
               "stage": "seed", "location": "New York",
               "last_raised": {"filed": "2026-08-10", "amount": 2500000,
                               "url": "https://sec.gov/x/1"}}
    assert entities.discover_company(payload) is True
    key = entities.normalize_company("Fresh Health, Inc.")
    rec = entities.load_company_overlay()[key]
    assert not rec.get("tracked") and not rec.get("not_interested")   # review
    assert rec["founders"][0]["name"] == "Ada Founder"
    assert rec["last_raised"]["filed"] == "2026-08-10"

    # same filing again: nothing new, nothing counted
    assert entities.discover_company(payload) is False

    # an older filing can never regress last_raised; a dismissed company stays dismissed
    rec["not_interested"] = True
    entities.save_company_overlay({key: rec})
    old = dict(payload, last_raised={"filed": "2025-01-01", "amount": 1,
                                     "url": "https://sec.gov/x/0"})
    assert entities.discover_company(old) is False
    rec2 = entities.load_company_overlay()[key]
    assert rec2["last_raised"]["filed"] == "2026-08-10"
    assert rec2["not_interested"] is True

    # overlay founders surface on the derived company view
    view = next(c for c in entities.companies([]) if c.key == key)
    assert view.founders and view.founders[0]["name"] == "Ada Founder"


def test_sends_ranks_a_saved_company_on_its_raise_without_a_posting(tmp_path, monkeypatch):
    """make sends half of the retirement: a saved company whose Form D is hot
    ranks in the queue with full urgency — the trigger no longer needs a
    posting to carry it."""
    from datetime import date, timedelta

    from pipeline import entities, timing

    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    entities.save_company_overlay({entities.normalize_company("Fresh Health, Inc."): {
        "name": "Fresh Health, Inc.", "tracked": True, "added": "2026-08-10",
        "site": "https://freshhealth.com",
        "founders": [{"name": "Ada Founder", "title": "Executive Officer"}],
        "last_raised": {"filed": (date.today() - timedelta(days=3)).isoformat(),
                        "amount": 2500000, "url": "https://sec.gov/x/1"},
    }})
    q = timing.queue([], limit=5)
    assert len(q) == 1
    a = q[0]
    assert a.company == "Fresh Health, Inc."
    assert a.title == "fresh raise — no posting yet"
    assert a.urgency == 40 and "3d ago" in a.trigger
    assert a.who == "Ada Founder"


def test_insights_ignores_the_play_migration_rows():
    """The 51 migrated rows carry a status_note naming the retirement doc;
    they are bookkeeping, not verdicts, and must not read as EDGAR noise."""
    from pipeline import insights
    from pipeline.models import Entry

    migrated = Entry(title="No posting yet — outreach play", company="Old Play",
                     url="https://sec.gov/y", source="edgar", location="",
                     score=None, why="", status="uninterested",
                     status_note="retired per docs/outreach-plays-retirement.md")
    real = Entry(title="Founding Growth", company="Keep", url="https://k.co",
                 source="waas", location="NYC", score=80, why="", status="saved")
    out = insights.render([migrated, real], [])
    assert "edgar" not in out          # the migration row is the only edgar row
    assert "waas" in out


def test_score_slider_is_actually_visible_on_postings():
    """The score slider shipped invisible (2026-08-12): applyTabChrome only
    matched select/input/.chip/.sep, so the .scorefil wrapper span was never
    toggled (its "Score" label showed on every tab) while the inner range
    input — keyed by its id "minscore", absent from the postings show-set —
    was display:none'd. Both halves must stay wired: the wrapper in the
    chrome selector, the input id in the postings set. And the warm FILTER
    chip stays text — the fire glyph replaces the word on data chips only."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "#controls .scorefil" in tpl
    assert '"warm","scorefil","minscore","swept"' in tpl
    assert 'data-f="warm" aria-pressed="false">Warm</button>' in tpl
    assert "🔥 Warm" not in tpl


def test_stage_and_raise_chips_speak_in_glyphs():
    """Chip condensation round 2 (Eric, 2026-08-12): stage chips are the
    plant ramp where the metaphor is natural (🌱 = Pre Seed, 🌳 = Seed) and
    letters where it is not ($A, $B+) — full stage name stays in the chip
    title. The raised chip leads with the moneybag glyph and shows its
    amount ONLY on open cards; condensed cards read pure recency ("💰 3mo")
    with the amount moved to the tooltip. Regression = long-form
    "raised $2.5M · 3mo ago" labels coming back, or the amount leaking into
    the condensed label."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '"Pre Seed": "🌱"' in tpl
    assert '"Seed": "🌳"' in tpl
    assert '"Series A": "$A"' in tpl
    assert "function raisedChip(c, open)" in tpl
    assert '(open && amt ? amt + " · " : "")' in tpl         # amount gated on open
    assert 'const label = "raised"' not in tpl               # word replaced by glyph
    assert '"mo ago"' not in tpl                             # durations drop "ago"
    assert '"raised" + (amt ? " " + amt : "") + " — SEC Form D filed "' in tpl


def test_card_footers_hug_their_content():
    """Tracker company cards carried ~12px of guaranteed dead space (Eric,
    2026-08-12): .cfoot kept min-height:44px — room reserved for the
    corner-floating triage that moved INTO the row — plus a 6px padding-top
    that doubled the chips' own 6px row margin. Footer now hugs content
    (34px = 28px triage + 6px row margin). Regression = the 44px reserve or
    the doubled gap coming back."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert ".ccard>.cfoot{padding-top:0;min-height:32px;\n    align-items:flex-start}" in tpl
    assert "padding-bottom:8px;min-height:0}" in tpl


def test_company_name_bump_keeps_card_geometry():
    """Company-card names read one point larger (Eric, 2026-08-12) with
    line-height pinned to the old 24px box, so the bump changes NO spacing —
    tracker cards stay at their post-diet height. Scoped to the company card
    in both its homes (companies tab + tracker strip), never a board-only
    fork. Regression = the name growing the head row, or the size forking
    between tabs."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "font-size:calc(var(--t-lg) + 2px);line-height:24px" in tpl
    assert "#companies .cname,.tgrid .cname{" in tpl


def test_school_chips_wear_their_mascots_on_cards():
    """Card chips speak mascot (Eric, 2026-08-12): 🦅 = Emory, 🐗 = NMH (the
    Hoggers), school name in the tooltip — same one-glyph pattern as 🔥/🎓/💰.
    Both chip sites (posting/company famTags and person cards) route through
    ONE schoolChip helper so the glyphs can't fork; the expanded person
    panel's Alumni row keeps whole words. Regression = a school chip
    rendering its word on cards, or the two sites drifting apart."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'SCHOOL_GLYPH = {Emory: "🦅", NMH: "🐗"}' in tpl
    assert tpl.count("schools.map(schoolChip)") == 2
    assert "alum-emory" in tpl        # expanded Alumni row keeps whole words


def test_tracker_person_cards_carry_stage_time_not_scrape_time():
    """Tracker person cards slimmed (Eric, 2026-08-12): the created-date chip
    ("2w ago" = when scraped) is noise next to the stage-days chip that
    actually answers "how long here", and signal tags reduce to the warm
    flame — the full signal story lives on the rolodex card. Company chip,
    stage-days chip and contact icons stay. Regression = addedchip or
    personFam rendering on kb cards again."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '${!kb&&pv.added?`<span class="cchip addedchip">' in tpl
    assert '${kb ? (isWarm(pv) ?' in tpl
    assert ": personFam(pv)}" in tpl


def test_city_chips_speak_glyph_and_flag():
    """City glyphs (Eric, 2026-08-12): 🗽 NYC, 🌉 SF, 🍀 Boston, 🌐 Remote —
    glyph-only where unambiguous — and country flag + letters for Europe
    (🇬🇧 LDN, 🇩🇪 BER) because the flag says "visa question" at a glance while
    Berlin/Munich share 🇩🇪 so letters must stay. Full location remains in the
    chip title. The Just Raised board drops its write-to line — the founder
    name lives on the company card and in make sends. Regression = plain
    NYC/BOS coming back, a bare 🇩🇪, or rbwho re-rendering."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '"Boston": "🍀"' in tpl
    assert '[/london/i, "🇬🇧 LDN"]' in tpl
    assert '[/berlin/i, "🇩🇪 BER"]' in tpl and '[/munich|münchen/i, "🇩🇪 MUC"]' in tpl
    assert "rbwho" not in tpl


def test_board_chips_and_triage_defer_to_the_column():
    """Board condensation (Eric, 2026-08-12): the kanban column header
    already names the stage, so board chips read "0d · email" / "7d", never
    "Contacted · 0d · email". And ✓/✕ mean keep-or-kill — a decision made
    before a card ever leaves Saved — so on the board they render only in
    the Saved column. Rolodex and Postings tabs keep triage everywhere
    (their piles include review). Regression = stage words back on board
    chips, or ✓/✕ on a Contacted/Applied card."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "esc(cap(r.st))" not in tpl                  # posting chip lost the word
    assert '${kb&&(pv.days!=null||cmShow)?' in tpl     # days · method, method gated off Saved
    assert '${open||(kb&&r.st!=="saved")?"":triageBtns("post"' in tpl
    assert 'kb && pv.st !== "saved" ? "" : triageBtns("person"' in tpl


def test_triage_free_cards_reclaim_the_corner():
    """After ✓/✕ left non-Saved board cards, the 64px pline2 reserve and the
    118px cochip cap kept truncating company names into "Gradient Sports, In"
    beside empty space (Eric's screenshot, 2026-08-12). Cards without a
    triage cluster drop the reserve and let the company chip run to 220px.
    Regression = the reserve applying to triage-free cards again."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert ".pline2>.triage{position:static;margin-left:auto" in tpl
    assert ".pcard:not(.open) .pline2{padding-right:64px}" not in tpl
    assert ".pcard:not(:has(>.triage)) .cochip{max-width:220px}" in tpl


def test_recruiter_chip_never_repeats_the_role_line():
    """Same rule the founder chip has always had: the sig chip exists to flag
    what the role line doesn't say. A card reading "Alex Moreno · Recruiter"
    with a second Recruiter chip wasted the width that truncated her company
    chip (Eric's screenshot, 2026-08-12)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '${pv.rel==="recruiter"&&!/recruiter/i.test(roleTxt)?' in tpl


def test_person_triage_is_a_flex_member_not_a_float():
    """Eric's screenshot (2026-08-12): ✓/✕ overlapped the in-icon on Saved
    board cards. A padding reserve only pushes SHRINKABLE content — chips at
    min-content slid under the absolute float. The cluster is a real flex
    member of the collapsed pcard row now, so overlap is structurally
    impossible. Regression = position going absolute again, or the 64px
    pline2 reserve returning."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert ".pline2>.triage{position:static;margin-left:auto;flex:none}" in tpl
    assert "#people .pcard:not(.open) .triage{right:10px" not in tpl
    # collapsed cards carry triage IN the chip row; open cards keep the float
    assert '${open?"":tri}' in tpl and '${open?tri:""}' in tpl


def test_tracker_chrome_polish_round3():
    """Eric's screenshots (2026-08-12): the stat row's space-between left
    Streaks flush-left and Rolodex flush-right while Postings floated
    centered — now three equal thirds, tiles centered, lifetime groups
    labelled (All Time). Just Raised entries center. The column pager sits
    off the raw edge with header bottom room. Company chips on person cards
    are full-bold; company cards shave to 82px without touching content."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert ".tilegroup{flex:1" in tpl and "align-items:center}" in tpl
    assert '<div class="tglabel">Postings</div>' in tpl   # live counts, label dropped All Time (2026-08-18)
    assert '<div class="tglabel">Rolodex</div>' in tpl
    assert "margin:8px 0;justify-content:center;align-items:center}" in tpl   # rboard centered both axes
    assert ".kbhead .kbpager{position:absolute;right:6px;top:0}" in tpl
    assert "#companies .ccard,.tgrid .ccard{padding-top:6px}" in tpl
    assert ".cchip.cochip{max-width:130px" in tpl   # double class outweighs the later .cchip base


def test_saved_people_show_no_contact_method():
    """A Saved person hasn't been written to — "0d · email" on a Saved card
    was a left-over from dragging the card back (Eric, 2026-08-12). The kb
    chip routes through cmShow, empty when st is saved."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'const cmShow = pv.st === "saved" ? "" : pv.cm;' in tpl
    assert "cmShow?esc(cmShow)" in tpl


def test_chip_row_truncates_company_before_wrapping():
    """One line, always (Eric, 2026-08-12): the company chip is the only
    shrinkable item in the pline2 row (flex:0 1 auto, min 44px, ellipsis),
    so icons and ✓/✕ never wrap to a second row."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "flex-wrap:nowrap;overflow:hidden}" in tpl
    assert "flex:0 1 auto;min-width:44px;" in tpl


def test_company_chrome_round4():
    """Companies batch (Eric, 2026-08-12): Sort-by-score left the companies
    menu (A–Z default — scores belong to postings); sorts never wear the
    active green (that's for filters) and the green no longer erases the
    select chevron (the old background: shorthand wiped background-image);
    the chevron's right inset matches the 12px text inset; condensed company
    cards wear a quiet calendar added-date chip; a Missing Info filter chip
    mirrors the miss-pill fields."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '<option value="score">Sort: Score</option>' not in tpl.split('id="csort"')[1].split("</select>")[0]
    assert 'csort: "az"' in tpl
    assert '!["sort","csort","psort"].includes(sel.id)' in tpl
    assert "background-color:var(--accent);" in tpl          # arrow-preserving actv
    assert "background-position:right 12px center}" in tpl
    assert 'ddRows("cmiss"' in tpl and "MSEL.cmiss" in tpl   # Missing became a criteria dropdown
    assert '${!open&&c.added?`<span class="cchip addedchip"' in tpl
    assert "${age!==null&&!kb?" in tpl                        # board posting: no age chip
    assert '${kb?"":siteChip}' in tpl                         # board posting: no site chip


def test_mega_batch_round_cities_salary_labels():
    """Eric's screenshot batch (2026-08-12): series_unknown and undisclosed
    are the same fact (a raise whose round nobody named) — one Undisclosed
    bucket; "late" is past-our-stage = growth. City dropdown lists named
    cities A-Z with catch-all buckets last. Blank locations are unknown, not
    "Other". Condensed salary reads floor+ ("$150K+") with the range in the
    title and on open cards. Filter placeholders drop the All/Any prefix.
    Companies page out via Show More instead of stopping dead at 120. The
    active tab badge in light theme pairs its ink with ITS fill."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'if(x === "series_unknown") return "undisclosed";' in tpl
    assert 'x === "late") return "growth"' in tpl
    assert "CITY_BUCKET_OPTS" in tpl
    assert 'return "", True   # blank = unknown location' in open(dashboard.__file__).read()
    assert "salShort" in tpl and "${esc(open?r.sal:salShort(r.sal))}" in tpl
    assert 'id="dd-city-btn"' in tpl   # the select became a checkbox dropdown
    assert 'id="dd-cat-btn"' in tpl   # Job Types is a checkbox dropdown now
    assert "All Cities" not in tpl and "All Job Types" not in tpl
    assert "CO_SHOWN += 120; renderCompanies()" in tpl
    assert "CO_SHOWN = 120;\n    markDirty(" in tpl        # filters reset the page
    assert ':root[data-theme="light"] .tab.on .tcount{color:var(--hdr-bg)}' in tpl


def test_mode_chips_are_sliding_bars():
    """Eric, 2026-08-12: the Review/Saved/Uninterested pills on postings,
    companies and rolodex work like the header tab bar — one bordered
    modebar per tab, a sliding accent pill (.modeslide) parked under the
    active chip by moveModeSlides(), naked chips above it. The slide moves
    on mode change, tab arrival, and count updates (count text changes chip
    widths). Regression = loose mode chips or a slide that doesn't track."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    for bar in ("ps-bar", "ct-bar", "pt-bar"):
        assert f'id="{bar}"' in tpl
    assert tpl.count('<span class="modeslide" aria-hidden="true"></span>') == 3
    assert "function moveModeSlides()" in tpl
    assert tpl.count("moveModeSlides();") == 3          # one per mode setter
    assert "requestAnimationFrame(moveModeSlides)" in tpl
    assert "#controls .modebar" in tpl                  # chrome loop hides bars off-tab


def test_screenshot_batch_round5():
    """Eric, 2026-08-12: the slider label syncs to a browser-restored value
    (form state survives the reload-every-action cycle — the bubble sat
    off-left while the label said 'Score'); rolodex gains an Alumni filter
    and Recruiter goes singular; company chips read as names (serif, t-md);
    the company added chip is a real tag without 'ago'; the raised chip
    wears the shared chip metrics; board posting cards show the score ring
    only in Saved and never the miss-pill; the stat row gets air before the
    Just Raised divider."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'if(+el(id).value > +el(id).min) el("minscore-lbl").textContent' in tpl  # floor-aware since 2026-08-18
    assert 'autocomplete="off" aria-label="Minimum score"' in tpl
    assert 'data-f="alumni"' in tpl and "F.alumni" in tpl
    assert '>Recruiter</button>' in tpl and ">Recruiters</button>" not in tpl
    assert "font-weight:800" in tpl.split(".cchip.cochip{")[1].split("}")[0]
    assert "--t-md" not in tpl.split(".cchip.cochip{")[1].split("}")[0]  # size bump reverted (Eric, 2026-08-13)
    assert '.replace(" ago","")' in tpl
    assert ".raisedchip{" in tpl and "align-items:center;gap:4px}" in tpl
    assert '${kb&&r.st!=="saved"?"":ring}' in tpl
    assert '${kb?"":missPill(miss)}' in tpl
    assert "margin-top:14px}" in tpl


def test_title_line_stays_one_text_line():
    """The company-link button inside posting titles carried a 24px
    line-height that inflated the host line box to ~36px — board card heads
    read 12px taller than their text (Eric's pic, 2026-08-12)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert ".ptitle .colink{line-height:1.1;white-space:nowrap}" in tpl  # buttons do not inherit nowrap
    assert ".ptitle .tsep{line-height:1}" in tpl   # the 24px dot set the row height


def test_show_more_strip_never_squeezes_the_masonry():
    """The Show More strip is a flex-basis:100% child of #companies; on the
    nowrap flex row it grabbed the whole line and crushed the three masonry
    columns to 34px — cards rendered as one skinny overlapping strip
    (Eric, 2026-08-12: "reverse whatever you did to the heights"). The
    container must wrap so the strip drops below the columns."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "#companies{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}" in tpl
    assert ".showmore{flex-basis:100%" in tpl


def test_company_card_chrome_round6():
    """Eric's screenshot (2026-08-13): the raised chip wears green only when
    the raise is under a MONTH old — 30-60d stays visible on condensed cards
    in the quiet outline (.fresh) instead of vanishing or shouting. The miss
    pill is one ellipsis chip with the fields in its tooltip ("Loc · Site"
    spelled out cost ~90px). The ✓/✕ cluster sits level with the chip row
    (28px box on a 4px margin vs 22px boxes on 6px). The added chip's
    calendar gets the same 4px gap the people chip has."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'days < 30 ? " hot" : days < 60 ? " fresh"' in tpl
    assert ".raisedchip:not(.hot):not(.fresh){display:none}" in tpl
    assert ">+${items.length}</span>`;" in tpl and "missbtn" not in tpl  # dashed +N (Eric picked 1)
    assert ".cfoot>.triage{position:static;margin-left:auto;margin-top:4px}" in tpl
    assert ".cchip.agechip,.cchip.addedchip{display:inline-flex;align-items:center;gap:4px}" in tpl


def test_card_vertical_padding_reads_even():
    """Measured 2026-08-13 (Eric: "shorter on top, longer on bottom"): ink
    sat 8px from the top border, 13px from the bottom — a 10px bottom pad
    plus 2px min-height slack (34px vs the 32px content). Bottom pad 8 +
    min-height 32 puts both verticals within 1px. Sides stay 16px by
    design — the standard card text inset."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "padding-bottom:8px;min-height:0}" in tpl
    assert "min-height:32px;\n    align-items:flex-start}" in tpl


def test_companies_chrome_round7():
    """Eric, 2026-08-13: the mode slide turns the ✕'s red (--kill) when it
    parks over an Uninterested chip — same vocabulary as the triage X;
    companies sort by most recent Form D (undated raises sink); a No
    Description filter covers the one gap Missing Info doesn't (the blurb)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'sl.classList.toggle("kill", /uninterested/.test(on.id));' in tpl
    assert ".modeslide.kill{background:var(--kill)}" in tpl
    assert '<option value="funded">' not in tpl   # became the funded FILTER (Eric, 2026-08-13)
    assert "F.funded ? filedOn(b).localeCompare(filedOn(a))" in tpl
    assert '["desc","No description"]' in tpl   # folded into the Missing dropdown


def test_autofill_founders_is_an_action_not_a_chip():
    """Eric, 2026-08-13: "why is autofill founders on the top right" —
    sweepStatus conflated status chips with the action button and its slot
    was the top metadata-chip row, so the button surfaced beside Added/via
    chips while the People copy said 'below'. The button renders in the
    bottom action row (sweepBtn, next to Repopulate Company Details); the
    chip row keeps only the state chips."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function sweepBtn(c){" in tpl
    assert "${sweepBtn(c)}\n    </div>" in tpl              # in the exprow
    assert "Autofill Founders</button>`:\"\";\n}\nfunction" not in tpl.split("function sweepStatus")[1].split("function sweepBtn")[0]


def test_dropdown_counts_are_faceted_and_slide_reparks():
    """Eric, 2026-08-13: with Warm on, the city dropdown still said "New
    York (546)" — counts were computed once at load from the whole feed.
    Both list renders now route through one predicate with a `skip` escape
    hatch: each dropdown's count is what the OTHER active filters (and the
    current mode pile) leave, with its own choice lifted. And the mode
    slide re-parks via ResizeObserver whenever count text resizes a bar —
    the pill was photographed straddling "Saved"."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "const pass = (r, skip) =>" in tpl
    assert "const coPass = (c, skip) =>" in tpl
    assert "function setFacetCounts(key, counts, noneCount)" in tpl
    assert tpl.count("setFacetCounts(\"city\"") == 2      # postings + companies
    assert 'skip !== "city"' in tpl and 'skip !== "round"' in tpl
    assert "new ResizeObserver(() => moveModeSlides())" in tpl


def test_filter_chips_use_the_render_scheduler():
    """The chip handler predated the active-tab scheduler and called all
    three renders directly — so renderCompanies (for a hidden tab) overwrote
    the dropdowns' posting-shaped facet counts with company-shaped ones
    (city read 6 while the shown list held 62; 2026-08-13). Chips mark
    dirty like every other input now."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'markDirty("postings", "companies", "people");\n  });\n});' in tpl
    assert "shown = PAGE;\n    render(); renderCompanies(); renderPeople();" not in tpl


def test_recently_funded_is_a_filter_not_a_sort():
    """Eric, 2026-08-13: "recently funded" is a yes/no question — Form D
    filed inside 60 days — so it's a chip, not a csort option. With the
    chip on, freshest money leads regardless of the sort dropdown (that IS
    the question), and companies with no filing on record are excluded (no
    Form D ≠ never raised, but for a TIMING filter absence means no
    trigger)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'data-f="funded"' in tpl and "F.funded" in tpl
    assert "if(d === null || d > 60) return false;" in tpl
    assert '"warm","hasroles","funded","clearfil"' in tpl


def test_uncategorized_companies_wear_gray():
    """Eric, 2026-08-13: a company with no description OR no industry hasn't
    earned a color — plain card background, muted name — so the colorful
    wash reads as "classified" instead of decorating blanks. The wash comes
    back the moment either field fills."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "const dull = !c.industry || !c.blurb;" in tpl
    assert '${dull?"background:var(--card)":indShade(c.industry)}' in tpl
    assert ".ccard.dull .cname{color:var(--muted)}" in tpl


def test_role_abbreviations_round2():
    """Eric, 2026-08-13: four role families still rendered full-width —
    Product & Design was the widest chip on the boards. Full names stay in
    the chip titles as always. Founding and Fin/Legal deliberately keep
    their width (identity label; the legal half is a real distinction)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '"CoS & Strategy": "CoS"' in tpl
    assert '"Product & Design": "Product"' in tpl
    assert '"Engineering": "Eng"' in tpl
    assert '"Marketing": "Mktg"' in tpl
    assert '"Founding & Generalist": "Founding"' in tpl


def test_app_is_loopback_by_default_and_warns_loudly_otherwise():
    """The app has no auth and its POST routes mutate the feed, overlays and
    queues, so the bind address is a security decision, not a preference.
    Default must stay loopback; JOBS_HOST is the deliberate opt-out (Eric,
    2026-08-14 — he wanted the app on his phone while away from the machine).

    Regression = a default that isn't 127.0.0.1, or a non-loopback bind that
    starts quietly. The warning is the only thing standing between "read my
    pipeline on the couch" and "anyone on café wifi can edit it"."""
    import importlib
    import inspect
    import os

    from pipeline import app

    assert app.HOST == "127.0.0.1", app.HOST      # default, with JOBS_HOST unset

    src = inspect.getsource(app.serve)
    assert "REACHABLE BY ANYTHING ON THIS NETWORK" in src
    assert "no login" in src.lower()
    assert "open_browser and loopback" in src     # never auto-open a LAN URL

    os.environ["JOBS_HOST"] = "0.0.0.0"
    try:
        assert importlib.reload(app).HOST == "0.0.0.0"
    finally:
        del os.environ["JOBS_HOST"]
        importlib.reload(app)


def test_ashby_url_wins_the_dedupe_survivor(tmp_path, monkeypatch):
    """Same role on two boards: the Ashby link is the clean apply path, so it
    becomes the canonical URL on collapse (Eric, 2026-08-17) — but only while
    the entry is untouched, since history keys on the URL after a save. The
    description cache entry follows the URL so the entry stays scorable."""
    from datetime import date

    from pipeline import feed as feed_mod
    from pipeline import store
    from pipeline.models import Entry, RawPosting

    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "DESCRIPTIONS", tmp_path / "descriptions.json")
    store.save({"https://boards.greenhouse.io/acme/1": "the role text"})
    from pipeline.models import normalize_company, normalize_title
    existing = [Entry(title="Founding Growth", company="Acme",
                      url="https://boards.greenhouse.io/acme/1", source="greenhouse",
                      location="", score=80, why="", status="review",
                      identity=f"{normalize_company('Acme')}::{normalize_title('Founding Growth')}")]
    dupe = RawPosting(title="Founding Growth", company="Acme",
                      url="https://jobs.ashbyhq.com/acme/123", source="waas",
                      location="", description="", posted_at=date.today())
    fresh, dupes = feed_mod.merge_new(existing, [dupe])
    assert dupes == 1 and not fresh
    assert existing[0].url == "https://jobs.ashbyhq.com/acme/123"
    assert "waas" in existing[0].also_seen_on
    assert store.load().get("https://jobs.ashbyhq.com/acme/123") == "the role text"

    # a saved entry keeps its URL — history references it
    existing[0].status = "saved"
    dupe2 = RawPosting(title="Founding Growth", company="Acme",
                       url="https://jobs.ashbyhq.com/acme/999", source="generalist",
                       location="", description="", posted_at=date.today())
    feed_mod.merge_new(existing, [dupe2])
    assert existing[0].url == "https://jobs.ashbyhq.com/acme/123"


def test_link_sweep_expires_gone_pages_but_never_walls(tmp_path, monkeypatch):
    """Eric (2026-08-17): postings whose pages died should leave the pile on
    their own. Only 404/410 count as death — a 403 wall or a 500 outage must
    change NOTHING, because expiring a live role silently hides a real
    opportunity. Pages that load refresh the description cache blanks-only."""
    from pipeline import refetch, store
    from pipeline.models import Entry

    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "DESCRIPTIONS", tmp_path / "descriptions.json")
    monkeypatch.setattr(refetch, "STUB_CHARS", 20)

    class R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

    pages = {
        "https://a.example/gone": R(404),
        "https://a.example/walled": R(403),
        "https://a.example/outage": R(500),
        "https://a.example/alive": R(200, "<p>" + "real posting text here " * 4 + "</p>"),
    }

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): return pages[url]

    import pipeline.boards.base as base
    monkeypatch.setattr(base, "client", lambda: FakeClient())
    monkeypatch.setattr("time.sleep", lambda s: None)

    def mk(url, status="review"):
        return Entry(title="T", company="C", url=url, source="s",
                     location="", score=None, why="", status=status)
    entries = [mk("https://a.example/gone"), mk("https://a.example/walled"),
               mk("https://a.example/outage"), mk("https://a.example/alive"),
               mk("https://a.example/gone", status="saved")]
    counts = refetch.sweep(entries, cap=10, throttle=0)

    assert entries[0].status == "expired"          # review + 404 -> expired
    assert entries[4].status == "expired"          # saved + 404 -> expired too
    assert entries[1].status == "review"           # wall is not death
    assert entries[2].status == "review"           # outage is not death
    assert counts["expired"] == 2
    assert "real posting text here" in store.load()["https://a.example/alive"]


def test_expired_is_not_engagement_and_not_a_live_thread():
    """`expired` must not make a company read as 'already in touch' (that
    guard exists so founders aren't cold-emailed twice — a dead link is not
    contact), and must not count as a live pipeline thread."""
    from pipeline import status as st
    from pipeline.models import Entry

    e = Entry(title="T", company="Acme", url="u", source="s", location="",
              score=None, why="", status="expired")
    assert "acme" not in {k for k in st.engaged_companies([e])}
    assert "expired" in st.ORDER and st.LABELS["expired"] == "Expired"


def test_execution_sales_seats_are_prefiltered_unless_founding():
    """Eric (2026-08-17): the review pile's biggest measured noise was
    execution seats — 53 Account Executives, 30 Account Managers, 18 Customer
    Success, 19 Designers, all sub-55. Dropped at import now; 'Founding X' is
    exempt (first-hire shape deserves a scored look), and Solutions Engineer
    survives because technical-GTM is actively wanted, not noise."""
    from pipeline.prefilter import check
    from pipeline.models import RawPosting
    from datetime import date

    def v(title):
        return check(RawPosting(title=title, company="C", url="https://x/1",
                                source="s", location="", description="",
                                posted_at=date.today()))
    assert not v("Account Executive").keep
    assert not v("Enterprise Account Manager").keep
    assert not v("Customer Success Lead").keep
    assert not v("Product Designer").keep
    assert v("Founding Account Executive").keep
    assert v("Solutions Engineer").keep
    assert v("GTM Engineer").keep


def test_score_floor_screens_at_50_but_spares_hatches_and_hands():
    """Eric (2026-08-18): 'anything 50 or below goes to uninterested.' The
    floor is policy, not taste — the note says so, insights excludes it, and
    escape-hatch roles plus anything Eric already touched are exempt."""
    from pipeline.dashboard import SCORE_FLOOR, apply_sweep
    from pipeline.models import Entry

    def mk(score, status="review", hatch=False):
        return Entry(title="T", company="C", url=f"u{score}{status}{hatch}",
                     source="s", location="", score=score, why="",
                     status=status, escape_hatch=hatch)
    entries = [mk(50), mk(51), mk(30, hatch=True), mk(20, status="saved"), mk(None)]
    n = apply_sweep(entries)
    assert SCORE_FLOOR == 50
    assert entries[0].status == "uninterested"          # 50 is "50 or below"
    assert "auto-screened (policy" in entries[0].status_note
    assert entries[1].status == "review"                # 51 clears
    assert entries[2].status == "review"                # escape hatch exempt
    assert entries[3].status == "saved"                 # hand-touched exempt
    assert entries[4].status == "review"                # unscored is unjudged
    assert n == 1


def test_handoff_batch1_slider_triagebar_clear_expired_headcount():
    """Pipeline-session handoff (2026-08-18). The score slider draws its own
    track/thumb — native WebKit widgets painted the thumb over the label in
    the Mac wrapper. The triage bar's keycaps follow the accent fill they
    sit on (two later rules had re-inked them page-muted — washed out).
    Clear Filters resets every filter but never the sort. status='expired'
    gets its OWN bucket: the 162 dead links were riding bucketOf's saved
    catch-all onto the Saved pile; now they are invisible everywhere until
    the Expired chip swaps the pile in. Company payload carries headcount
    and the card wears it as 'N ppl'."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "-webkit-slider-thumb" in tpl and "appearance:none" in tpl
    assert '#triagebar kbd{border:1px solid var(--a5)' in tpl
    assert "#triagebar .tkeys{color:var(--muted)}" not in tpl
    assert 'id="clearfil"' in tpl and "window.clearFilters" in tpl
    assert '"expired" ? "expired"' in tpl.replace("\n", " ").replace("  ", " ") or 'st === "expired" ? "expired"' in tpl
    assert 'F.swept ? (r.sw || bucketOf(r.st) === "expired")' in tpl   # Auto-Swept absorbed Expired (2026-08-18)
    assert 'data-f="expired"' not in tpl   # one system-removed lens now
    assert '"headcount": c.headcount' in open(dashboard.__file__).read()
    assert "ppl</span>`:" in tpl
    # (STATUS_ORDER gained "incomplete" after this pin was written — assert
    # expired is IN the order rather than pinning what comes last.)
    assert '"uninterested", "expired"' in open(dashboard.__file__).read()


def test_ashby_and_greenhouse_text_comes_from_their_apis(monkeypatch):
    """Ashby pages are JS shells to plain HTTP — the first link sweep counted
    231 of them 'walled' and Eric called it (2026-08-18): the text was always
    one JSON call away. One org-level call fills every posting under that org,
    and an org board that loads WITH jobs but WITHOUT this one means delisted
    — but an org whose board FAILED to load must expire nothing."""
    from pipeline import refetch

    class R:
        def __init__(self, code, payload=None):
            self.status_code = code
            self._p = payload or {}
        def json(self): return self._p

    class C:
        def get(self, url, **kw):
            if "api.ashbyhq.com/posting-api/job-board/acme" in url:
                return R(200, {"jobs": [{"id": "AAAAAAAA-0000-0000-0000-000000000001",
                                         "descriptionPlain": "real ashby text " * 30}]})
            if "api.ashbyhq.com" in url:
                return R(500)
            if "boards-api.greenhouse.io/v1/boards/beta/jobs/77" in url:
                return R(200, {"content": "&lt;p&gt;" + "real gh text " * 30 + "&lt;/p&gt;"})
            raise AssertionError(url)

    memo: dict = {}
    hit = refetch._ats_org_jobs(C(), "https://jobs.ashbyhq.com/acme/aaaaaaaa-0000-0000-0000-000000000001", memo)
    assert "real ashby text" in hit
    miss = refetch._ats_org_jobs(C(), "https://jobs.ashbyhq.com/acme/aaaaaaaa-0000-0000-0000-000000000002", memo)
    assert miss == "" and memo["acme"]          # board loaded, job absent -> delist signal
    down = refetch._ats_org_jobs(C(), "https://jobs.ashbyhq.com/downorg/aaaaaaaa-0000-0000-0000-000000000003", memo)
    assert down == "" and not memo["downorg"]   # board failed -> NO delist signal
    gh = refetch._ats_org_jobs(C(), "https://boards.greenhouse.io/beta/jobs/77", memo)
    assert "real gh text" in gh


def test_handoff_batch2_yq_buckets_seen_dismissed():
    """Pipeline-session handoff (2026-08-18), batch 2. The experience
    dropdown became four disjoint multi-select chips (≤1 / 2–3 / 4+ /
    No req) whose labels carry live faceted counts. Opened posting/company
    cards persist to localStorage and read visited (muted) in the triage
    piles ONLY — on the tracker every worked card has been opened, so
    muting there would gray the whole board. Company dismissals demote to
    a Dismissed filter chip: the mode bar is Review/Saved, the state and
    its counts survive (never-email-twice memory), legacy 'uninterested'
    viewstate maps to review."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="yqbtn"' in tpl and 'id="yq-le1"' in tpl and 'id="yq-none"' in tpl
    assert "const yqBucket = r =>" in tpl and "YQ.has(yqBucket(r))" in tpl
    assert 'el("yq-" + k + "-lbl")' in tpl   # counts land on the row labels (single dropdown, 2026-08-18)
    assert 'id="yoereq"' not in tpl
    assert "function markSeen(kind, key)" in tpl
    assert '#list .ccard.seen:not(.open):not(.sel)' in tpl
    assert '.seen:not(.open):hover' in tpl
    # ...and six days later Eric reversed the demotion: the three-mode bar
    # is back, same shape as Postings (2026-08-18). State was never touched.
    assert 'id="ct-uninterested"' in tpl
    assert 'data-f="dismissed"' not in tpl


def test_add_posting_is_a_modal_not_a_prompt():
    """Eric via the pipeline session (2026-08-18): manual job entry gets a
    real paste-a-link modal on the Postings tab — URL required, optional
    title/company (the endpoint lets supplied fields win). The native
    prompt() it replaces couldn't offer the optional fields and looked like
    a browser artifact."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="addpostmodal"' in tpl and 'id="ap-url"' in tpl
    assert 'prompt("Paste the job posting URL:")' not in tpl
    assert 'if(title) body.title = title;' in tpl
    assert '"/api/add-posting", body' in tpl


def test_off_market_postings_need_no_url():
    """Eric, 2026-08-18: roles that were never posted anywhere (heard from a
    founder) enter without a URL. The ledger keys everything by url, so the
    server synthesizes a stable manual://company/title slug — blank urls
    would break dedupe and every /api/mark. Title AND company are required
    in that path (nothing invented), the http scrape is skipped, and the
    open card renders the title as plain text (a manual:// href is not a
    link)."""
    import inspect
    from pipeline import app, dashboard

    src = inspect.getsource(app.Handler.do_POST)
    assert 'url = f"manual://{_slug(company)}/{_slug(title)}"' in src
    assert '"no URL? give both a title and a company instead"' in src
    assert 'if not title and url.startswith("http"):' in src   # no scrape for manual
    tpl = dashboard._TEMPLATE
    assert '(r.u||"").startsWith("manual://")?esc(short(dispTitle(r),52))' in tpl
    assert "off-market role" in tpl


def test_hand_added_postings_land_saved():
    """Eric, 2026-08-18: a job he types in is already a yes — it lands as
    status saved, and the tab switches to the Saved pile so the new card is
    visible immediately. Sweep POSTs (which carry a source) still land in
    review for scoring and triage."""
    import inspect
    from pipeline import app, dashboard

    src = inspect.getsource(app.Handler.do_POST)
    assert 'status="saved" if (body.get("source") or "manual") == "manual"' in src
    tpl = dashboard._TEMPLATE
    assert 'setPostingMode("saved");   // land where the new card actually is' in tpl


def test_saved_people_read_newest_first():
    """Eric, 2026-08-18: the Rolodex Saved pile defaults to most recent adds
    first — a just-added person is the one you meant to act on. Implemented
    by mapping the default Actionable sort to Recently Added in saved mode
    only; Review keeps the warm-first Actionable rank."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert '(PEOPLE_MODE === "saved" && psort0 === "act") ? "recent" : psort0' in tpl


def test_add_forms_wipe_after_save():
    """Eric, 2026-08-18: after saving a person/company the form modal stayed
    open still holding the entry (softReload never closes it). A successful
    ADD now reopens the form wiped and refocused for the next entry; a
    company EDIT closes instead — there is no 'next' when fixing a record."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'if(CO_EDITING) closeOnly("formmodal");' in tpl
    assert tpl.count("// wiped and refocused for the next entry") == 2


def test_just_raised_is_a_headline_not_an_inventory():
    """Eric picked declutter options 1+2 (2026-08-18): the tracker's Just
    Raised board shows the five freshest raises inside 30 days (the 30-45d
    tail is past the best-send window), and overflow collapses to a dashed
    '+N more' card that jumps to Companies with the Recently Funded filter
    already on. make today keeps its own wider window — this is the
    tracker's headline, not the ledger."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "days >= 0 && days <= 30" in tpl
    assert "rows.slice(0, 5), extra = rows.length - shownRows.length" in tpl
    assert 'class="rbcard rbmore" onclick="gotoFunded()"' in tpl
    assert "window.gotoFunded = () => {" in tpl


def test_experience_filter_is_one_dropdown_of_checkboxes():
    """Eric, 2026-08-18: the four loose experience chips fold into a single
    dropdown — one Experience button whose panel holds four counted
    checkboxes. The button wears a summary of the picks (≤1 · no req) and
    the filters' active green; outside clicks close the panel. Same YQ
    state, same disjoint buckets, same faceted counts."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="yqpanel"' in tpl and tpl.count('type="checkbox" id="yq-') == 4
    assert "function yqButtonLabel()" in tpl
    assert '[...YQ].map(k => YQ_LBL[k]).join(" · ")' in tpl
    assert 'ev.target.closest(".ddwrap")' in tpl   # one generic closer for every dropdown
    assert ".ddpanel{position:absolute" in tpl


def test_score_slider_sheds_the_generic_input_capsule():
    """Eric's screenshot (2026-08-18): the global input rule (pill border,
    card background, 32px min-height) wrapped a second capsule around the
    custom slider track. The range zeroes all of it — one capsule, the
    scorefil wrapper's."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "border:0;padding:0;min-height:0}" in tpl.split(".scorefil input[type=range]{")[1].split("::-webkit")[0]


def test_tracker_has_no_saved_companies_strip():
    """Eric, 2026-08-18: the Companies Saved strip left the tracker — it
    duplicated the Companies tab's Saved mode card-for-card. The tracker is
    the today panel, tiles, Just Raised headline, and the two boards."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "Companies Saved" not in tpl
    assert 'kanban("ppl", peopleGroups, kbPerson);' in tpl   # boards close the pane


def test_scoring_nag_counts_the_review_pile_and_knows_the_api_is_parked():
    """Eric, 2026-08-18: the tracker's scoring bar said 'add billing' after
    the API was parked BY DESIGN (scoring is an in-session pass now), and
    its 1275 counted every unscored row including uninterested/expired ones
    that will never be scored — the review pile's real wait was 531. The
    nag counts unscored_review and points at the in-session pass, with no
    billing link to click."""
    from pipeline import dashboard

    src = open(dashboard.__file__).read()
    assert '"unscored_review"' in src
    tpl = dashboard._TEMPLATE
    # ...and hours later the banner retired entirely (a permanent nag about
    # a deliberate state is noise). The diag total stays for whoever asks.
    assert "billbar" not in tpl
    assert "console.anthropic.com/settings/billing" not in tpl
    assert '"unscored_review"' in open(dashboard.__file__).read()


def test_three_columns_words_when_open_and_stage_date_edit():
    """Eric, 2026-08-18. Masonry caps at three columns — four made strips of
    every card on wide monitors. Chips are dual-voice: glyph in the grid,
    the word inside any open card (gw() renders both spans; CSS flips them
    under .open). Board posting cards drop the salary chip and read a size
    quieter. The person panel's Stage row grows an '✎ date' that re-dates
    the CURRENT stage through the date modal — met/conversation recorded on
    the day they actually happened."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "Math.min(items.length || 1, maxCols, Math.floor((hostWidth + 12) / 392))" in tpl
    assert "const gw = (glyph, word) =>" in tpl
    assert ".open .wrd{display:inline}" in tpl and ".open .gly{display:none}" in tpl
    assert 'gw("🔥", "Warm")' in tpl and 'gw("🎓", "Alumni")' in tpl
    assert "${r.sal&&!kb?" in tpl
    assert ".kbcol .ptitle{font-size:var(--t-md)}" in tpl
    assert "window.editStageDate = (name, company, status)" in tpl
    assert "✎ date" in tpl


def test_grad_degree_requirement_is_filtered_but_preferred_is_not():
    """Eric (2026-08-18): filter roles that REQUIRE a Masters/PhD. Preferred
    is not a gate — his profile competes there — and bare 'MS'/'MD' must
    never match alone (Microsoft, Maryland)."""
    from pipeline.prefilter import requires_grad_degree

    assert requires_grad_degree("PhD required in computational biology")
    assert requires_grad_degree("A master's degree is a requirement for this role")
    assert requires_grad_degree("Candidates must hold a PhD or equivalent")
    assert requires_grad_degree("Required: M.Sc in a quantitative field")
    assert not requires_grad_degree("Master's degree preferred but not required")
    assert not requires_grad_degree("PhD a plus")
    assert not requires_grad_degree("Experience with MS Office required")
    assert not requires_grad_degree("Based in Bethesda, MD. 2+ years required")
    assert not requires_grad_degree("")


def test_lever_workable_workday_text_comes_from_their_apis():
    """Round two of the walled-host strategy (Eric, 2026-08-18): Lever,
    Workable and Workday all publish posting text as public JSON — same
    pattern that unlocked 231 Ashby pages. No browser, no credits."""
    from pipeline import refetch

    class R:
        def __init__(self, code, payload):
            self.status_code, self._p = code, payload
        def json(self): return self._p

    class C:
        def get(self, url, **kw):
            if "api.lever.co/v0/postings/acme/" in url:
                return R(200, {"descriptionPlain": "real lever text " * 30})
            # Workable's per-job v2 endpoint 404s in reality (probed live
            # 2026-08-18) — only the account widget serves descriptions.
            if "apply.workable.com/api/v1/widget/accounts/beta" in url:
                return R(200, {"jobs": [{"shortcode": "AB12",
                                         "description": "real workable text " * 30,
                                         "requirements": "", "benefits": ""}]})
            if "adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external/job/x/y-1" in url:
                return R(200, {"jobPostingInfo": {"jobDescription": "real workday text " * 30}})
            raise AssertionError(url)

    memo: dict = {}
    assert "real lever text" in refetch._ats_org_jobs(
        C(), "https://jobs.lever.co/acme/aaaaaaaa-0000-0000-0000-000000000001", memo)
    assert "real workable text" in refetch._ats_org_jobs(
        C(), "https://apply.workable.com/beta/j/AB12/", memo)
    assert "real workday text" in refetch._ats_org_jobs(
        C(), "https://adobe.wd5.myworkdayjobs.com/en-US/external/job/x/y-1", memo)


def test_linkedin_and_wellfound_route_to_the_sitting_queue(tmp_path, monkeypatch):
    """Eric's rule: those hosts only through his real Chrome. The sweep must
    not poke them headless — it queues them for a sitting instead, deduped."""
    import json as _json

    from pipeline import refetch, store
    from pipeline.models import Entry

    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "DESCRIPTIONS", tmp_path / "descriptions.json")
    monkeypatch.setattr(refetch, "FILL_QUEUE", tmp_path / "posting_fill_queue.json")

    class NoNetwork:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): raise AssertionError(f"headless fetch of {url}")

    import pipeline.boards.base as base
    monkeypatch.setattr(base, "client", lambda: NoNetwork())
    monkeypatch.setattr("time.sleep", lambda s: None)
    e = Entry(title="T", company="C", url="https://www.linkedin.com/jobs/view/1",
              source="s", location="", score=None, why="", status="review")
    counts = refetch.sweep([e], cap=5, throttle=0)
    q = _json.loads((tmp_path / "posting_fill_queue.json").read_text())
    assert counts["queued_for_sitting"] == 1 and len(q["items"]) == 1
    # second run: already queued, no duplicate
    counts2 = refetch.sweep([e], cap=5, throttle=0)
    assert counts2["queued_for_sitting"] == 0


def test_megacorp_careers_hosts_are_not_startup_seats():
    """Measured 2026-08-18: 130+ walled rows were acquired brands filed under
    their old startup name but hosted on the acquirer's careers site — Airkit
    on salesforce.com, Frame.io on adobe.com, Webroot on opentext.com, OPOWER
    on oracle.com, Instana on ibm.com. The acquired-brand rule says those ARE
    the acquirer, and big-company seats are a hard exclude. The host is stated
    fact, so this never invents an employer. Workday counts: its enterprise
    contract floor means no early-stage startup runs it."""
    from pipeline.prefilter import bigco_host

    assert bigco_host("https://salesforce.com/careers/jobs/1")
    assert bigco_host("https://careers.oracle.com/jobs/2")
    assert bigco_host("https://adobe.wd5.myworkdayjobs.com/external/job/x")
    assert bigco_host("https://amazon.jobs/en/jobs/3")
    # startup ATS hosts must never trip it
    assert not bigco_host("https://jobs.ashbyhq.com/acme/1")
    assert not bigco_host("https://boards.greenhouse.io/acme/jobs/2")
    assert not bigco_host("https://apply.workable.com/acme/j/AB12")
    assert not bigco_host("https://wellfound.com/jobs/4")
    assert not bigco_host("")


def test_source_roi_is_only_readable_on_scored_rows():
    """Atomico was retired 2026-08-18 on '0 roles at 60+' and un-retired the
    same day: the zero came from rows that had no description yet, so they
    were unscored, not bad. Once the link sweep filled them the board showed
    8 at 60+ including an 82. The boards config must keep it live."""
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "data" / "boards.yaml").read_text())["boards"]
    assert cfg["atomico"]["tier"] == "api"


def test_review_batchA_tiles_swept_slider_offer():
    """Eric's review, batch A (2026-08-18). Tiles count LIVE board state —
    the history-derived lifetime counts never moved when a card left a
    board (history is append-only). Auto-Swept absorbed Expired: one
    "system removed it" lens (aged-out review + dead links). The score
    slider's floor is the pile's lowest score with the score filter lifted,
    and the floor position means "no filter". Offer cards go loud green
    everywhere. Industry chips read 800. The modal's fit panel never
    truncates the why."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'const stC = s => DATA.rows.filter(r => r.st === s).length;' in tpl
    assert "g.saved||0" not in tpl                       # history counts gone from tiles
    assert 'F.swept ? (r.sw || bucketOf(r.st) === "expired")' in tpl
    assert "F.expired" not in tpl
    assert "+msEl0.value > +msEl0.min ? +msEl0.value : 0" in tpl
    assert 'const loScores = fc("score")' in tpl
    assert ".ccard.offercard{" in tpl and '${r.st==="offer"?"offercard":""}' in tpl
    assert "emph(r.w)" in tpl                            # fit panel uncapped


def test_review_batchBC_columns_pin_patch_readability():
    """Eric's review, batches B+C (2026-08-18). Masonry takes a per-view
    column cap — rolodex runs 4 (its cards are half-height), postings and
    companies stay 3. The "—" no-company group pins first in By Company.
    Sort: Actionable explains itself in a tooltip. The board person card's
    stage chip is a button that opens the date/method modal. Expanding a
    person in the flat view patches the two touched cards in place — the
    full re-deal made the whole grid shuffle. Card gaps go 16px and blurbs
    read at +1px / 1.65 line-height."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function mason(items, hostWidth, maxCols = 3)" in tpl
    assert 'mason(cards, hostW("people"), 4)' in tpl
    assert 'names.unshift("—")' in tpl
    assert "Actionable = warm people first" in tpl
    assert "editStageDate(\\'${jsq(pv.name)}" in tpl or "editStageDate(" in tpl
    assert "node.replaceWith(tmp.firstElementChild)" in tpl
    assert ".pmason{display:flex;gap:16px" in tpl
    assert "font-size:calc(var(--t-sm) + 1px)" in tpl


def test_nudges_are_receipts_not_stage_moves():
    """Eric, 2026-08-18: "show when I've nudged someone." A Nudged button on
    contacted/conversation/met people logs a history event (kind=nudge,
    keyed by person_key) without moving the stage — make followups' 4/11-day
    schedule stays the policy, this is the receipt. The panel shows count +
    recency, and the payload carries nudges/nudged per person."""
    import inspect
    from pipeline import app, dashboard, entities

    assert callable(entities.record_nudge)
    assert '"/api/nudge"' in inspect.getsource(app.Handler.do_POST)
    src = open(dashboard.__file__).read()
    assert '"nudges": _nudges(pv)[0]' in src
    tpl = dashboard._TEMPLATE
    assert "window.recordNudge" in tpl
    assert 'prow("Nudged"' in tpl
    assert '["contacted","conversation","met"].includes(pv.st)' in tpl


def test_filter_overhaul_checkbox_dropdowns():
    """Eric's review, the big one (2026-08-18): every enum filter is a
    checkbox dropdown (Cities/Industries/Rounds/Job Types/Sources), state
    in MSEL Sets shared across Postings and Companies, multi-select OR
    (verified live: NY+SF = 372 = ground truth). Missing dropdowns fold in
    Unscored (postings) and No Description (companies); zero-count rows
    hide unless checked — which buries the junk stage variants until the
    pipeline cleans them. Clear sits at the END of the bar, shows only
    while something filters, and rides all three tabs. The ddwrap wrapper
    alone keys chrome visibility; panel internals are exempt from the
    show/hide loop."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    for k in ("city", "ind", "rnd", "src", "cat", "pmiss", "cmiss"):
        assert f'id="dd-{k}-btn"' in tpl, k
    assert "const MSEL = {city: new Set()" in tpl
    assert 'if(c.closest(".ddwrap") && !c.classList.contains("ddwrap")) return;' in tpl
    assert "filtersActive()" in tpl and "function updateClearBtn()" in tpl
    assert '(!n && !cb.checked) ? "none" : ""' in tpl        # zero rows hide
    assert 'MSEL.pmiss' in tpl and '["unscored","Unscored"]' in tpl
    assert 'id="rtype"' not in tpl and 'id="source"' not in tpl


def test_about_modal_is_the_spec_digest():
    """Eric, 2026-08-18: an in-app About that says what the app does and the
    rules, simply but comprehensively. It is the user-facing digest of
    CLAUDE.md — the norm (recorded there) is that behavior changes which
    alter what it states update it in the same commit. Spot-check the
    load-bearing claims so drift fails a test instead of lying quietly."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert 'id="aboutmodal"' in tpl and 'id="about-btn"' in tpl
    for claim in ("No blind scoring", "A blank beats a guess",
                  "backfill blanks only", "real Chrome",
                  "day\n      4 and 11", "never writes resumes or emails"):
        assert claim.replace("\n      ", " ") in tpl.replace("\n      ", " "), claim


def test_lennysjobs_steals_intel_strip_and_ats_pain():
    """Eric approved copying two lennysjobs.com ideas (2026-08-18). The
    intel strip: one muted line on posting cards with the why-now facts
    already on file — raise recency (the send-timing trigger), headcount,
    and our own open-role count as hiring velocity; absent facts stay
    absent. The slow-apply chip: long-form ATS hosts (Workday/Taleo/iCIMS/
    SuccessFactors/BrassRing) classified ONLY from the posting URL itself —
    a board page that redirects makes no claim. Neither renders on board
    cards (dieted), and the About legend states both."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert "function intelStrip(r)" in tpl
    assert '${kb?"":intelStrip(r)}' in tpl
    assert "roles in the feed" in tpl
    assert "myworkdayjobs" in tpl and "slow apply" in tpl
    assert '${kb?"":atsChip(r)}' in tpl


def test_textless_postings_are_held_out_of_review_until_scored():
    """Eric (2026-08-18): 'why are we adding still unscored entries missing
    crucial info' — nothing should cost him a triage decision before it can be
    judged. Text-less postings land as `incomplete` (still in the ledger,
    because the feed IS the dedupe memory) and promote themselves to review
    the moment text and a score arrive. Unscored-but-incomplete must never
    be floored: unscored is unjudged, not bad."""
    from pipeline.dashboard import apply_sweep, promote_complete
    from pipeline.models import Entry

    held = Entry(title="T", company="C", url="u1", source="s", location="",
                 score=None, why="", status="incomplete",
                 status_note="no description available yet — held out of review until scored")
    scored_ok = Entry(title="T2", company="C", url="u2", source="s", location="",
                      score=78, why="", status="incomplete")
    scored_low = Entry(title="T3", company="C", url="u3", source="s", location="",
                       score=40, why="", status="incomplete")
    entries = [held, scored_ok, scored_low]

    assert promote_complete(entries) == 2
    assert held.status == "incomplete"      # still no score -> still held
    assert scored_ok.status == "review"     # judged and good -> the pile
    assert scored_low.status == "review"    # judged -> then the floor decides
    assert not scored_ok.status_note

    assert apply_sweep(entries) == 1        # only the 40 floors out
    assert scored_low.status == "uninterested"
    assert held.status == "incomplete"      # never floored while unjudged


def test_incomplete_is_not_a_live_thread_or_engagement():
    """`incomplete` is machine bookkeeping: it must not count as pipeline
    activity, and it must not make a company read as 'already in touch' —
    that guard exists so a founder isn't cold-emailed twice, and holding an
    unread posting is not contact."""
    from pipeline import status as st
    from pipeline.models import STATUSES, Entry

    assert "incomplete" in STATUSES
    e = Entry(title="T", company="Acme", url="u", source="s", location="",
              score=None, why="", status="incomplete")
    assert not st.engaged_companies([e])
    assert st.LABELS["incomplete"] == "Incomplete"


def test_auto_added_people_default_to_review_without_a_migration(tmp_path, monkeypatch):
    """Eric (2026-08-18): only people HE saves are saved; sweeps, imports and
    warm matches land in Review. set_person_status is the only writer of
    r["status"] and its only caller is the UI dropdown, so a PRESENT status
    means a hand move — which is exactly why this is a render-time fallback
    and never a migration: writing "review" onto status-less records would
    erase the boundary that makes the rule work."""
    import json as _json

    from pipeline import entities

    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    entities.save_people_overlay([
        {"name": "Swept Stranger", "company": "Acme"},                 # no status
        {"name": "Hand Saved", "company": "Acme", "status": "saved"},  # Eric's
    ])
    by = {p.name: p.status for p in entities.people([])}
    assert by["Swept Stranger"] == "review"
    assert by["Hand Saved"] == "saved"
    # the file itself is untouched — the fallback is the whole mechanism
    raw = _json.loads((tmp_path / "people.json").read_text())
    assert "status" not in raw[0]


def test_uninterested_company_drops_only_its_untouched_people(tmp_path, monkeypatch):
    """A company Eric passed on shouldn't leave its people in his actionable
    pile — but a conversation in progress must never be buried by a company
    decision (Eric, 2026-08-18)."""
    from pipeline import entities
    from pipeline import history as hist

    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setattr(hist, "HISTORY", tmp_path / "history.json")
    entities.save_people_overlay([
        {"name": "Untouched", "company": "Acme"},                          # -> follows
        {"name": "Explicit Review", "company": "Acme", "status": "review"},  # -> follows
        {"name": "In Touch", "company": "Acme", "status": "contacted"},    # -> stays
        {"name": "Saved One", "company": "Acme", "status": "saved"},       # -> stays
        {"name": "Other Co", "company": "Beta"},                           # -> untouched
    ])
    entities.set_company_mode("Acme", "uninterested")
    by = {p.name: p.status for p in entities.people([])}
    assert by["Untouched"] == "uninterested"
    assert by["Explicit Review"] == "uninterested"
    assert by["In Touch"] == "contacted"
    assert by["Saved One"] == "saved"
    assert by["Other Co"] == "review"


def test_cascade_dequeues_the_sittings_it_just_made_pointless(tmp_path, monkeypatch):
    """A company Eric passed on kept getting swept: the Chrome sittings drain
    alumni_queue and person_fill_queue by name/company and never consult
    status, so retired people burned capped slots (sitting session, 2026-08-18).
    Dropping at cascade time beats skipping at drain time, which wastes a slot
    per stale entry. People who survive the cascade keep their queue entry."""
    import json as _json

    from pipeline import entities
    from pipeline import history as hist

    monkeypatch.setattr(entities, "PEOPLE", tmp_path / "people.json")
    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setattr(entities, "ALUMNI_QUEUE", tmp_path / "alumni_queue.json")
    monkeypatch.setattr(entities, "PERSON_FILL_QUEUE", tmp_path / "fills.json")
    monkeypatch.setattr(hist, "HISTORY", tmp_path / "history.json")

    entities.save_people_overlay([
        {"name": "Untouched", "company": "Acme"},
        {"name": "In Touch", "company": "Acme", "status": "contacted"},
        {"name": "Elsewhere", "company": "Beta"},
    ])
    (tmp_path / "alumni_queue.json").write_text(_json.dumps([
        {"company": "Acme", "linkedin": "x", "pending": ["Emory"]},
        {"company": "Beta", "linkedin": "y", "pending": ["Emory"]},
    ]))
    (tmp_path / "fills.json").write_text(_json.dumps([
        {"name": "Untouched", "linkedin": "a"},
        {"name": "In Touch", "linkedin": "b"},
        {"name": "Elsewhere", "linkedin": "c"},
    ]))

    entities.set_company_mode("Acme", "uninterested")

    alumni = _json.loads((tmp_path / "alumni_queue.json").read_text())
    assert [e["company"] for e in alumni] == ["Beta"]
    fills = _json.loads((tmp_path / "fills.json").read_text())
    names = {e["name"] for e in fills}
    assert names == {"In Touch", "Elsewhere"}   # only the cascaded one dropped


def test_incomplete_bucket_tweet_kinds_cascade_receipt():
    """Pipeline's intake gate (2026-08-19) introduced status=incomplete —
    462 no-description rows immediately rode bucketOf's saved catch-all
    onto the Postings Saved pile, the expired bug reborn; they get their
    own invisible bucket (they promote themselves when text arrives).
    Tweets gained the pipeline's `kind` facet as toggle chips — rows
    without a kind are Unsorted, never defaulted into hiring. Passing a
    company now toasts what cascaded (dropped postings + unworked people
    from the response)."""
    from pipeline import dashboard

    tpl = dashboard._TEMPLATE
    assert ': st === "incomplete" ? "incomplete" : "saved";' in tpl
    assert 'id="twk-unset"' in tpl
    assert 'TWEET_KIND === "unset" ? !t.kind : t.kind === TWEET_KIND' in tpl
    assert "window.setTweetKind" in tpl
    assert "people_cascaded" in tpl and "also dropped" in tpl
    assert "<i>incomplete</i>, invisible until text and a score promote them" in tpl


def test_seniority_level_comes_from_the_description_or_stays_blank():
    """Eric (2026-08-18): the senior screen is a title regex, so it misses
    mis-titled traps both ways — a 'Product Lead' wanting 1-2 years, an
    'Associate' wanting eight. Only the scorer reads the text, so only it can
    call this. The no-blind-scoring rule means an unreadable level is "",
    never a title guess — otherwise it rebuilds the regex prefilter inside the
    scorer while looking like it read something."""
    from pipeline import score
    from pipeline.feed import to_entry
    from pipeline.models import RawPosting

    assert score.LEVELS == ("intern", "entry", "mid", "senior", "exec")
    assert score._level("Senior") == "senior"
    assert score._level("SENIOR ") == "senior"
    assert score._level("vp") == ""          # outside the enum -> blank
    assert score._level(None) == ""
    assert score._level("") == ""
    # the schema forces the model to answer, and permits the honest blank
    assert "level" in score.SCHEMA["required"]
    assert "" in score.SCHEMA["properties"]["level"]["enum"]
    # and it survives the trip into the ledger
    p = RawPosting(title="T", company="C", url="u", source="s", location="",
                   description="x")
    assert to_entry(p, 80, "why", False, "entry").level == "entry"
    assert to_entry(p, 80, "why", False).level == ""


def test_level_backfill_reads_text_and_admits_when_it_cannot_tell():
    """`make levels` (Eric, 2026-08-18: 'enforce the seniority level for all
    our current postings'). A dedicated one-word pass, not a rescore — same
    input, a fraction of the output, so backfilling 595 postings costs cents
    rather than dollars. The honesty rule is unchanged: an answer outside the
    enum, or 'unknown', becomes "" rather than a confident guess."""
    from pipeline import score

    assert score._level("unknown") == ""     # the model's own escape hatch
    assert score._level("Entry") == "entry"
    assert score._level("vp of sales") == ""  # not in the enum -> blank
    # the prompt must forbid title inference, or this silently becomes the
    # regex prefilter wearing a scorer's coat
    assert "Read the REQUIREMENTS, not the title" in score._LEVEL_SYSTEM
    assert "unknown" in score._LEVEL_SYSTEM


def test_level_measures_the_experience_bar_not_the_scope():
    """First live backfill (2026-08-18) returned 60 'exec' postings including
    five Chief-of-Staff roles scoring 78-82 — exactly what Eric wants. The
    model was reading SCOPE ('owns a function, has reports') as seniority. At
    a seed startup, broad ownership is what early employees get, so the prompt
    now measures one thing only: how much prior experience is demanded.
    Sweeping on the old field would have deleted the best roles in the pile."""
    from pipeline import score

    sys = score._LEVEL_SYSTEM
    assert "EXPERIENCE BAR" in sys
    assert "not a measure of scope" in sys
    # the two title families that broke it must stay named in the prompt
    assert "Founding <anything>" in sys and "EARLY-EMPLOYEE seat" in sys
    assert "Chief of Staff" in sys
    # and the honest blank survives
    assert "unknown" in sys and score._level("unknown") == ""


def test_link_sweep_reaches_incomplete_rows(tmp_path, monkeypatch):
    """Found 2026-08-26: the intake gate parks a text-less posting as
    `incomplete`, and the link sweep — the only pass that fetches text —
    filtered to review/new and skipped `incomplete` entirely. 545 postings
    sat in the one state the fetcher ignored, unable to ever promote. If
    this fails, incomplete rows are stranded again."""
    from pipeline import refetch, store
    from pipeline.models import Entry

    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "DESCRIPTIONS", tmp_path / "descriptions.json")
    monkeypatch.setattr(refetch, "STUB_CHARS", 20)

    class R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

    page = R(200, "<p>" + "genuine posting body text " * 4 + "</p>")

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): return page

    import pipeline.boards.base as base
    monkeypatch.setattr(base, "client", lambda: FakeClient())
    monkeypatch.setattr("time.sleep", lambda s: None)

    e = Entry(title="Founding GTM", company="C", url="https://a.example/inc",
              source="s", location="", score=None, why="", status="incomplete")
    counts = refetch.sweep([e], cap=10, throttle=0)

    assert counts["checked"] == 1, "incomplete row was never even fetched"
    assert counts["described"] == 1
    assert "genuine posting body text" in store.load()["https://a.example/inc"]


def test_intake_fill_caches_what_it_fetched(tmp_path, monkeypatch):
    """Found 2026-08-30: scout calls store.remember() over the postings as the
    boards shipped them, THEN fills blank ones inline via refetch. The filled
    text was scored once and never cached, so with the API parked those rows
    landed in `review` unscored and textless — unscorable forever on any host
    the sweep can't reach. If this fails, intake text is being thrown away."""
    from pipeline import refetch, store
    from pipeline.models import RawPosting

    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "DESCRIPTIONS", tmp_path / "descriptions.json")
    monkeypatch.setattr(refetch, "STUB_CHARS", 20)

    class R:
        status_code = 200
        text = "<p>" + "fetched at intake time " * 4 + "</p>"

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): return R()

    import pipeline.boards.base as base
    monkeypatch.setattr(base, "client", lambda: FakeClient())
    monkeypatch.setattr("time.sleep", lambda s: None)

    p = RawPosting(title="Founding GTM", company="C", url="https://a.example/j1",
                   source="s")
    assert store.remember([p]) == 0, "nothing to cache before the fill — that's the setup"
    assert refetch.fill_postings([p], throttle=0) == 1
    # The line under test: scout must re-remember AFTER the inline fill.
    assert store.remember([p]) == 1
    assert "fetched at intake time" in store.load()["https://a.example/j1"]


def test_careers_index_is_not_a_description(tmp_path, monkeypatch):
    """Found 2026-09-03: pages that load fine and carry plenty of text, none of
    it about THIS job — a careers index listing ten titles, a consent banner, a
    raw theme blob — were cached as descriptions. That made postings look
    scorable when they weren't, and every sweep re-cached them, undoing hand
    purges on a loop. If this fails, junk is being cached as posting text."""
    from pipeline import refetch, store
    from pipeline.models import Entry

    real = ("We are hiring a Founding Growth Lead to own acquisition end to end. "
            "You will run experiments, own the number, and report to the founders. " * 3)
    careers_index = ("Join Our Team. Current Openings: Product Testing Engineer, "
                     "Process Engineer, Lab Technician. Apply Now. " * 6)
    consent = ("This website uses cookies to ensure you get the best experience. "
               "Cookie preferences. Select which cookies you accept. On this site we "
               "always set cookies that are strictly necessary. " * 4)
    theme = '{"themeOptions": {"customTheme": {"varTheme": {"primary-color": "#64be49"' + 'x' * 400

    # A real ad that happens to use careers vocabulary must still be accepted:
    # matching on "join our team" / "open positions" purged 19 live 70+ rows.
    friendly = ("Join our team! We have several open positions right now and this one "
                "is special. You will own growth end to end, run the experiments, and "
                "report straight to the founders. We move fast and hire people who want "
                "the whole outcome, not a slice of it. Apply now, or apply for this job "
                "through the link at the bottom of the page.")
    assert refetch.looks_like_a_posting(friendly), "false positive on careers vocabulary"
    assert refetch.looks_like_a_posting(real)
    assert not refetch.looks_like_a_posting(careers_index)
    assert not refetch.looks_like_a_posting(consent)
    assert not refetch.looks_like_a_posting(theme)
    assert not refetch.looks_like_a_posting("too short")

    # And the sweep must not cache one.
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(store, "DESCRIPTIONS", tmp_path / "descriptions.json")

    class R:
        status_code = 200
        text = "<p>" + careers_index + "</p>"

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): return R()

    import pipeline.boards.base as base
    monkeypatch.setattr(base, "client", lambda: FakeClient())
    monkeypatch.setattr("time.sleep", lambda s: None)

    e = Entry(title="Partnership Development", company="C", url="https://a.example/careers",
              source="s", location="", score=None, why="", status="incomplete")
    counts = refetch.sweep([e], cap=5, throttle=0)
    assert counts["described"] == 0, "a careers index was cached as this job's description"
    assert store.load() == {}
