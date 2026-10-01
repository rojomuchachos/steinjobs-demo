"""Substack ingestion — pinned to the shapes verified live on 2026-08-06.

Each test names its symptom, per the house style: a future break should
explain itself.
"""

from __future__ import annotations

import json
from datetime import date

from pipeline.boards import substack

# The exact listing line observed in AI Operators Vol. 032 (2026-08-06),
# including the utm_source that must NOT survive into the feed URL.
AI_OPERATORS_LINE = (
    '<ul><li><strong><span>Datalab</span></strong>'
    '<span> (Seed, AI for document intelligence, NY), </span>'
    '<a href="https://jobs.ashbyhq.com/datalab/1e37e95b-185b-4d63-8192-ccc39083c465'
    '?utm_source=bYYLn0g813"><span>Founding Business Operations</span></a></li></ul>'
)


def _rss(body: str, link: str = "https://aioperators.substack.com/p/vol-032",
         title: str = "Vol. 032") -> str:
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>t</title>
<item><title>{title}</title><link>{link}</link>
<pubDate>Wed, 06 Aug 2026 12:00:00 GMT</pubDate>
<content:encoded><![CDATA[{body}]]></content:encoded>
</item></channel></rss>"""


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, url, params=None):
        self.calls.append(url)
        return FakeResponse(self.pages[url])


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(substack, "SEEN", tmp_path / "seen.json")
    monkeypatch.setattr(substack, "QUEUE", tmp_path / "queue.json")
    monkeypatch.setattr(substack, "PEOPLE_QUEUE", tmp_path / "people_queue.json")
    monkeypatch.setattr(substack, "_PENDING", {})
    monkeypatch.setattr(substack.time, "sleep", lambda s: None)


CFG = {"pubs": [{"name": "ai-operators", "url": "https://aioperators.substack.com"}]}


def test_verified_listing_line_parses_completely(monkeypatch, tmp_path):
    """The real Vol. 032 line must yield company, title, stage, city — and a
    tracking-free URL, or the same role seen on Ashby directly won't dedupe."""
    _patch_paths(monkeypatch, tmp_path)
    posts = substack.extract_postings(
        AI_OPERATORS_LINE, "substack", "ai-operators", "Vol. 032",
        "https://aioperators.substack.com/p/vol-032", date(2026, 8, 6))
    assert len(posts) == 1
    p = posts[0]
    assert p.company == "Datalab"
    assert p.title == "Founding Business Operations"
    assert p.url == "https://jobs.ashbyhq.com/datalab/1e37e95b-185b-4d63-8192-ccc39083c465"
    assert "utm" not in p.url
    assert p.stage == "seed"
    assert p.location == "NY"
    assert p.posted_at == date(2026, 8, 6)
    assert p.founders == []  # newsletter blurbs never mint people records


def test_a16z_layout_role_in_strong_company_in_heading(monkeypatch, tmp_path):
    """a16z Build inverts the AI Operators layout — role in <strong>, team in
    the anchor, company in an <h4> above. The first live backfill imported
    'Senior Full Stack Engineer' as a COMPANY because of this."""
    _patch_paths(monkeypatch, tmp_path)
    body = (
        '<h4>Base Power</h4><p><em>just raised a monster round</em></p>'
        '<ul><li><strong>GTM Finance Lead</strong> — Finance team, '
        '<a href="https://jobs.ashbyhq.com/base-power/abc123">Enterprise Systems</a>'
        '</li></ul>'
    )
    posts = substack.extract_postings(body, "substack", "a16z-build", "t", "u", None)
    assert len(posts) == 1
    assert posts[0].company == "Base Power"
    assert posts[0].title == "GTM Finance Lead"


def test_ats_slug_arbitrates_when_no_heading_or_strong(monkeypatch, tmp_path):
    """With neither a heading nor a company-shaped <strong>, the ATS URL's
    own slug is the company — it is never wrong about whose board it is."""
    _patch_paths(monkeypatch, tmp_path)
    body = ('<p><strong>Founding BizOps Lead</strong>: '
            '<a href="https://jobs.ashbyhq.com/datalab/xyz">apply directly</a></p>')
    posts = substack.extract_postings(body, "substack", "ai-operators", "t", "u", None)
    assert len(posts) == 1
    assert posts[0].company == "Datalab"
    assert posts[0].title == "Founding BizOps Lead"


