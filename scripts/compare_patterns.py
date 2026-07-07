#!/usr/bin/env python3
"""
scripts/compare_patterns.py — Backtest each pattern individually and compare.

Usage:
    python scripts/compare_patterns.py               # 50 symbols (fast)
    python scripts/compare_patterns.py --symbols 20  # Quick sniff test
    python scripts/compare_patterns.py --symbols 100 # Full comparison
"""

import argparse
import asyncio
import importlib
import os
import pkgutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import patterns as patterns_pkg
from config import settings
from core.backtester import Backtester
from data.tv_client import TVClient
from patterns.base_pattern import BasePattern
from utils.logger import log


def discover_pattern_names() -> list[str]:
    names: list[str] = []
    for module_info in pkgutil.iter_modules(patterns_pkg.__path__):
        if module_info.name.startswith("_") or module_info.name == "base_pattern":
            continue
        module = importlib.import_module(f"patterns.{module_info.name}")
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BasePattern)
                and attr is not BasePattern
            ):
                names.append(attr().name)
    return sorted(names)


async def run_one(pattern_name: str, symbols: list[str]) -> tuple[str, int, int, int, int, float, float, float, float, float, list]:
    bt = Backtester(
        symbols,
        min_confidence=0.70,
        regime_filter=True,
        cooldown_bars=20,
        txn_cost_pct=0.001,
        position_sizing="risk",
        account_value=100_000.0,
        risk_per_trade_pct=0.02,
        trailing_activation_default=None,
        max_open_positions=settings.max_open_positions,
        min_hold_bars=2,
        pattern_filter=pattern_name,
    )
    r = await bt.run()
    return (
        pattern_name,
        r.total_signals,
        len(r.trades),
        r.win_count,
        r.loss_count,
        r.win_rate,
        r.total_pnl_pct,
        r.account_weighted_pnl_pct,
        r.avg_pnl_pct,
        r.max_drawdown_pct,
        r.sharpe_ratio,
        r.trades,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest each pattern individually and compare results."
    )
    parser.add_argument(
        "--symbols", type=int, default=50,
        help="Number of top symbols to test (default: 50).",
    )
    args = parser.parse_args()

    log.info(f"Fetching top {args.symbols} symbols from TradingView...")
    symbol_rows = TVClient.fetch_top_symbols_with_exchanges(
        args.symbols, settings.tv_screener
    )
    if not symbol_rows:
        log.error("No symbols fetched — aborting")
        return
    symbols = [s for s, _ in symbol_rows]

    pattern_names = discover_pattern_names()
    log.info(f"Discovered {len(pattern_names)} patterns: {pattern_names}")

    rows: list = []
    for pname in pattern_names:
        log.info(f"\n--- Backtesting: {pname} ---")
        rows.append(await run_one(pname, symbols))

    # Sort by win rate (ascending — worst first)
    rows.sort(key=lambda r: r[5])

    print()
    print("=" * 100)
    print("  PATTERN COMPARISON  (sorted by win rate — worst first)")
    print("=" * 100)
    hdr = f"{'Pattern':35s} {'Signals':>7s} {'Trades':>6s} {'W':>4s} {'L':>4s} {'Win%':>6s} {'EqP&L':>8s} {'AccP&L':>8s} {'AvgP&L':>7s} {'MaxDD':>7s} {'Sharpe':>7s}"
    print(hdr)
    print("-" * 100)
    for r in rows:
        print(
            f"{r[0]:35s} {r[1]:>7d} {r[2]:>6d} {r[3]:>4d} {r[4]:>4d} "
            f"{r[5]:>5.1%} {r[6]:>+7.2f}% {r[7]:>+7.2f}% "
            f"{r[8]:>+6.2f}% {r[9]:>+6.2f}% {r[10]:>6.2f}"
        )
    print("-" * 100)

    # Detailed trade dump per pattern
    for r in rows:
        print(f"\n  {r[0]}  (win rate {r[5]:.1%}, {r[2]} trades)")
        if r[2] == 0:
            print("    (no trades)")
        else:
            for t in r[11]:
                print(
                    f"    {t.entry_date.strftime('%Y-%m-%d')} {t.action:4s} "
                    f"{t.symbol:6s} {t.entry_price:>8.2f} → {t.exit_price:>8.2f} "
                    f"{t.pnl_pct:>+7.2f}% ({t.exit_reason})"
                )


if __name__ == "__main__":
    asyncio.run(main())
