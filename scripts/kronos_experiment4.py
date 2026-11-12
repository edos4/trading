#!/usr/bin/env python3
"""Start/status Experiment 4 and manage its isolated, self-removing cron block."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import fcntl
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BEGIN = "# --- kronos-experiment4 BEGIN ---"
END = "# --- kronos-experiment4 END ---"


def cron_block(root: Path = ROOT, stop_at: str | None = None) -> str:
    """Poll in any host timezone; Python enforces the two NY session closes."""
    inner = (f"cd {shlex.quote(str(root))} && "
             f"{shlex.quote(str(root / '.venv/bin/python'))} "
             "scripts/kronos_experiment4.py --collect --cron "
             + (f'--stop-at {shlex.quote(stop_at)} ' if stop_at else '') +
             f">> {shlex.quote(str(root / 'logs/kronos_experiment4.log'))} 2>&1")
    return (f"{BEGIN}\n# Arrow +1D/+3D actuals only; self-removes at completion or its frozen deadline.\n"
            f"*/15 * * * * {inner}\n{END}\n")


def merge_cron(existing: str, block: str = "") -> str:
    """Edit only Experiment 4; refuse to damage a malformed marked block."""
    if existing.count(BEGIN) != existing.count(END) or existing.count(BEGIN) > 1:
        raise ValueError("Malformed Experiment 4 cron markers; refusing to change crontab")
    if BEGIN in existing:
        start = existing.index(BEGIN)
        end = existing.index(END) + len(END)
        if end < start:
            raise ValueError("Experiment 4 cron markers are reversed")
        if existing[end:end + 1] == "\n":
            end += 1
        return existing[:start] + block + existing[end:]
    if not block:
        return existing
    return existing + ("\n" if existing and not existing.endswith("\n") else "") + block


def update_cron(install: bool, stop_at: str | None = None) -> None:
    """Preserve all unrelated jobs and environment assignments verbatim."""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode and "no crontab for" not in result.stderr.lower():
        raise RuntimeError(f"Cannot read crontab: {result.stderr.strip()}")
    existing = result.stdout if result.returncode == 0 else ""
    updated = merge_cron(existing, cron_block(stop_at=stop_at) if install else "")
    if existing != updated:
        subprocess.run(["crontab", "-"], input=updated, text=True, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--start", action="store_true")
    action.add_argument('--adopt-arrow-experiment3', action='store_true',
                        help='Move the existing frozen Arrow run into Experiment 4 without inference')
    action.add_argument('--reset', action='store_true', help='Archive/reset only the Arrow Experiment 4 run after new forecasts validate')
    action.add_argument("--collect", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--report", action="store_true", help="Write the daily directional accuracy Markdown report")
    action.add_argument("--remove-cron", action="store_true")
    parser.add_argument("--install-cron", action="store_true")
    parser.add_argument("--cron", action="store_true", help="Remove own job at completion/expiry")
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--universe-file', type=Path, help='Previously captured current-session universe')
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--archive-dir', type=Path)
    parser.add_argument('--stop-at', type=datetime.fromisoformat, help='Frozen cron deadline; checked even if the database is down')
    args = parser.parse_args(argv)
    (ROOT / "logs").mkdir(exist_ok=True)
    with (ROOT / "logs/kronos_experiment4.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if args.remove_cron:
            update_cron(False)
            print("Experiment 4 cron removed; saved results retained.")
            return 0
        from core.arrow_experiment4 import (
            adopt_arrow_experiment3, collect, ny_now, read_experiment, save_direction_report, start,
        )

        # Remove even if the database is unavailable at the hard deadline.
        if args.cron and args.stop_at and ny_now() >= ny_now(args.stop_at):
            update_cron(False)
            print("Experiment 4 collection window ended; cron removed.")
            return 0
        if args.adopt_arrow_experiment3:
            result = adopt_arrow_experiment3()
        elif args.start or args.reset:
            result = start(reset=args.reset, as_of=args.as_of, universe_file=args.universe_file,
                           model_dir=args.model_dir, archive_dir=args.archive_dir)
        else:
            result = collect() if args.collect else read_experiment()
        if args.adopt_arrow_experiment3 or args.start or args.reset or args.collect or args.report:
            report_path = save_direction_report(result, ROOT / "logs/kronos_experiment4_direction_report.md")
            if args.report:
                print(f"Daily directional accuracy report: {report_path}")
        if args.install_cron:
            if result["status"] != "collecting":
                raise ValueError("Install collection only after forecasts are saved and before expiry")
            update_cron(True, result['metadata']['deadline'])
        if args.cron and result["status"] in ('complete','expired_incomplete'):
            update_cron(False)
        if not args.cron or result.get("actual_count") or result["status"] == "complete":
            print(json.dumps({k: result.get(k) for k in
                              ("status", "actual_count", "expected_actual_count", "scores")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