def test_section_heading_is_not_a_company(monkeypatch, tmp_path):
    """a16z section labels ('Open roles', 'Build -1 → 0') sit in the heading
    slot — the second live backfill imported them as companies. The company
    must come from a company-site link in the line or the ATS slug instead."""
    _patch_paths(monkeypatch, tmp_path)
    body = (
        '<h4>Build -1 → 0</h4>'
        '<ul><li><p><a href="https://www.linkedin.com/in/someone/">Jane Founder</a>'
        ' just raised a seed for <a href="https://www.eonsats.com/">Eon</a>, hiring '
        'across the board: <a href="https://jobs.ashbyhq.com/EON/role-1">'
        'Founding Ops Lead</a></p></li></ul>'
        '<h4>Top 10 Open Roles</h4>'
        '<ul><li><a href="https://jobs.ashbyhq.com/astranis/xyz">'
        'Electronics Engineering Manager</a></li></ul>'
    )
    posts = substack.extract_postings(body, "substack", "a16z-build", "t", "u", None)
    assert len(posts) == 2
    assert posts[0].company == "Eon"          # company-site link in the line
    assert posts[0].title == "Founding Ops Lead"
    # The same anchor's href is the company website — without it the company
    # card shows 'Website?' forever and complete_companies has nothing to fetch.
    assert posts[0].company_url == "https://www.eonsats.com"
    assert posts[1].company_url == ""         # slug-derived company, no site link
    assert posts[1].company == "Astranis"     # ATS slug — heading is a label
    assert posts[1].title == "Electronics Engineering Manager"


def test_cta_anchors_and_news_headlines_are_not_companies(monkeypatch, tmp_path):
    """Third live backfill: 66 entries carried company='Apply here' (a CTA
    anchor picked as the company-site candidate) and a few got a news headline
    from <strong>. Both must fall through to the ATS slug."""
    _patch_paths(monkeypatch, tmp_path)
    body = (
        '<p><strong>SpaceX is buying Cursor in a $60B stock deal</strong> — '
        'meanwhile <a href="https://mintlify.com/careers">Apply here</a> for '
        '<a href="https://jobs.ashbyhq.com/mintlify/abc">Founding Product Marketing '
        'Manager</a></p>'
    )
    posts = substack.extract_postings(body, "substack", "a16z-build", "t", "u", None)
    assert len(posts) == 1
    assert posts[0].company == "Mintlify"
    assert posts[0].title == "Founding Product Marketing Manager"


def test_ats_company_root_is_a_careers_page_not_a_posting():
    """boards.greenhouse.io/<co> lists every role — importing it as one
    posting would put an unusable URL in the feed."""
    assert not substack._is_job_link("https://boards.greenhouse.io/datalab", "Datalab roles")
    assert substack._is_job_link("https://boards.greenhouse.io/datalab/jobs/123", "BizOps")
    assert not substack._is_job_link("https://wellfound.com/company/thunder-13", "Thunder")
    assert substack._is_job_link("https://wellfound.com/jobs/4446277-founding-growth-lead", "Growth Lead")
    assert not substack._is_job_link("https://jobs.ashbyhq.com/EliseAI/x", "careers")


def test_prose_lead_without_job_link_is_queued_not_imported(monkeypatch, tmp_path):
    """'X is hiring, email the founder' with no ATS link is a Tier-2 lead —
    it must reach the extraction queue, never the feed."""
    _patch_paths(monkeypatch, tmp_path)
    body = ("<p>Acme Robotics is hiring their first growth person — no posting "
            "yet, reach out to the founder directly at @acmefounder on X.</p>")
    posts = substack.extract_postings(body, "substack", "nextplay", "t", "u", None)
    leads = substack.extract_leads(body, "nextplay", "t", "u", None)
    assert posts == []
    assert len(leads) == 1
    assert "Acme Robotics" in leads[0]["text"]
    added = substack.queue_leads(leads)
    assert added == 1
    assert substack.queue_leads(leads) == 0  # re-queueing the same post is a no-op


