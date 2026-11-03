"""scripts/kronos_confirm_report.py — shadow-mode measurement for the gate.

Reads the paper signal logs (``logs/paper_signals_{us,ph}.jsonl``) and the
paper ledgers (``data/cache/paper_account*.json``), then groups every row that
carries a ``kronos_evidence`` dict by its decision.

Signal counts show how often each bucket fires. Win rate and average R come
from closed paper fills, because a blocked signal has no outcome by design.
Shadow mode is the calibration dataset; ``confirm`` mode only fills the
``confirm`` bucket.

    .venv/bin/python scripts/kronos_confirm_report.py --market all
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backtester import trade_r_multiple
from core.paper_trader import PaperAccount
from core.signal_log_store import load_signal_log

DECISIONS = ("confirm", "oppose", "no_opinion", "unknown")


def _bucket(row: dict) -> str:
    evidence = row.get("kronos_evidence")
    if not isinstance(evidence, dict):
        return ""
    decision = str(evidence.get("decision") or "").strip().lower()
    return decision if decision in DECISIONS else "unknown"


def _log_counts(market: str) -> tuple[dict[str, dict], int]:
    counts: dict[str, dict] = defaultdict(lambda: {"signals": 0, "blocked": 0})
    rows = load_signal_log(market, limit=0)
    tagged = 0
    for row in rows:
        bucket = _bucket(row)
        if not bucket:
            continue
        tagged += 1
        entry = counts[bucket]
        entry["signals"] += 1
        reason = str(row.get("reason") or "").lower()
        if row.get("status") == "rejected" and "kronos" in reason:
            entry["blocked"] += 1
    return counts, tagged


def _trade_stats(market: str) -> dict[str, dict]:
    try:
        account = PaperAccount.load(market=market)
    except Exception as exc:
        print(f"{market.upper()}: paper ledger unavailable ({exc})")
        return {}
    stats: dict[str, dict] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "r": [], "pnl_pct": []}
    )
    for trade in account.closed:
        bucket = _bucket({"kronos_evidence": trade.kronos_evidence})
        if not bucket:
            continue
        entry = stats[bucket]
        entry["trades"] += 1
        pnl_pct = float(trade.pnl_pct or 0.0)
        entry["pnl_pct"].append(pnl_pct)
        if pnl_pct > 0:
            entry["wins"] += 1
        exit_price = trade.exit_price if trade.exit_price is not None else trade.entry_price
        r = trade_r_multiple(trade, exit_price)
        if r is not None:
            entry["r"].append(float(r))
    return stats


def _row(label: str, bucket: str, log: dict, trade: dict) -> str:
    trades = int(trade.get("trades") or 0)
    wins = int(trade.get("wins") or 0)
    r_values = trade.get("r") or []
    avg_r = sum(r_values) / len(r_values) if r_values else float("nan")
    win_rate = 100.0 * wins / trades if trades else float("nan")
    total_r = sum(r_values) if r_values else float("nan")
    return (
        f"{label:5s} {bucket:11s} "
        f"{int(log.get('signals') or 0):>7d} "
        f"{int(log.get('blocked') or 0):>7d} "
        f"{trades:>7d} "
        f"{win_rate:>8.1f}% "
        f"{avg_r:>7.2f} "
        f"{total_r:>8.2f}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", choices=("us", "ph", "all"), default="all")
    args = ap.parse_args(argv)

    markets = ("us", "ph") if args.market == "all" else (args.market,)
    header = (
        f"{'mkt':5s} {'decision':11s} {'signals':>7s} {'blocked':>7s} "
        f"{'trades':>7s} {'win_rate':>9s} {'avg_r':>7s} {'total_r':>8s}"
    )
    print(header)
    print("-" * len(header))
    any_rows = False
    for market in markets:
        logs, tagged = _log_counts(market)
        trades = _trade_stats(market)
        if not tagged:
            print(f"{market.upper():5s} (no kronos_evidence rows)")
            continue
        for bucket in DECISIONS:
            log = logs.get(bucket, {"signals": 0, "blocked": 0})
            trade = trades.get(bucket, {})
            if not log.get("signals") and not trade.get("trades"):
                continue
            any_rows = True
            print(_row(market.upper(), bucket, log, trade))
    if not any_rows:
        print("No Kronos-tagged signals yet. Run paper with KRONOS_GATE_MODE=shadow.")
    print()
    print("blocked = rows rejected by the Kronos gate; trades/win_rate/avg_r are")
    print("closed paper fills in that bucket. Use shadow data for calibration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
