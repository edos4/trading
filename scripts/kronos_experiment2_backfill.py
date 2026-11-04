"""Repair missing Experiment 2 rows for anchors captured before the session's
daily bars were written.

The daily job runs at 17:00 America/New_York, but the post-close bar update can
still be filling the session for hours. Symbols whose bar was not yet present
then got an older anchor, so some anchor dates are short of rows. This script
re-forecasts, as-of each anchor date, only the symbols that have no row for that
anchor. Existing rows are never re-saved, so their settled actuals and accuracy
are preserved.

    .venv/bin/python scripts/kronos_experiment2_backfill.py --dry-run
    .venv/bin/python scripts/kronos_experiment2_backfill.py

Uses the app venv; the GPU forecast runs through .venv-kronos.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.kronos_experiment import HORIZONS, open_experiment_store
from core.kronos_process import ROOT, KronosBusy, run_worker

FORECAST_SCRIPT = ROOT / "scripts" / "kronos_web_forecast.py"
# Keep in sync with web.kronos_history.EXPERIMENT_DAYS.
DAYS = 5
# CPU inference (the local model server can hold most of the GPU) is slow:
# base + fine-tuned for ~100 symbols can run well past 30 minutes.
TIMEOUT_S = 5400


def _anchors_and_rows(store) -> tuple[list[date], dict[tuple[str, date], int]]:
    with store.connect() as con:
        anchors = [row["anchor_date"] for row in con.execute(
            "SELECT DISTINCT anchor_date FROM kronos_experiment2 "
            "ORDER BY anchor_date")]
        present: dict[tuple[str, date], int] = {}
        for row in con.execute(
                "SELECT symbol, anchor_date, COUNT(*) AS n FROM kronos_experiment2 "
                "GROUP BY symbol, anchor_date"):
            present[(row["symbol"], row["anchor_date"])] = int(row["n"])
    return anchors, present


def _universe(store, raw: str | None) -> list[str]:
    if raw:
        return [item.strip().upper() for item in raw.split(",") if item.strip()]
    # The experiment's own universe: every symbol that already has a row. This
    # skips tracked names that can never be forecast (no bars / too little
    # history) instead of retrying them on every anchor.
    with store.connect() as con:
        return [row["symbol"] for row in con.execute(
            "SELECT DISTINCT symbol FROM kronos_experiment2 ORDER BY symbol")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=None,
                    help="comma list to restrict the repair universe")
    ap.add_argument("--anchors", default=None,
                    help="comma list of anchor dates (default: every saved anchor)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    store = open_experiment_store()
    anchors, present = _anchors_and_rows(store)
    if args.anchors:
        wanted = {date.fromisoformat(part.strip())
                  for part in args.anchors.split(",") if part.strip()}
        anchors = [anchor for anchor in anchors if anchor in wanted]
    symbols = _universe(store, args.symbols)
    need = len(HORIZONS)

    saved_rows = 0
    for anchor in anchors:
        missing = [symbol for symbol in symbols
                   if present.get((symbol, anchor), 0) < need]
        if not missing:
            print(f"{anchor}: complete")
            continue
        print(f"{anchor}: {len(missing)} symbol(s) missing — {', '.join(missing)}")
        if args.dry_run:
            continue

        try:
            payload = run_worker(
                FORECAST_SCRIPT,
                ["--symbols", *missing, "--days", str(DAYS),
                 "--as-of", anchor.isoformat()],
                timeout_s=TIMEOUT_S,
                label="exp2-backfill",
            )
        except KronosBusy as exc:
            print(f"  skipped: {exc}")
            continue

        stamp = anchor.isoformat()
        keep: list[dict] = []
        stale: list[str] = []
        for result in payload.get("results", []):
            got = (result.get("anchor") or {}).get("time")
            if got == stamp and (result.get("base") or result.get("finetuned")):
                keep.append(result)
            else:
                stale.append(str(result.get("symbol")))
        if stale:
            print(f"  no {stamp} bar: {', '.join(sorted(stale))}")
        if not keep:
            print("  nothing saved")
            continue

        saved = store.save_payload(
            {**payload, "symbols": [result["symbol"] for result in keep],
             "results": keep},
            saved_by="backfill",
        )
        saved_rows += saved["predictions"]
        print(f"  saved {saved['predictions']} row(s) for "
              f"{len(saved['symbols'])} symbol(s)")

    if args.dry_run:
        return 0

    graded = store.grade()
    summary = store.summary()
    print(f"backfilled {saved_rows} row(s); graded {graded}; "
          f"total {summary['saved']['predictions']}, "
          f"pending {summary['pending']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
