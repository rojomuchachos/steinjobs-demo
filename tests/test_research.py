"""Company research — identity-guarded web lookups, blanks-only writes."""

from __future__ import annotations

import json

from pipeline import entities, research


def test_parse_strips_citation_markup_and_validates_labels():
    """Live web-search replies embed <cite index=…> tags — they leaked into
    company descriptions on the first live test. Industry must be a known
    label; junk labels are dropped, not stored."""
    raw = ('{"description": "<cite index=\\"1-2\\">SimCare trains clinical skills'
           ' in healthcare</cite>", "industry": "ed-tech", "website": "https://simcare.ai"}')
    out = research._parse(raw)
    assert "<cite" not in out["description"]
    assert out["industry"] == "ed-tech"
    assert out["site"] == "https://simcare.ai"
    assert "industry" not in research._parse('{"description": "long enough to pass the gate", "industry": "vibes"}')


def test_auto_fill_writes_blanks_only_and_needs_a_key(monkeypatch, tmp_path):
    """A hand-written overlay description must never be overwritten by
    research, and without an API key the pass is a silent no-op."""
    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert research.auto_fill([]) is None

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    entities.save_company_overlay({"acme": {"name": "Acme", "description": "hand-written truth"}})

    class V:
        key, name, blurb, why, site, stage = "acme", "Acme", "", "", "", ""
        status, locations, best_score, tracked = "", [], 80, False
    monkeypatch.setattr(entities, "companies", lambda entries: [V()])
    monkeypatch.setattr(research, "research", lambda views, by_key, model=None: {
        "answered": {"acme": {"description": "researched blurb long enough", "industry": "fintech"}},
        "found": {"acme": {"description": "researched blurb long enough", "industry": "fintech"}}})
    counts = research.auto_fill([])
    overlay = json.loads((tmp_path / "companies.json").read_text())
    assert overlay["acme"]["description"] == "hand-written truth"  # blank-only
    assert overlay["acme"]["industry"] == "fintech"                # blank filled
    assert counts["description"] == 0 and counts["industry"] == 1


def test_errored_calls_do_not_stamp(monkeypatch, tmp_path):
    """A failed API run (billing, rate limit) once stamped 303 companies as
    researched, blocking their retry for 30 days. Errored != answered."""
    import json
    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    class V:
        key, name, blurb, why, site, stage = "acme", "Acme", "", "", "", ""
        status, locations, best_score, tracked = "", [], 80, False
    monkeypatch.setattr(entities, "companies", lambda entries: [V()])
    monkeypatch.setattr(research, "research",
                        lambda views, by_key, model=None: {"answered": {}, "found": {}})
    counts = research.auto_fill([])
    overlay = json.loads((tmp_path / "companies.json").read_text())
    assert "research_checked" not in overlay.get("acme", {})
    assert counts["errored"] == 1


def test_score_gate_skips_low_scoring_untracked_companies(monkeypatch, tmp_path):
    """Research spends only where a posting cleared 60 or Eric tracks the
    company — a 40-scoring SDR posting's company gets no web spend."""
    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    class Low:
        key, name, blurb, why, site, stage = "lowco", "LowCo", "", "", "", ""
        status, locations, best_score, tracked = "", [], 40, False
    monkeypatch.setattr(entities, "companies", lambda entries: [Low()])
    assert research.auto_fill([]) is None  # nothing eligible -> no-op


def test_unscored_companies_clear_the_score_gate(monkeypatch, tmp_path):
    """`(best_score or 0) >= 60` read unscored as scored-zero, so a company
    with no description could never be researched — the very pass that would
    have supplied one. 400+ blank cards accumulated before this pin
    (2026-08-13). Unscored means unjudged, not bad."""
    import json
    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    class Unscored:
        key, name, blurb, why, site, stage = "mystco", "MystCo", "", "", "", ""
        status, locations, best_score, tracked = "", [], None, False
    monkeypatch.setattr(entities, "companies", lambda entries: [Unscored()])
    monkeypatch.setattr(research, "research", lambda views, by_key, model=None: {
        "answered": {"mystco": {}},
        "found": {"mystco": {"description": "a real researched description"}}})
    counts = research.auto_fill([])
    overlay = json.loads((tmp_path / "companies.json").read_text())
    assert overlay["mystco"]["description"] == "a real researched description"
    assert counts["description"] == 1

