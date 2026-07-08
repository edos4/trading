"""
main.py - Entry point. Runs the market scanner, backtester, or GUI.

Usage:
    python main.py                                  # Live/paper scan mode
    python main.py --backtest                       # Backtest all patterns (100 symbols)
    python main.py --backtest 10                    # Backtest all patterns (10 symbols)
    python main.py --backtest --pattern double_top  # Test one pattern only
    python main.py --ui                             # Launch the symbol explorer GUI
    python scripts/compare_patterns.py              # Cross-pattern comparison (parallel)
    python scripts/compare_patterns.py -p 4         # Limit to 4 concurrent backtests

Prerequisites:
  - .env file filled in (copy from .env.example)
  - pip install -r requirements.txt
  - Scanner/backtester only: TWS or IB Gateway running locally
    (paper: 7497, live: 7496) - not required for --ui
"""

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core.scanner import MarketScanner
from core.backtester import Backtester
from data.tv_client import TVClient
from utils.logger import log


async def run_scanner(n_symbols: int = 100) -> None:
    os.makedirs("logs", exist_ok=True)
    os.makedirs("charts", exist_ok=True)

    log.info("=" * 60)
    log.info(f"  Trading Bot — mode: {settings.trading_mode.upper()}")
    log.info(f"  Scan every: {settings.scan_interval_seconds}s")
    log.info(f"  History:    {settings.tv_history_days} daily bars")
    log.info(f"  Vision:     {'ON' if settings.vision_confirmation_enabled else 'OFF'}")
    log.info(f"  IBKR:       disabled (commented out)")
    log.info("=" * 60)

    log.info(f"Fetching top {n_symbols} symbols from TradingView...")
    symbol_rows = TVClient.fetch_top_symbols_with_exchanges(
        n_symbols, settings.tv_screener
    )
    if not symbol_rows:
        log.error("Failed to fetch symbols from TradingView — aborting")
        return
    symbols = [symbol for symbol, _exchange in symbol_rows]
    exchange_overrides = dict(symbol_rows)
    log.info(f"Watchlist:  {symbols}")

    if settings.is_live:
        log.warning("S LIVE TRADING MODE — real capital is at risk")

    scanner = MarketScanner(symbols=symbols, exchange_overrides=exchange_overrides)
    await scanner.run()


