"""`make schedule` — run the scout every morning without being asked.

macOS launchd, not cron: launchd fires missed calendar jobs on wake, so a
laptop that was asleep at 8am still scouts when it opens. Cron would just skip
the day.

Everything the scheduled run needs was already built for other reasons — this
module is deliberately the boring last step:

  - dedupe means a daily run only adds what's genuinely new
  - the agent-backend fallback means a missing/broken API key degrades to
    writing candidates.json instead of failing or silently spending
  - the spend guard caps an API run even if a key works
  - the dashboard rebuilds itself after the feed changes

Each run logs to data/logs/scout-YYYY-MM-DD.log, and `make today` reads fresh
data the next time you open a terminal. Nothing is sent anywhere; a scheduled
run has exactly the powers a manual `make scout` has.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = "com.ericsteinberg.jobpipeline.scout"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOGS = ROOT / "data" / "logs"


def plist_contents(hour: int, minute: int) -> dict:
    runner = ROOT / ".venv" / "bin" / "python"
    return {
        "Label": LABEL,
        # Run via bash -lc so the working directory and env resolve the same
        # way they do in a terminal; .env is read by the CLI itself.
        "ProgramArguments": [
            "/bin/bash",
            "-lc",
            f'cd "{ROOT}" && "{runner}" -m pipeline.cli scout '
            f'>> "{LOGS}/scout-$(date +%Y-%m-%d).log" 2>&1',
        ],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
    }


def install(hour: int = 8, minute: int = 0) -> str:
    LOGS.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist_contents(hour, minute)))

    # bootout first so re-running `make schedule` updates cleanly.
    subprocess.run(
        ["launchctl", "bootout", f"gui/{_uid()}", str(PLIST)],
        capture_output=True,
    )
    r = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{_uid()}", str(PLIST)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}"
    return (
        f"scheduled: daily scout at {hour:02d}:{minute:02d}\n"
        f"  plist: {PLIST}\n"
        f"  logs : {LOGS}/scout-YYYY-MM-DD.log\n"
        "  runs on wake if the machine was asleep; remove with `make unschedule`"
    )


def uninstall() -> str:
    r = subprocess.run(
        ["launchctl", "bootout", f"gui/{_uid()}", str(PLIST)],
        capture_output=True,
        text=True,
    )
    existed = PLIST.exists()
    if existed:
        PLIST.unlink()
    if not existed and r.returncode != 0:
        return "nothing was scheduled"
    return "unscheduled — the daily scout will no longer run"


def status() -> str:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    loaded = LABEL in r.stdout
    lines = [f"plist present: {PLIST.exists()}", f"loaded in launchd: {loaded}"]
    if LOGS.exists():
        logs = sorted(LOGS.glob("scout-*.log"))
        if logs:
            last = logs[-1]
            lines.append(f"last log: {last.name} ({last.stat().st_size} bytes)")
    return "\n".join(lines)


def _uid() -> int:
    import os

    return os.getuid()
