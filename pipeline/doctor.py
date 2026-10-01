"""`make doctor` — is the pipeline healthy, and if not, which part?

The failure modes this app has actually had are quiet ones: a board changes its
markup and returns zero rows, an API key exists but can't be billed, Chrome
moves and PDFs stop rendering, a JSON file gets corrupted. Each was discovered
mid-task. This is the two-minute check that finds them on purpose instead.

Read-only by design: doctor never fixes, migrates, or writes anything. It
reports, and every failure line says what to do about it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK, WARN, FAIL = "✓", "△", "✗"


def _check(label: str, ok: bool | None, detail: str = "", fix: str = "") -> tuple[str, str]:
    mark = OK if ok else (WARN if ok is None else FAIL)
    line = f"  {mark} {label}"
    if detail:
        line += f" — {detail}"
    if fix and not ok:
        line += f"\n      fix: {fix}"
    return ("ok" if ok else ("warn" if ok is None else "fail")), line


def run(live: bool = False) -> tuple[list[str], int]:
    lines: list[str] = []
    fails = 0

    def add(label, ok, detail="", fix=""):
        nonlocal fails
        state, line = _check(label, ok, detail, fix)
        if state == "fail":
            fails += 1
        lines.append(line)

    # --- data files ---
    lines.append("\nDATA")
    for name, required in (
        ("feed.json", True),
        ("calibration.json", True),
        ("history.json", False),
        ("boards.yaml", True),
        ("master_cv.md", True),
    ):
        p = ROOT / "data" / name
        if not p.exists():
            add(name, None if not required else False, "missing",
                "regenerate via scout/mark, or restore from git")
            continue
        if name.endswith(".json"):
            try:
                json.loads(p.read_text(encoding="utf-8") or "{}")
                add(name, True, f"{p.stat().st_size // 1024}KB, parses")
            except json.JSONDecodeError as e:
                add(name, False, f"CORRUPT: {e}",
                    f"git checkout -- data/{name}  (git history is the backup)")
        else:
            add(name, True, f"{p.stat().st_size // 1024}KB")

    # feed sanity
    try:
        from . import feed as feed_mod

        entries = feed_mod.load()
        n_dupes = len(entries) - len({(e.url or "") for e in entries})
        add("feed integrity", n_dupes == 0,
            f"{len(entries)} entries, {n_dupes} duplicate URLs",
            "duplicate URLs mean dedupe broke — investigate before the next scout")
    except Exception as e:  # noqa: BLE001
        add("feed loads", False, f"{type(e).__name__}: {e}")

    # --- environment ---
    lines.append("\nENVIRONMENT")
    venv = ROOT / ".venv" / "bin" / "python"
    add(".venv", venv.exists(), str(venv) if venv.exists() else "missing", "make setup")

    for mod in ("httpx", "selectolax", "yaml", "anthropic", "dotenv", "openpyxl", "pytest"):
        try:
            __import__(mod)
            add(f"module {mod}", True)
        except ImportError:
            add(f"module {mod}", False, "not importable", "make setup")

    # --- credentials ---
    lines.append("\nCREDENTIALS")
    env_file = ROOT / ".env"
    add(".env", env_file.exists(), "", "cp .env.example .env")
    from dotenv import load_dotenv

    load_dotenv(env_file)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        add("ANTHROPIC_API_KEY", None,
            "not set — scoring runs via the in-session agent backend (works, needs a human)")
    else:
        shaped = key.startswith("sk-ant-") and len(key) > 80
        add("ANTHROPIC_API_KEY shape", shaped, f"{len(key)} chars")
        if live and shaped:
            try:
                import anthropic

                anthropic.Anthropic().messages.create(
                    model="claude-haiku-4-5", max_tokens=8,
                    messages=[{"role": "user", "content": "ok"}],
                )
                add("API billing (live call)", True, "1 tiny call, ~$0.0001")
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                hint = ("credits are in a different org than this key, or none purchased — "
                        "console.anthropic.com → Plans & Billing"
                        if "credit balance" in msg else "check the key in the console")
                add("API billing (live call)", False, msg[:90], hint)
        elif shaped:
            add("API billing", None, "not tested — run with --live to spend ~$0.0001 checking")

    # --- boards (live) ---
    if live:
        lines.append("\nBOARDS (live fetch, small)")
        import yaml as yaml_mod

        from .boards import run_board

        cfg = yaml_mod.safe_load((ROOT / "data" / "boards.yaml").read_text())["boards"]
        for name, bc in cfg.items():
            if bc.get("tier") == "skip":
                continue
            if name == "edgar":
                bc = {**bc, "terms": ["health"], "limit_per_term": 3}
            r = run_board(name, bc)
            ok = bool(r.postings or r.companies) and not r.blocked
            add(f"board {name}", ok,
                (f"{len(r.companies)} companies" if r.companies and not r.postings
                 else f"{len(r.postings)} postings") + (f" ({r.error})" if r.error else ""),
                "markup or API may have changed — see the adapter's docstring")

    # --- scheduling ---
    lines.append("\nSCHEDULING")
    from . import schedule as sch

    add("daily scout", None if not sch.PLIST.exists() else True,
        sch.status().replace("\n", "; "),
        "")

    return lines, fails


def render(live: bool = False) -> str:
    lines, fails = run(live)
    head = "─" * 72
    verdict = "healthy" if fails == 0 else f"{fails} problem(s)"
    return "\n".join(["", head, f"DOCTOR — {verdict}", head] + lines + [""])
