"""Import a hand-built shortlist spreadsheet into the feed and the calibration set.

Eric maintains shortlists by hand (Job_Shortlist_Tracker.xlsx: 154 listings,
each bucketed Top match / Worth a look / Stretch-senior / Screened out, with a
one-line fit verdict). That file is worth two different things:

  1. Feed content — real, vetted postings with stage, comp, YOE and location
     already filled in, which the boards mostly don't give us.

  2. Calibration signal — and this is the more valuable half. Those buckets are
     Eric's own judgments on 154 real postings. Seeded into data/calibration.json
     they become worked examples in the scorer's prompt, so the scorer starts
     calibrated to his actual taste instead of to the rubric alone. The
     screened-out rows matter most: knowing what he rejects, and why, is what
     stops the scorer marking everything 80.

Expected columns: Bucket, Company, Role, URL, Status, Funding/Stage/Size, Pay,
YOE, Location, Fit verdict. Unknown columns are ignored; missing ones are blank.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .models import Entry, normalize_company, normalize_title, today

ROOT = Path(__file__).resolve().parent.parent
CALIBRATION_PATH = ROOT / "data" / "calibration.json"

# Bucket -> (feed status, indicative score, include in feed?)
#
# The scores are Eric's bucketing translated to the 0-100 scale, NOT model
# output. They're recorded so the feed sorts sensibly and so `make status` has
# something to rank on; `why` carries his verbatim verdict so the provenance is
# never ambiguous.
BUCKETS = {
    "top match":           ("saved", 85, True),
    "worth a look":        ("review",       70, True),
    "stretch/senior":      ("review",       55, True),
    "screened out":        ("uninterested", 25, True),
    "closed/unverifiable": ("uninterested", None, False),
    "duplicate":           ("uninterested", None, False),
    "board":               ("uninterested", None, False),  # a search URL, not a job
}

_STAGE_PATTERNS = [
    (re.compile(r"pre-?seed", re.I), "pre_seed"),
    (re.compile(r"\bseed\b", re.I), "seed"),
    (re.compile(r"series\s*a\b", re.I), "series_a"),
    (re.compile(r"series\s*b\b", re.I), "series_b"),
    (re.compile(r"series\s*[c-f]\b", re.I), "series_c"),
    (re.compile(r"\bYC\s*[WSF]\d{2}\b", re.I), "seed"),
    (re.compile(r"public|scale|\$1B", re.I), "growth"),
]


def _stage(text: str) -> str:
    for pat, label in _STAGE_PATTERNS:
        if pat.search(text or ""):
            return label
    return ""


@dataclass
class ImportStats:
    rows: int = 0
    added: int = 0
    skipped_bucket: int = 0
    skipped_dupe: int = 0
    calibration: int = 0


def read_rows(path: Path) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    # The listings sheet is whichever has a "Bucket" column.
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        hdr = [str(h).strip() if h else "" for h in rows[0]]
        if "Bucket" not in hdr:
            continue
        return [
            {k: ("" if v is None else str(v).strip()) for k, v in zip(hdr, r)}
            for r in rows[1:]
            if any(r)
        ]
    raise ValueError(f"no sheet with a 'Bucket' column in {path}")


def to_entry(row: dict, source: str) -> Entry | None:
    bucket = (row.get("Bucket") or "").strip().lower()
    status, score, include = BUCKETS.get(bucket, ("review", None, True))
    if not include:
        return None

    title, company = row.get("Role", ""), row.get("Company", "")
    if not title or not row.get("URL"):
        return None

    funding = row.get("Funding/Stage/Size", "")
    verdict = row.get("Fit verdict", "").strip()

    e = Entry(
        title=title,
        company=company,
        url=row["URL"],
        source=source,
        location=row.get("Location", ""),
        score=score,
        why=verdict or f"imported from shortlist — bucket: {bucket}",
        funding=funding,
        headcount="",
        date_added=today(),
        status=status,
        status_note=f"shortlist bucket: {row.get('Bucket', '')}",
        last_touched=today(),
        first_seen=today(),
        identity=f"{normalize_company(company)}::{normalize_title(title)}",
        stage=_stage(funding),
    )
    return e


def to_calibration(row: dict) -> dict | None:
    """Every row becomes a labeled example — including the screened-out ones."""
    bucket = (row.get("Bucket") or "").strip()
    if bucket.lower() in ("duplicate", "board"):
        return None
    if not row.get("Role") or not row.get("Company"):
        return None
    return {
        "title": row["Role"],
        "company": row["Company"],
        "verdict": bucket,
        "why": row.get("Fit verdict", ""),
        "stage_note": row.get("Funding/Stage/Size", ""),
        "pay": row.get("Pay", ""),
        "yoe": row.get("YOE", ""),
        "location": row.get("Location", ""),
        "recorded": today(),
        "source": "Job_Shortlist_Tracker",
    }


def load_calibration() -> list[dict]:
    if not CALIBRATION_PATH.exists():
        return []
    return json.loads(CALIBRATION_PATH.read_text(encoding="utf-8") or "[]")


def save_calibration(records: list[dict]) -> None:
    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_PATH.write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
