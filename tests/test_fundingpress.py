"""Funding press — the raises Form D never sees (Eric, 2026-08-18)."""

from datetime import date

from pipeline.boards import fundingpress as fp


def test_headline_facts_only_and_the_raise_band_holds():
    """A headline is not a filing: take the company, the amount and the round
    it states, and nothing else. The same MIN/MAX band EDGAR uses keeps growth
    rounds and megadeals out of a pre-seed pipeline."""
    got = fp.parse_headline("Acme secures $3.5 million seed round",
                            "https://example.com/a", when=date(2026, 8, 18))
    assert got["name"] == "Acme"
    assert got["stage"] == "seed"
    assert got["last_raised"]["amount"] == 3_500_000
    assert got["last_raised"]["url"] == "https://example.com/a"
    # no invented facts — a headline never supplies these
    assert got["description"] == "" and got["founders"] == []

    # band: too big and too small are both out
    assert fp.parse_headline("Rillet raises $100M Series C at $1B valuation") is None
    assert fp.parse_headline("Tiny raises $50K from friends") is None
    # a bare number has no unit, so it is not read as millions
    assert fp.parse_headline("Flip raises 22 to expand") is None


def test_non_raises_and_vc_funds_never_enter():
    """Reg D's lesson, repeated here: the same sentence shape is used by funds,
    acquirers and accelerators, and they vastly outnumber operating startups."""
    for headline in (
        "Sequoia raises its ninth fund at $8B",
        "Oakley Capital takes majority stake in Graphwise",
        "Acme acquires Foo for $12M",
        "Bar closes its $30M debt facility",
        "Y Combinator applications now open",
    ):
        assert fp.parse_headline(headline) is None, headline


def test_press_prefixes_are_stripped_including_the_curly_apostrophe():
    """Verified live 2026-08-18: the European feeds ship a CURLY apostrophe,
    so a stripper written for the straight quote left 'Germany’s Flip' as the
    company name. Both must work, along with '<City> startup <Name>' and the
    colon clause TechCrunch and tech.eu use."""
    cases = {
        "Germany’s Flip raises €22 mil": "Flip",
        "Germany's Flip raises €22 mil": "Flip",
        "Berlin-based Acme raises $4M seed": "Acme",
        "Detroit startup Grounded raises $5M": "Grounded",
        "UK startup Foo secures £2M pre-seed": "Foo",
        "Beyond AI scribes: Aisel raises €1.7M": "Aisel",
        "A year after their last raise, Flip raises €22 mil": "Flip",
        "Plain raises $4M seed": "Plain",
    }
    for headline, want in cases.items():
        got = fp.parse_headline(headline)
        assert got and got["name"] == want, (headline, got and got["name"])


def test_a_raise_produces_a_company_never_a_posting():
    """Same rule as EDGAR after the outreach-play retirement: you don't apply
    to a round, you write to the founder. The adapter must return companies
    and leave postings empty."""
    class R:
        status_code = 200
        text = ("<rss><channel><item>"
                "<title>Acme raises $3M seed</title>"
                "<link>https://example.com/a</link>"
                "<pubDate>Tue, 18 Aug 2026 10:00:00 +0000</pubDate>"
                "<description>Acme is a thing.</description>"
                "</item></channel></rss>")

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): return R()

    import pipeline.boards.fundingpress as mod
    orig = mod.client
    mod.client = lambda: C()
    try:
        res = mod.fetch("fundingpress", {"feeds": {"one": "https://x/feed"},
                                         "max_age_days": 3650})
    finally:
        mod.client = orig
    assert res.postings == []
    assert len(res.companies) == 1
    assert res.companies[0]["name"] == "Acme"
