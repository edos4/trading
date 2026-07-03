"""
main.py - Entry point. Runs the market scanner, backtester, or GUI.

Usage:
    python main.py                     # Live/paper scan mode
    python main.py --backtest          # Backtest mode (top 100 symbols)
    python main.py --backtest 10       # Backtest mode (top 10 symbols)
    python main.py --ui                # Launch the symbol explorer GUI

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


async def run_backtest(n_symbols: int) -> None:
    os.makedirs("logs", exist_ok=True)

    log.info("=" * 60)
    log.info(f"  Trading Bot — BACKTEST MODE")
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

    backtester = Backtester(symbols)
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
        await run_backtest(n_symbols=args.backtest)
    else:
        await run_scanner()


if __name__ == "__main__":
    asyncio.run(main())