async def run_backtest(n_symbols: int, pattern: str | None = None) -> None:
    os.makedirs("logs", exist_ok=True)

    title = f"BACKTEST MODE{' — ' + pattern if pattern else ''}"
    log.info("=" * 60)
    log.info(f"  Trading Bot — {title}")
    log.info(f"  Symbols:    top {n_symbols} by market cap")
    log.info("=" * 60)

    log.info(f"Fetching top {n_symbols} symbols from TradingView...")
    symbol_rows = TVClient.fetch_top_symbols_with_exchanges(
        n_symbols, settings.tv_screener
    )
    if not symbol_rows:
        log.error("Failed to fetch symbols from TradingView — aborting")
        return
    symbols = [symbol for symbol, _exchange in symbol_rows]
    log.info(f"Watchlist:  {symbols}")

    backtester = Backtester(
        symbols,
        # Raised from 0.70. Every pattern's confidence score is 0.55 (base,
        # all hard filters passed) plus up to a handful of +0.05/+0.10
        # confluence bonuses. 0.70 let through setups with only one weak
        # bonus hit; 0.78 requires meaningfully more confluence before the
        # engine will act on a signal, which disproportionately screens out
        # the marginal double_bottom-style setups that were driving most of
        # the aggregate loss (44.4% win, -10.54% eq P&L over 9 trades) while
        # barely touching cleaner setups like head_and_shoulders/double_top,
        # whose trades were already comfortably above this bar.
        min_confidence=0.78,
        regime_filter=True,
        # Widened from 20. A loss is frequently followed by more chop in the
        # same symbol/pattern combination (e.g. AMZN/VZ stop_loss exits) —
        # giving more bars before a repeat entry is allowed reduces
        # re-entering into the same still-unresolved failure.
        cooldown_bars=35,
        txn_cost_pct=0.001,
        position_sizing="risk",
        account_value=100_000.0,
        risk_per_trade_pct=0.02,
        # Give trades a small cushion of unrealized profit before the
        # trailing stop arms, so normal entry-day chop doesn't stop trades
        # out before the pattern's own trailing logic gets to manage them.
        trailing_activation_default=0.01,
        # Lowered from 0.05 → 0.025 → 0.01. This is the single highest-leverage,
        # purely execution-layer lever for win rate: once a trade has been
        # ahead by this much at any point, its floor is raised to ~entry
        # (buffer above entry on longs, below on shorts) — i.e. a round trip
        # exits at a small WIN instead of riding back down to the pattern's
        # full stop/time-exit distance. At 0.01 (1%) this aligns with the
        # trailing activation threshold, so any trade that activates its
        # trailing stop also arms breakeven protection — preventing the
        # round-trip-to-loss pattern (e.g. PFE trailing_stop exit at -1.44%
        # after being up ~1%) while still letting winners run since the
        # ratcheting trailing stop sits above the breakeven floor once the
        # trade is meaningfully ahead.
        breakeven_trigger_pct=0.01,
        # Raised from 0.0015 → 0.003. A 0.15% buffer above entry left
        # round-trip exits at scratch/loss after txn costs (KO exited at
        # -0.05% via breakeven_stop). 0.3% ensures the breakeven floor
        # produces a small but positive exit even after 0.1% txn costs.
        breakeven_buffer_pct=0.003,
        # Raised from 1.25. Requires the trailing/stop cushion to be a
        # noticeably larger multiple of the symbol's recent ATR before an
        # entry is taken at all, screening out setups where the stop is
        # basically ordinary daily noise (a frequent cause of quick
        # trailing_stop losses like KO/PFE above) rather than a real
        # thesis failure.
        min_atr_stop_multiple=1.6,
        # Widen the synthetic catastrophic stop so it backstops real gap
        # risk instead of duplicating (and pre-empting) the trailing stop.
        synthetic_stop_multiple=1.75,
        max_open_positions=settings.max_open_positions,
        # Raised from 2. A couple of extra bars of mandatory hold further
        # reduces exits driven by entry-day/next-day noise before the
        # pattern's own trailing/target logic has had a chance to work.
        min_hold_bars=4,
        pattern_filter=pattern,
    )
    result = await backtester.run()

    print()
    print(result.summary())
    print()

    if result.trades:
        print(
            f"{'Date':>10s}  {'Action':5s} {'Symbol':6s} {'TF'} {'Entry':>8s} {'Exit':>8s} {'P&L%':>8s}  Pattern"
        )
        print("-" * 85)
        for t in result.trades:
            print(
                f"{t.entry_date.strftime('%Y-%m-%d'):>10s}  "
                f"{t.action:5s} {t.symbol:6s} {t.timeframe:2s} "
                f"{t.entry_price:>8.2f} {t.exit_price:>8.2f} "
                f"{t.pnl_pct:>+7.2f}%  "
                f"{t.pattern}"
            )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    txt_path = f"backtest_results_{ts}.txt"
    json_path = f"backtest_results_{ts}.json"
    result.save(txt_path)
    json.dump(result.to_dict(), Path(json_path).open("w", encoding="utf-8"), indent=2)
    log.info(f"Backtest | JSON saved to {json_path}")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trading Bot - market scanner / backtester / GUI"
    )
    parser.add_argument(
        "--backtest",
        nargs="?",
        const=100,
        type=int,
        default=None,
        metavar="N",
        help="Run backtest on top N symbols (default: 100). "
        "Without --backtest, runs live/paper scan.",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default=None,
        metavar="NAME",
        help="Filter to a specific pattern (case-insensitive substring). "
        "Use with --backtest to test one pattern in isolation. "
        "E.g.: --backtest --pattern double_top",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Launch the tkinter symbol explorer GUI instead of scanning.",
    )
    args = parser.parse_args()

    if args.ui:
        # tkinter mainloop is blocking and not async.
        from ui.app import run as run_ui

        run_ui()
        return

    if args.backtest is not None:
        await run_backtest(n_symbols=args.backtest, pattern=args.pattern)
    else:
        await run_scanner()


if __name__ == "__main__":
    asyncio.run(main())