def test_paragraph_with_job_link_is_not_double_captured(monkeypatch, tmp_path):
    """A listing line matching Tier 1 must not ALSO become a Tier-2 lead —
    the queue would fill with items the feed already has."""
    _patch_paths(monkeypatch, tmp_path)
    body = ('<li>Datalab is hiring their first business operations person — '
            'seed stage, NY, apply directly: '
            '<a href="https://jobs.ashbyhq.com/datalab/1e37e95b">Founding Business '
            'Operations</a></li>')
    leads = substack.extract_leads(body, "ai-operators", "t", "u", None)
    assert leads == []


def test_seen_memory_is_idempotent_and_commits_after_the_feed(monkeypatch, tmp_path):
    """Run 1 finds the posting; the seen file must stay UNWRITTEN until
    commit_seen() (the funding.py ordering lesson — a crash between fetch and
    feed.save must not mark posts seen whose finds were never recorded).
    Run 2 after commit finds nothing."""
    _patch_paths(monkeypatch, tmp_path)
    pages = {"https://aioperators.substack.com/feed": _rss(AI_OPERATORS_LINE)}
    monkeypatch.setattr(substack, "client", lambda: FakeClient(pages))

    r1 = substack.fetch("substack", CFG)
    assert len(r1.postings) == 1
    assert not (tmp_path / "seen.json").exists()  # staged, not written

    assert substack.commit_seen() == 1
    assert (tmp_path / "seen.json").exists()

    r2 = substack.fetch("substack", CFG)
    assert r2.postings == []
    assert r2.ok


def test_dead_pub_never_sinks_the_board(monkeypatch, tmp_path):
    """One pub 404ing must not lose the other pubs' finds."""
    _patch_paths(monkeypatch, tmp_path)

    class Client404(FakeClient):
        def get(self, url, params=None):
            if "deadpub" in url:
                raise RuntimeError("boom")
            return super().get(url, params)

    pages = {"https://aioperators.substack.com/feed": _rss(AI_OPERATORS_LINE)}
    monkeypatch.setattr(substack, "client", lambda: Client404(pages))
    cfg = {"pubs": [{"name": "dead", "url": "https://deadpub.substack.com"}] + CFG["pubs"]}
    r = substack.fetch("substack", cfg)
    assert len(r.postings) == 1


def test_backfill_skips_paywalled_posts(monkeypatch, tmp_path):
    """audience=only_paid posts have no readable body — they must be counted
    and marked seen, never fetched."""
    _patch_paths(monkeypatch, tmp_path)
    base = "https://aioperators.substack.com"
    archive = [
        {"canonical_url": f"{base}/p/free-one", "slug": "free-one",
         "post_date": "2026-08-01T00:00:00", "audience": "everyone", "title": "Free"},
        {"canonical_url": f"{base}/p/paid-one", "slug": "paid-one",
         "post_date": "2026-07-20T00:00:00", "audience": "only_paid", "title": "Paid"},
        {"canonical_url": f"{base}/p/ancient", "slug": "ancient",
         "post_date": "2020-01-01T00:00:00", "audience": "everyone", "title": "Old"},
    ]

    class ArchiveClient(FakeClient):
        def get(self, url, params=None):
            self.calls.append(url)
            if url.endswith("/api/v1/archive"):
                return type("R", (), {"raise_for_status": lambda s: None,
                                      "json": lambda s: archive if not params.get("offset") else []})()
            if url.endswith("/api/v1/posts/free-one"):
                return type("R", (), {"raise_for_status": lambda s: None,
                                      "json": lambda s: {"body_html": AI_OPERATORS_LINE}})()
            raise AssertionError(f"unexpected fetch: {url}")

    fake = ArchiveClient({})
    monkeypatch.setattr(substack, "client", lambda: fake)
    postings, done, paid = substack.backfill("substack", CFG, months=3)
    assert done == 1 and paid == 1
    assert len(postings) == 1
    # The paywalled body must never be fetched — only its archive row is read.
    assert not any(c.endswith("/api/v1/posts/paid-one") for c in fake.calls)
