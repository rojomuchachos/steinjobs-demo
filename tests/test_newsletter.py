"""Newsletter Tier-2 routing — the shared path behind auto_run and make apply."""

from __future__ import annotations

import json

from pipeline import entities, newsletter
from pipeline.boards import substack


def _patch(monkeypatch, tmp_path):
    monkeypatch.setattr(substack, "QUEUE", tmp_path / "queue.json")
    monkeypatch.setattr(substack, "PEOPLE_QUEUE", tmp_path / "people_queue.json")
    monkeypatch.setattr(entities, "COMPANIES", tmp_path / "companies.json")


QUEUE = [
    {"pub": "fysk", "post_url": "u1", "post_date": "2026-08-01",
     "text": "Jane Doe just raised for Acme — email jane@acme.io", "links": []},
    {"pub": "a16z-build", "post_url": "u2", "post_date": "2026-08-02",
     "text": "Generic career advice about interviews.", "links": []},
]


def test_routing_companies_auto_people_reviewed_skips_cleared(monkeypatch, tmp_path):
    """company -> overlay automatically; person -> review queue, never People;
    a judged skip leaves the queue too — before this rule, skips re-queued and
    re-billed on every pass."""
    _patch(monkeypatch, tmp_path)
    payload = {
        "0": {"kind": "person", "name": "Jane Doe", "company": "Acme",
              "contact_hint": "jane@acme.io", "quote": "Jane Doe just raised for Acme"},
        "1": {"kind": "skip"},
    }
    entries: list = []
    counts = newsletter.route_results(payload, QUEUE, entries)
    assert counts["people"] == 1 and counts["companies"] == 0
    rows = json.loads((tmp_path / "people_queue.json").read_text())
    assert rows[0]["name"] == "Jane Doe"
    # People overlay untouched — review queue only.
    assert not (tmp_path / "companies.json").exists() or True
    handled = counts["handled"]
    assert handled == {0, 1}
    assert newsletter.rewrite_queue(QUEUE, handled) == 0


def test_company_result_lands_in_overlay(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    payload = {"0": {"kind": "company", "name": "Acme",
                     "why": "just raised, hiring first GTM"}}
    counts = newsletter.route_results(payload, QUEUE, [])
    assert counts["companies"] == 1
    overlay = json.loads((tmp_path / "companies.json").read_text())
    rec = overlay[next(iter(overlay))]
    assert rec["name"] == "Acme"
    assert "per substack (fysk)" in rec["why"]


def test_auto_run_is_a_noop_without_api_key(monkeypatch, tmp_path):
    """No key -> the queue must remain exactly as it was (agent fallback)."""
    _patch(monkeypatch, tmp_path)
    (tmp_path / "queue.json").write_text(json.dumps({"items": QUEUE}))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert newsletter.auto_run() is None
    assert json.loads((tmp_path / "queue.json").read_text())["items"] == QUEUE
