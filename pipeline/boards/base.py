"""Shared HTTP plumbing and the board result envelope.

Every adapter returns a BoardResult. A board that fails is recorded, never
fatal — CLAUDE.md: "If a board blocks scraping, note it and move on."
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..models import RawPosting

# Safari, not Chrome: Rock Health's nginx blocklists the Chrome UA string
# outright (403 on every path) but serves Safari — verified 2026-08-05.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
TIMEOUT = httpx.Timeout(30.0, connect=15.0)


@dataclass
class BoardResult:
    board: str
    postings: list[RawPosting] = field(default_factory=list)
    error: str = ""
    blocked: bool = False  # login-walled or bot-blocked, for the triage summary
    # Companies discovered without a posting to hang them on (EDGAR fresh
    # raises, 2026-08-12 — the outreach-play retirement). Routed into the
    # company overlay by the scout, never into the feed.
    companies: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and not self.blocked


def client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"},
    )


def classify_http_error(exc: Exception) -> tuple[str, bool]:
    """Return (message, blocked). 401/403/429 mean 'they blocked us', not 'we broke'."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403, 429):
            return f"HTTP {code} — blocked or rate-limited", True
        return f"HTTP {code}", False
    if isinstance(exc, httpx.TimeoutException):
        return "timed out", False
    return f"{type(exc).__name__}: {exc}", False
