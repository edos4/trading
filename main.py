"""
main.py - Entry point. Runs the market scanner, backtester, or GUI.

Usage:
    python main.py                                  # Live/paper scan mode
    python main.py --backtest                       # Backtest all patterns (100 symbols)
    python main.py --backtest 10                    # Backtest all patterns (10 symbols)
    python main.py --backtest --pattern double_top  # Test one pattern only
    python main.py --paper                          # Paper trade top 100 symbols (simulated fills)
    python main.py --paper --paper-reset            # ...starting from a fresh virtual account
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

from config import settings, DISABLED_PATTERNS
from core.scanner import MarketScanner
from core.backtester import Backtester
from core.paper_trader import PaperAccount, DEFAULT_ACCOUNT_PATH, days_held, r_multiple, unrealized_pct
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

    scanner = MarketScanner(
        symbols=symbols,
        exchange_overrides=exchange_overrides,
        disabled_patterns=DISABLED_PATTERNS,
    )
    await scanner.run()


async def run_paper(n_symbols: int = 100, reset: bool = False) -> None:
    os.makedirs("logs", exist_ok=True)
    os.makedirs("charts", exist_ok=True)

    if reset and DEFAULT_ACCOUNT_PATH.exists():
        DEFAULT_ACCOUNT_PATH.unlink()
        log.info("Paper | account reset")

    account = PaperAccount.load()

    log.info("=" * 60)
    log.info("  Trading Bot — PAPER TRADING MODE (simulated fills, no broker)")
    log.info(f"  Starting equity: ${account.equity():,.2f}")
    log.info(f"  Scan every: {settings.scan_interval_seconds}s")
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

    scanner = MarketScanner(
        symbols=symbols,
        exchange_overrides=exchange_overrides,
        paper_account=account,
        disabled_patterns=DISABLED_PATTERNS,
    )
    try:
        await scanner.run()
    finally:
        account.save()
        print()
        print(account.to_result().summary())
        print(f"  Open positions:    {len(account.positions)}")
        print(f"  Equity:            ${account.equity():,.2f}")
        print()

        if account.positions:
            print("  OPEN POSITIONS")
            print("-" * 85)
            for sym, p in account.positions.items():
                current = account.last_price(sym, p.entry_price)
                r = r_multiple(p, current)
                r_str = f"{r:+.2f}" if r is not None else "-"
                print(
                    f"  {p.entry_date.strftime('%Y-%m-%d %H:%M:%S')}  "
                    f"{p.action:5s} {sym:8s} entry={p.entry_price:.2f} current={current:.2f} "
                    f"unrl={unrealized_pct(p, current):+.2f}% R={r_str} "
                    f"days={days_held(p):.1f}  {p.pattern}"
                )
            print()

        if account.closed:
            print("  CLOSED TRADES")
            print("-" * 85)
            for t in sorted(account.closed, key=lambda t: t.exit_date):
                r = r_multiple(t, t.exit_price)
                r_str = f"{r:+.2f}" if r is not None else "-"
                print(
                    f"  opened={t.entry_date.strftime('%Y-%m-%d %H:%M:%S')}  "
                    f"closed={t.exit_date.strftime('%Y-%m-%d %H:%M:%S')}  "
                    f"held={days_held(t):.1f}d  "
                    f"{t.action:5s} {t.symbol:8s} R={r_str}  "
                    f"entry={t.entry_price:.2f} exit={t.exit_price:.2f} "
                    f"pnl={t.pnl_pct:+.2f}%  reason={t.exit_reason}  {t.pattern}"
                )
            print()


async def run_backtest(n_symbols: int, pattern: str | None = None) -> None:
    os.makedirs("logs", exist_ok=True)

    title = f"BACKTEST MODE{' — ' + pattern if pattern else ''}"
    log.info("=" * 60)
    log.info(f"  Trading Bot — {title}")
    log.info(f"  Symbols:    top {n_symbols} by market cap")
    log.info("=" * 60)

    log.info(f"Fetching top {n_symbols} symbols from TradingView (cached)...")
    symbol_rows = TVClient.fetch_top_symbols_with_exchanges_cached(
        n_symbols, settings.tv_screener
    )
    if not symbol_rows:
        log.error("Failed to fetch symbols from TradingView — aborting")
        return
    symbols = [symbol for symbol, _exchange in symbol_rows]
    log.info(f"Watchlist:  {symbols}")

    # Engine-level risk overlays below are deliberately neutral: generic,
    # principled defaults — not fit to any specific past backtest run's
    # named trades/symbols. Re-tune only against out-of-sample data, never
    # by adjusting a threshold until a specific historical loss disappears
    # (that guarantees a good-looking win rate on that one sample and
    # nothing else). Each pattern still owns its own stop/target/trailing
    # logic via TradeSignal — these are entry gates and execution backstops
    # on top of that, not a replacement for it.
    backtester = Backtester(
        symbols,
        min_confidence=0.6,
        regime_filter=True,
        cooldown_bars=10,
        txn_cost_pct=0.001,
        position_sizing="risk",
        account_value=100_000.0,
        risk_per_trade_pct=0.02,
        # Diversification ceiling, independent of risk_per_trade_pct. At 0.10
        # this silently capped every ~6%-stop trade (the common case, via
        # hard_stop_percentage) to ~0.6% real risk instead of the configured
        # 2% (0.02 risk / 0.06 stop = 33% notional needed to fully use the
        # risk budget) — risk_per_trade_pct was a no-op in practice. Raised
        # to 0.33 so it actually binds at the intended risk level; max_open_positions
        # stays unlimited so this is per-name concentration, not total exposure.
        max_position_pct=0.33,
        # Cushion of unrealized profit before the trailing stop arms, so
        # ordinary entry-day chop doesn't stop trades out before the
        # pattern's own trailing logic gets to manage them.
        trailing_activation_default=0.02,
        breakeven_trigger_pct=None,
        min_atr_stop_multiple=1.0,
        # Generic catastrophic-gap backstop, wide enough to not pre-empt
        # the pattern's own trailing stop under normal conditions.
        synthetic_stop_multiple=2.0,
        # Widens (never tightens) a structural stop that's tighter than
        # 1.2x ATR(14) — screens out stops that are just ordinary daily
        # noise rather than a real invalidation level.
        atr_stop_floor_multiple=1.2,
        # Hard cap: no trade risks more than 6% from entry, regardless of
        # how wide a pattern's own structural stop is. Was 0.03, which sat
        # *tighter* than the synthetic (6%) and ATR-floor (1.2x ATR) stops
        # it's supposed to backstop — so it silently overrode both on most
        # trades and became the everyday stop instead of a tail backstop.
        hard_stop_percentage=0.06,
        min_reward_risk_ratio=1.5,
        max_open_positions=settings.max_open_positions,
        min_hold_bars=2,
        pattern_filter=pattern,
        disabled_patterns=DISABLED_PATTERNS,
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
    parser.add_argument(
        "--paper",
        nargs="?",
        const=100,
        type=int,
        default=None,
        metavar="N",
        help="Run paper trading on top N symbols (default: 100) — live scan, "
        "simulated fills, no real broker.",
    )
    parser.add_argument(
        "--paper-reset",
        action="store_true",
        help="Wipe the saved paper-trading account and start fresh (use with --paper).",
    )
    parser.add_argument(
        "--learn",
        action="store_true",
        help="Ingest historical daily OHLCV CSVs (default /home/r00t/stocks_data) and "
        "train the pattern_012_ml_signal model used by paper/live trading.",
    )
    parser.add_argument(
        "--learn-data-dir",
        type=str,
        default=None,
        metavar="DIR",
        help="Override the CSV directory for --learn (default: /home/r00t/stocks_data).",
    )
    parser.add_argument(
        "--learn-max-tickers",
        type=int,
        default=None,
        metavar="N",
        help="Limit --learn to the first N ticker CSVs (smoke test before a full run).",
    )
    args = parser.parse_args()

    if args.learn:
        from pathlib import Path as _Path
        from learn.train import run_learn

        kwargs = {"max_tickers": args.learn_max_tickers}
        if args.learn_data_dir:
            kwargs["data_dir"] = _Path(args.learn_data_dir)
        run_learn(**kwargs)
        return

    if args.ui:
        # tkinter mainloop is blocking and not async.
        from ui.app import run as run_ui

        run_ui()
        return

    if args.paper is not None:
        await run_paper(n_symbols=args.paper, reset=args.paper_reset)
    elif args.backtest is not None:
        await run_backtest(n_symbols=args.backtest, pattern=args.pattern)
    else:
        await run_scanner()


if __name__ == "__main__":
    asyncio.run(main())
