"""
main.py - Entry point. Runs the market scanner, backtester, or GUI.

Usage:
    python main.py                                  # Live/paper scan mode
    python main.py --backtest --market ph            # PSE / PHP / long-only
    python main.py --paper --market ph               # PHP paper ledger (separate file)
    python main.py --backtest                       # Backtest all patterns (100 symbols)
    python main.py --backtest 10                    # Backtest all patterns (10 symbols)
    python main.py --backtest --pattern double_top  # Test one pattern only
    python main.py --backtest --volume-gate         # Backtest with volume confirm gate ON
    python main.py --backtest 50 --volume-gate-compare  # A/B: gate OFF vs ON
    python main.py --paper                          # Paper trade top 100 symbols (simulated fills)
    python main.py --paper --paper-reset            # ...starting from a fresh virtual account
    python main.py --ui                             # Launch the symbol explorer GUI
    python main.py --web                            # Launch the authenticated web UI (VPS)
    python main.py --papertrade-stream              # Serve historical CSV bars for paper trading when markets are closed
    python main.py --papertrade-stream --papertrade-stream-start 2025-01-02  # Replay from a specific date
    python main.py --kronos-test                    # Score Kronos-base +1d/+1w forecast accuracy (20 random symbols)
    python main.py --kronos-test 50                  # ...on 50 randomly sampled symbols
    python main.py --kronos-test 50 --kronos-liquid-only  # ...top 50 by $ volume instead of random
    python scripts/compare_patterns.py              # Cross-pattern comparison (parallel)
    python scripts/compare_patterns.py -p 4         # Limit to 4 concurrent backtests

Prerequisites:
  - .env file filled in (copy from .env.example)
  - pip install -r requirements.txt
  - Scanner/backtester only: TWS or IB Gateway running locally
    (paper: 7497, live: 7496) - not required for --ui
  - --kronos-test / Kronos gate: https://github.com/shiyu-coder/Kronos cloned to
    ~/Kronos with Kronos-base + Kronos-Tokenizer-base weights saved under
    ~/Kronos/weights/ (see README "Kronos Forecast Accuracy Test").
    When KRONOS_GATE_ENABLED=true, scanner / paper / UI backtest / UI paper
    require a 1w Kronos forecast to agree with each chart-pattern signal.
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
from core.engine_defaults import backtest_kwargs
from core.market import format_money, get_market
from core.paper_trader import PaperAccount, days_held, r_multiple, unrealized_pct
from data.tv_client import TVClient
from utils.logger import log


async def run_scanner(
    n_symbols: int = 100,
    *,
    volume_gate: bool | None = None,
    market: str | None = None,
) -> None:
    os.makedirs("logs", exist_ok=True)
    os.makedirs("charts", exist_ok=True)

    profile = get_market(market)
    use_volume = (
        settings.volume_gate_enabled if volume_gate is None else volume_gate
    )

    log.info("=" * 60)
    log.info(f"  Trading Bot — mode: {settings.trading_mode.upper()}")
    log.info(f"  Market:     {profile.label} ({profile.currency})")
    log.info(f"  Scan every: {profile.scan_interval_seconds}s")
    log.info(f"  History:    {settings.tv_history_days} daily bars")
    log.info(f"  Vision:     {'ON' if settings.vision_confirmation_enabled else 'OFF'}")
    log.info(f"  Kronos gate:{'ON' if profile.kronos_gate_default else 'OFF'}")
    log.info(f"  Kronos rank:{'ON' if profile.kronos_rank_default else 'OFF'}")
    log.info(f"  Volume gate:{'ON' if use_volume else 'OFF'}")
    log.info(f"  Long-only:  {'YES' if profile.long_only else 'no'}")
    log.info(f"  IBKR:       disabled (commented out)")
    log.info("=" * 60)

    log.info(f"Fetching top {n_symbols} symbols from TradingView ({profile.tv_screener})...")
    symbol_rows = TVClient.fetch_universe(n_symbols, profile.id)
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
        kronos_gate=profile.kronos_gate_default,
        kronos_rank=profile.kronos_rank_default,
        volume_gate=use_volume,
        market=profile.id,
    )
    await scanner.run()


async def run_paper(
    n_symbols: int = 100,
    reset: bool = False,
    *,
    volume_gate: bool | None = None,
    market: str | None = None,
) -> None:
    os.makedirs("logs", exist_ok=True)
    os.makedirs("charts", exist_ok=True)

    profile = get_market(market)
    if reset and profile.paper_account_path.exists():
        profile.paper_account_path.unlink()
        log.info(f"Paper | {profile.id} account reset")

    account = PaperAccount.load(market=profile.id)
    use_volume = (
        settings.volume_gate_enabled if volume_gate is None else volume_gate
    )
    kronos_gate = profile.kronos_gate_default
    kronos_rank = profile.kronos_rank_default

    log.info("=" * 60)
    log.info("  Trading Bot — PAPER TRADING MODE (simulated fills, no broker)")
    log.info(f"  Market:     {profile.label} ({profile.currency})")
    log.info(f"  Starting equity: {format_money(account.equity(), profile.id)}")
    log.info(f"  Scan every: {profile.scan_interval_seconds}s")
    log.info(f"  Kronos gate:{'ON' if kronos_gate else 'OFF'}")
    log.info(f"  Kronos rank:{'ON' if kronos_rank else 'OFF'}")
    log.info(f"  Volume gate:{'ON' if use_volume else 'OFF'}")
    log.info(f"  Long-only:  {'YES' if profile.long_only else 'no'}")
    log.info("=" * 60)

    log.info(f"Fetching top {n_symbols} symbols from TradingView ({profile.tv_screener})...")
    symbol_rows = TVClient.fetch_universe(n_symbols, profile.id)
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
        kronos_gate=kronos_gate,
        kronos_rank=kronos_rank,
        volume_gate=use_volume,
        market=profile.id,
    )
    try:
        await scanner.run()
    finally:
        account.save()
        print()
        print(account.to_result().summary())
        print(f"  Open positions:    {len(account.positions)}")
        print(f"  Equity:            {format_money(account.equity(), profile.id)}")
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


async def run_backtest(
    n_symbols: int,
    pattern: str | None = None,
    *,
    volume_gate: bool | None = None,
    volume_gate_compare: bool = False,
    market: str | None = None,
) -> None:
    os.makedirs("logs", exist_ok=True)

    profile = get_market(market)
    use_volume = (
        settings.volume_gate_enabled if volume_gate is None else volume_gate
    )
    title = f"BACKTEST MODE{' — ' + pattern if pattern else ''}"
    universe_note = (
        "top by peso volume" if profile.id == "ph" else "top by market cap"
    )
    log.info("=" * 60)
    log.info(f"  Trading Bot — {title}")
    log.info(f"  Market:     {profile.label} ({profile.currency})")
    log.info(f"  Symbols:    {n_symbols} {universe_note}")
    log.info(f"  Kronos gate:{'ON' if profile.kronos_gate_default else 'OFF'}")
    log.info(f"  Kronos rank:{'ON' if profile.kronos_rank_default else 'OFF'}")
    log.info(f"  Long-only:  {'YES' if profile.long_only else 'no'}")
    log.info(f"  Txn cost:   {profile.txn_cost_pct:.4f} one-way")
    log.info(f"  Volume gate:{'ON' if use_volume else 'OFF'}"
             f"{' (A/B compare)' if volume_gate_compare else ''}")
    log.info("=" * 60)

    log.info(f"Fetching {n_symbols} symbols from TradingView (cached, {profile.tv_screener})...")
    symbol_rows = TVClient.fetch_universe_cached(n_symbols, profile.id)
    if not symbol_rows:
        log.error("Failed to fetch symbols from TradingView — aborting")
        return
    symbols = [symbol for symbol, _exchange in symbol_rows]
    log.info(f"Watchlist:  {symbols}")

    # Shared money-path knobs — same dict paper + MarketScanner now honor via
    # core.engine_defaults.ENGINE. Do not re-hardcode here or paper/backtest
    # will drift again. Re-tune only against out-of-sample data.
    bt_kwargs = backtest_kwargs(
        market=profile.id,
        pattern_filter=pattern,
        disabled_patterns=DISABLED_PATTERNS,
        kronos_gate=profile.kronos_gate_default,
        kronos_rank=profile.kronos_rank_default,
    )

    if volume_gate_compare:
        from analysis.price_volume import ab_metrics_from_result

        log.info("Volume A/B | running gate OFF then ON (same symbols/patterns)...")
        off_bt = Backtester(symbols, volume_gate=False, **bt_kwargs)
        result_off = await off_bt.run()
        on_bt = Backtester(symbols, volume_gate=True, **bt_kwargs)
        result_on = await on_bt.run()

        off_m = ab_metrics_from_result(result_off)
        on_m = ab_metrics_from_result(result_on)
        keys = [
            "trades", "win_rate", "avg_r", "expectancy_pct",
            "profit_factor", "max_drawdown_pct", "account_weighted_pnl_pct",
            "total_signals",
        ]

        def _fmt(v):
            if v is None:
                return "—"
            if isinstance(v, float):
                return f"{v:+.4f}" if abs(v) < 10 else f"{v:.4f}"
            return str(v)

        print()
        print("=" * 72)
        print("  VOLUME GATE A/B COMPARE")
        print("=" * 72)
        print(f"  {'metric':28s}  {'OFF':>12s}  {'ON':>12s}  {'delta':>12s}")
        print("-" * 72)
        for k in keys:
            a, b = off_m[k], on_m[k]
            if a is None or b is None:
                delta = "—"
            elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
                delta = _fmt(b - a)
            else:
                delta = "—"
            print(f"  {k:28s}  {_fmt(a):>12s}  {_fmt(b):>12s}  {delta:>12s}")
        print("=" * 72)
        print()
        print("--- Gate OFF summary ---")
        print(result_off.summary())
        print()
        print("--- Gate ON summary ---")
        print(result_on.summary())

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        ab_path = f"backtest_volume_ab_{ts}.json"
        payload = {
            "n_symbols": n_symbols,
            "pattern": pattern,
            "volume_gate_rvol_min": settings.volume_gate_rvol_min,
            "volume_gate_obv_bars": settings.volume_gate_obv_bars,
            "off": off_m,
            "on": on_m,
            "off_full": result_off.to_dict(),
            "on_full": result_on.to_dict(),
        }
        json.dump(payload, Path(ab_path).open("w", encoding="utf-8"), indent=2)
        log.info(f"Backtest | Volume A/B JSON saved to {ab_path}")
        return

    backtester = Backtester(symbols, volume_gate=use_volume, **bt_kwargs)
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trading Bot - market scanner / backtester / GUI"
    )
    parser.add_argument(
        "--market",
        choices=["us", "ph"],
        default=None,
        help="Market profile: us (NASDAQ/NYSE, USD) or ph (PSE, PHP, long-only). "
        "Default: MARKET in .env (us). UI/web can also pick per run.",
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
        "--volume-gate",
        action="store_true",
        help="Enable the RVOL+OBV volume confirm gate for this run "
        "(overrides VOLUME_GATE_ENABLED=false). Use with --backtest / --paper.",
    )
    parser.add_argument(
        "--volume-gate-compare",
        action="store_true",
        help="With --backtest, run twice (volume gate OFF then ON) and print a "
        "side-by-side A/B report; writes backtest_volume_ab_*.json.",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Launch the tkinter symbol explorer GUI instead of scanning.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch the authenticated FastAPI web UI (requires WEB_UI_PASSWORD). "
        "Binds WEB_UI_HOST:WEB_UI_PORT (default 0.0.0.0:8080).",
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
    parser.add_argument(
        "--kronos-test",
        nargs="?",
        const=20,
        type=int,
        default=None,
        metavar="N",
        help="Evaluate Kronos-base's +1 day / +1 week close-price forecast accuracy "
        "on N randomly sampled symbols (default: 20) from /home/r00t/stocks_data.",
    )
    parser.add_argument(
        "--kronos-liquid-only",
        action="store_true",
        help="With --kronos-test, rank symbols by recent dollar volume and take the "
        "top N (liquid large/mid-caps) instead of a random sample.",
    )
    parser.add_argument(
        "--kronos-sample-count",
        type=int,
        default=1,
        metavar="N",
        help="With --kronos-test, average N sampled forecast paths per window "
        "(Kronos does this internally) instead of a single noisy path. Default: 1.",
    )
    parser.add_argument(
        "--kronos-start-date",
        type=str,
        default="2026-03-01",
        metavar="YYYY-MM-DD",
        help="With --kronos-test, drop tickers with no data on/after this date "
        "(stale/delisted) and only score windows as-of this date or later. "
        "Older bars are still used as lookback context. Default: 2026-03-01.",
    )
    parser.add_argument(
        "--kronos-use-finetuned",
        action="store_true",
        help="With --kronos-test, use the fine-tuned checkpoint from --kronos-finetune "
        "instead of base Kronos weights (falls back to base if none exists).",
    )
    parser.add_argument(
        "--kronos-finetune",
        action="store_true",
        help="Fine-tune Kronos-base's tokenizer + predictor on liquid tickers from "
        "/home/r00t/stocks_data. Saves to ~/Kronos/finetuned/. Needs a CUDA GPU to "
        "finish in a reasonable time.",
    )
    parser.add_argument(
        "--kronos-finetune-symbols",
        type=int,
        default=1500,
        metavar="N",
        help="With --kronos-finetune, train on the top N liquid tickers (default: 1500).",
    )
    parser.add_argument(
        "--kronos-finetune-epochs",
        type=int,
        default=10,
        metavar="N",
        help="With --kronos-finetune, epochs for both tokenizer and predictor stages (default: 10).",
    )
    parser.add_argument(
        "--kronos-finetune-batch-size",
        type=int,
        default=16,
        metavar="N",
        help="With --kronos-finetune, training batch size (default: 16, sized for an 8GB GPU).",
    )
    parser.add_argument(
        "--kronos-finetune-skip-tokenizer",
        action="store_true",
        help="With --kronos-finetune, reuse the existing fine-tuned tokenizer checkpoint "
        "(or base tokenizer if none) and only fine-tune the predictor.",
    )
    parser.add_argument(
        "--papertrade-stream",
        action="store_true",
        help="Run the paper trade stream server: replays historical daily CSVs "
        "from settings.papertrade_stream_dir (default /home/r00t/stocks_data) over "
        "a local WebSocket so paper trading (--paper / --ui) can run with the "
        "'Use paper trade stream' option even when US markets are closed.",
    )
    parser.add_argument(
        "--papertrade-stream-start",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="With --papertrade-stream, start replaying each CSV from this date "
        "(first bar on/after). Default: PAPERTRADE_STREAM_START_DATE, or near "
        "the end of each CSV.",
    )
    return parser.parse_args()


async def main(args: argparse.Namespace | None = None) -> None:
    if args is None:
        args = _parse_args()

    if args.kronos_finetune:
        from core.kronos_finetune import run_kronos_finetune

        run_kronos_finetune(
            n_symbols=args.kronos_finetune_symbols,
            tokenizer_epochs=args.kronos_finetune_epochs,
            predictor_epochs=args.kronos_finetune_epochs,
            batch_size=args.kronos_finetune_batch_size,
            skip_tokenizer=args.kronos_finetune_skip_tokenizer,
        )
        return

    if args.kronos_test is not None:
        from core.kronos_eval import run_kronos_test

        run_kronos_test(
            n_symbols=args.kronos_test,
            liquid_only=args.kronos_liquid_only,
            sample_count=args.kronos_sample_count,
            start_date=args.kronos_start_date,
            use_finetuned=args.kronos_use_finetuned,
        )
        return

    if args.papertrade_stream:
        from data.stream_server import run_stream_server

        await run_stream_server(start_date=args.papertrade_stream_start)
        return

    if args.learn:
        from pathlib import Path as _Path
        from learn.train import run_learn

        kwargs = {"max_tickers": args.learn_max_tickers}
        if args.learn_data_dir:
            kwargs["data_dir"] = _Path(args.learn_data_dir)
        run_learn(**kwargs)
        return

    if args.paper is not None:
        await run_paper(
            n_symbols=args.paper,
            reset=args.paper_reset,
            volume_gate=True if args.volume_gate else None,
            market=args.market,
        )
    elif args.backtest is not None:
        await run_backtest(
            n_symbols=args.backtest,
            pattern=args.pattern,
            volume_gate=True if args.volume_gate else None,
            volume_gate_compare=args.volume_gate_compare,
            market=args.market,
        )
    else:
        await run_scanner(
            volume_gate=True if args.volume_gate else None,
            market=args.market,
        )


if __name__ == "__main__":
    # --ui / --web own their own event loop (tk / uvicorn). Running them
    # inside asyncio.run(main()) nests asyncio.run and crashes uvicorn.
    _args = _parse_args()
    if _args.ui:
        from ui.app import run as run_ui

        run_ui()
    elif _args.web:
        from web.app import run as run_web

        run_web()
    else:
        asyncio.run(main(_args))
