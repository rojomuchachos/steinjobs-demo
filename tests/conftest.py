"""Suite-wide guard: tests must never touch the real data files.

Three funding tests wrote "X filed a Form D" junk under keys u/u1/u2 into
data/history.json on EVERY suite run — it surfaced in the app's Activity log.
This fixture hashes the ledgers before the session and fails loudly if any
changed, so the next unpatched writer is caught at once instead of polluting
quietly for weeks.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parent.parent / "data"
GUARDED = ["feed.json", "history.json", "people.json", "companies.json",
           "calibration.json", "funding.json", "substack_seen.json",
           "substack_queue.json", "substack_people_queue.json", "x_seen.json",
           "x_tweets.json"]


def _digest(name: str) -> str:
    f = DATA / name
    return hashlib.sha256(f.read_bytes()).hexdigest() if f.exists() else "absent"


@pytest.fixture(autouse=True, scope="session")
def real_data_files_stay_untouched():
    before = {n: _digest(n) for n in GUARDED}
    yield
    dirty = [n for n in GUARDED if _digest(n) != before[n]]
    assert not dirty, (
        f"tests wrote to real data files: {dirty} — monkeypatch the module "
        f"path (entities.PEOPLE, hist.HISTORY, …) to a tmp_path instead"
    )
