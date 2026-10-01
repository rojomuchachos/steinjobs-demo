"""Company blurbs must describe the company, never the vacancy."""

from __future__ import annotations

from pipeline.entities import _blurb_from


def test_job_voiced_text_yields_blank_not_a_vacancy_blurb():
    """Dozens of company cards showed "You'll operate as…" / "He's hiring a
    TPM…" as the company description. Job copy with no company-voiced
    sentence must yield "", so the card shows its honest no-description
    state instead."""
    assert _blurb_from(
        "He's hiring a Technical Product Manager (based in Redwood City). "
        "You'll operate as forward-deployed product leader, owning the full "
        "lifecycle of AI-first enterprise solutions.", "8090") == ""
    assert _blurb_from(
        "Own how a payments technology platform gets implemented and operated "
        "across the Asia-Pacific region.", "Acme Technology") == ""


def test_substack_listing_lines_never_become_blurbs():
    """The adapter's own provenance format marks a listing line — it is a job
    listing, not company prose, and used to render on cards verbatim."""
    assert _blurb_from(
        "Abundant(Seed, Agent simulation and RL, SF),Chief of Staff — via "
        "ai-operators (Vol. 030 // AI Operators: 40 Chief of Staff & BizOps "
        "jobs in AI, 2026-07-09)", "Abundant") == ""


def test_company_voiced_sentence_survives_mixed_job_copy():
    """Real ATS text usually opens with the company's own line — keep it,
    drop the you'll-sentences that follow."""
    out = _blurb_from(
        "Adaptional is building AI for insurance, one of the largest "
        "industries globally. You'll own the GTM motion end to end. "
        "The role reports to the CEO.", "Adaptional")
    assert out.startswith("Adaptional is building AI for insurance")
    assert "You'll" not in out and "reports" not in out
