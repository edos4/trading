"""
main.py - Entry point. Runs the market scanner, backtester, or GUI.

Usage:
    python main.py                                  # Live/paper scan mode
    python main.py --backtest --market ph            # PSE / PHP / long-only
    python main.py --paper --market ph               # PHP paper ledger (separate file)
    python main.py --backtest                       # Backtest all patterns (50 liquid symbols)
    python main.py --backtest 10                    # Backtest all patterns (10 symbols)
    python main.py --backtest --pattern double_top  # Test one pattern only
    python main.py --backtest --volume-gate         # Backtest with volume confirm gate ON
    python main.py --backtest 50 --volume-gate-compare  # A/B: gate OFF vs ON
    python main.py --paper                          # Paper trade top 50 liquid symbols (simulated fills)
    python main.py --paper --paper-reset            # ...starting from a fresh virtual account
    python main.py --paper --symbols=500 --pattern-only --collect-first=4 \\
        --stream=01/05/2026 --duration-days=30 --export-trades-log=output_trades.json
                                                    # 500-name paper stream from 2026-01-05, 30 sessions,
                                                    # top-4 R:R, dump open+closed trades on exit
    python main.py --ui                             # Launch the symbol explorer GUI
    python main.py --web                            # Launch the authenticated web UI (VPS)
                                                    # On start: connect to stocks_history and
                                                    # backfill missing/stale daily bars per symbol.
    python main.py --papertrade-stream              # Serve historical bars (33ai /api/history) for paper trading when markets are closed
    python main.py --papertrade-stream --papertrade-stream-start 2025-01-02  # Replay from a specific date
    python main.py --kronos-test                    # Score Kronos-base +1d/+3d forecast accuracy (20 random symbols)
    python main.py --kronos-test 50                  # ...on 50 randomly sampled symbols
    python main.py --kronos-test 50 --kronos-liquid-only  # ...top 50 by $ volume instead of random
    python main.py --check-db                        # Verify US + PH freshness
    python main.py --update-db                       # Daily incremental update: Yahoo/PSE fetch for stale symbols
    python main.py --ingest-pse                      # Seed local stocks_history from PSE Edge (TICKER.PS)
    python main.py --export-pse /tmp/pse_dump        # COPY PH rows for 33ai import
    python main.py --import-pse /tmp/pse_dump        # Load PH dump (refuses non-.PS / non-ph)
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
    require a 3-trading-day Kronos forecast to agree with each chart-pattern signal.
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

# Bare `--collect-first` (no N) enables the mode with COLLECT_FIRST_TOP_N.
_COLLECT_FIRST_USE_DEFAULT = -1


def parse_stream_date(value: str) -> str:
    """Normalize a CLI stream date to YYYY-MM-DD.

    Accepts YYYY-MM-DD or US MM/DD/YYYY (also M/D/YYYY). Slash dates are
    month/day/year — `01/05/2026` is 5 January 2026, not 1 May.
    """
    raw = (value or "").strip()
    if not raw:
        raise argparse.ArgumentTypeError("empty stream date")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"invalid stream date {value!r}; use MM/DD/YYYY or YYYY-MM-DD"
    )


def _resolve_n_symbols(
    args: argparse.Namespace,
    fallback: int | None = None,
    default: int = 50,
) -> int:
    if args.symbols is not None:
        return args.symbols
    if fallback is not None:
        return fallback
    return default


def _resolve_collect_first(
    args: argparse.Namespace,
) -> tuple[bool | None, int | None]:
    if args.collect_first is None:
        return None, args.collect_first_top_n
    top_n = args.collect_first_top_n
    if args.collect_first > 0:
        top_n = args.collect_first
    return True, top_n


def _effective_stream_start(
    account: PaperAccount,
    stream_start: str | None,
    market: str,
) -> str | None:
    """Resume a saved ledger's sim date when it is ahead of --stream."""
    from zoneinfo import ZoneInfo

    effective = stream_start
    if account.sim_now() is None:
        return effective
    resume_from = account.sim_now()
    if resume_from is None:
        return effective
    profile = get_market(market)
    resume_date = resume_from.astimezone(ZoneInfo(profile.session_tz)).date()
    configured_date = None
    if stream_start:
        try:
            configured_date = datetime.strptime(stream_start, "%Y-%m-%d").date()
        except ValueError:
            configured_date = None
    if configured_date is None or configured_date <= resume_date:
        return resume_date.isoformat()
    return effective


def _write_trades_log(
    path: str,
    account: PaperAccount,
    scan_stats: dict | None = None,
) -> None:
    from utils.trade_export import build_paper_account_export

    payload = build_paper_account_export(account, scan_stats=scan_stats)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(payload, out.open("w", encoding="utf-8"), indent=2)
    log.info(f"Paper | trades log written to {out}")


async def run_scanner(
    n_symbols: int = 50,
    *,
    volume_gate: bool | None = None,
    market: str | None = None,
    collect_first: bool | None = None,
    collect_first_top_n: int | None = None,
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
    cf_on = settings.collect_first_enabled if collect_first is None else collect_first
    cf_n = (
        settings.collect_first_top_n
        if collect_first_top_n is None
        else max(1, int(collect_first_top_n))
    )
    log.info(
        f"  Collect-first:{'ON top-' + str(cf_n) if cf_on else 'OFF'}"
    )
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
        collect_first=collect_first,
        collect_first_top_n=collect_first_top_n,
    )
    await scanner.run()


async def run_paper(
    n_symbols: int = 50,
    reset: bool = False,
    *,
    kronos_gate: bool | None = None,
    volume_gate: bool | None = None,
    pattern_only: bool = False,
    market: str | None = None,
    collect_first: bool | None = None,
    collect_first_top_n: int | None = None,
    use_stream: bool = False,
    stream_start: str | None = None,
    export_trades_log: str | None = None,
    duration_days: int | None = None,
) -> None:
    os.makedirs("logs", exist_ok=True)
    os.makedirs("charts", exist_ok=True)

    profile = get_market(market)
    if reset and profile.paper_account_path.exists():
        profile.paper_account_path.unlink()
        log.info(f"Paper | {profile.id} account reset")
    if reset:
        from core.signal_log_store import reset_signal_log

        reset_signal_log(profile.id)
        log.info(f"Paper | {profile.id} signal log reset")

    account = PaperAccount.load(market=profile.id)
    use_volume = (
        settings.volume_gate_enabled if volume_gate is None else volume_gate
    )
    kronos_gate_on = (
        profile.kronos_gate_default if kronos_gate is None else kronos_gate
    )
    if pattern_only:
        # Pattern-only isolates the chart pattern: confirm gates default OFF
        # unless their flag is explicitly passed.
        if volume_gate is None:
            use_volume = False
        if kronos_gate is None:
            kronos_gate_on = False
    kronos_rank = profile.kronos_rank_default
    cf_on = settings.collect_first_enabled if collect_first is None else collect_first
    cf_n = (
        settings.collect_first_top_n
        if collect_first_top_n is None
        else max(1, int(collect_first_top_n))
    )
    effective_stream_start = (
        _effective_stream_start(account, stream_start, profile.id)
        if use_stream else None
    )
    scan_interval = (
        settings.papertrade_stream_interval_seconds
        if use_stream
        else profile.scan_interval_seconds
    )

    log.info("=" * 60)
    log.info("  Trading Bot — PAPER TRADING MODE (simulated fills, no broker)")
    log.info(f"  Market:     {profile.label} ({profile.currency})")
    log.info(f"  Symbols:    {n_symbols}")
    log.info(f"  Starting equity: {format_money(account.equity(), profile.id)}")
    if use_stream and scan_interval <= 0:
        log.info("  Scan pace:  scan-paced stream replay")
    else:
        log.info(f"  Scan every: {scan_interval}s")
    log.info(f"  Stream:     {effective_stream_start or ('ON' if use_stream else 'OFF')}")
    log.info(f"  Kronos gate:{'ON' if kronos_gate_on else 'OFF'}")
    log.info(f"  Kronos rank:{'ON' if kronos_rank else 'OFF'}")
    log.info(f"  Volume gate:{'ON' if use_volume else 'OFF'}")
    log.info(f"  Pattern-only:{'ON' if pattern_only else 'OFF'}")
    log.info(
        f"  Collect-first:{'ON top-' + str(cf_n) if cf_on else 'OFF'}"
    )
    log.info(
        f"  Duration:    {duration_days} market sessions"
        if duration_days is not None else "  Duration:    unlimited"
    )
    log.info(f"  Long-only:  {'YES' if profile.long_only and not pattern_only else 'no'}")
    log.info("=" * 60)

    scanner = None
    stream_book = None
    try:
        if use_stream:
            from core.paper_books import PaperBook
            from data.stream_client import StreamClient

            stream_book = PaperBook(profile.id)
            error = stream_book._ensure_stream_server(
                start_date=effective_stream_start,
            )
            if error:
                log.error(error)
                return
            data_feed = StreamClient()
            account.assume_session_open = True
            log.info(
                f"Fetching top {n_symbols} symbols from TradingView "
                f"(cached, {profile.tv_screener})..."
            )
            symbol_rows = TVClient.fetch_universe_cached(n_symbols, profile.id)
        else:
            data_feed = None
            log.info(
                f"Fetching top {n_symbols} symbols from TradingView "
                f"({profile.tv_screener})..."
            )
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
            data_feed=data_feed,
            scan_interval_seconds=scan_interval if use_stream else None,
            kronos_gate=kronos_gate_on,
            kronos_rank=kronos_rank,
            volume_gate=use_volume,
            pattern_only=pattern_only,
            market=profile.id,
            collect_first=collect_first,
            collect_first_top_n=collect_first_top_n,
            duration_days=duration_days,
        )
        await scanner.run()
    finally:
        account.save()
        if (
            stream_book is not None
            and stream_book._stream_proc is not None
            and stream_book._stream_proc.poll() is None
        ):
            stream_book._stream_proc.terminate()
            stream_book._stream_proc = None
        if export_trades_log:
            stats = scanner.stats if scanner is not None else None
            try:
                _write_trades_log(export_trades_log, account, stats)
            except OSError:
                log.exception(
                    f"Paper | failed to write trades log {export_trades_log}"
                )
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
    kronos_gate: bool | None = None,
    volume_gate: bool | None = None,
    volume_gate_compare: bool = False,
    pattern_only: bool = False,
    market: str | None = None,
) -> None:
    os.makedirs("logs", exist_ok=True)

    profile = get_market(market)
    use_volume = (
        settings.volume_gate_enabled if volume_gate is None else volume_gate
    )
    kronos_gate_on = (
        profile.kronos_gate_default if kronos_gate is None else kronos_gate
    )
    if pattern_only:
        if volume_gate is None:
            use_volume = False
        if kronos_gate is None:
            kronos_gate_on = False
    title = f"BACKTEST MODE{' — ' + pattern if pattern else ''}"
    universe_note = (
        "top by peso volume" if profile.id == "ph" else "top by market cap"
    )
    log.info("=" * 60)
    log.info(f"  Trading Bot — {title}")
    log.info(f"  Market:     {profile.label} ({profile.currency})")
    log.info(f"  Symbols:    {n_symbols} {universe_note}")
    log.info(f"  Kronos gate:{'ON' if kronos_gate_on else 'OFF'}")
    log.info(f"  Kronos rank:{'ON' if profile.kronos_rank_default else 'OFF'}")
    log.info(f"  Long-only:  {'YES' if profile.long_only else 'no'}")
    log.info(f"  Txn cost:   {profile.txn_cost_pct:.4f} one-way")
    log.info(f"  Volume gate:{'ON' if use_volume else 'OFF'}"
             f"{' (A/B compare)' if volume_gate_compare else ''}")
    log.info(f"  Pattern-only:{'ON' if pattern_only else 'OFF'}")
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
        kronos_gate=kronos_gate_on,
        kronos_rank=profile.kronos_rank_default,
        pattern_only=pattern_only,
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
        const=50,
        type=int,
        default=None,
        metavar="N",
        help="Run backtest on top N liquid symbols (default: 50). "
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
        "--kronos-gate",
        action="store_true",
        help="Enable the Kronos 3d confirm gate for this run "
        "(overrides the market profile default). Use with --backtest / --paper.",
    )
    parser.add_argument(
        "--volume-gate",
        action="store_true",
        help="Enable the RVOL+OBV volume confirm gate for this run "
        "(overrides VOLUME_GATE_ENABLED). Use with --backtest / --paper.",
    )
    parser.add_argument(
        "--pattern-only",
        action="store_true",
        help="Skip min share-price, SMA200 regime, min confidence, cooldown, "
        "and long-only. Kronos and volume confirm gates default OFF unless "
        "--kronos-gate / --volume-gate are passed. Use with --backtest / --paper.",
    )
    parser.add_argument(
        "--symbols",
        type=int,
        default=None,
        metavar="N",
        help="Universe size (top N liquid names). Overrides the optional N on "
        "--paper / --backtest. E.g. --paper --symbols=500.",
    )
    parser.add_argument(
        "--collect-first",
        nargs="?",
        const=_COLLECT_FIRST_USE_DEFAULT,
        type=int,
        default=None,
        metavar="N",
        help="Collect chart-pattern signals during a scan without opening "
        "anything, rank them by reward:risk, and open only the top N "
        "(default: COLLECT_FIRST_TOP_N). "
        "--collect-first=4 is the same as --collect-first --collect-first-top-n 4. "
        "Use with --paper / scan mode.",
    )
    parser.add_argument(
        "--collect-first-top-n",
        type=int,
        default=None,
        metavar="N",
        help="With --collect-first (no N), how many top-ranked signals to open "
        "(default: COLLECT_FIRST_TOP_N). Ignored when --collect-first=N is set.",
    )
    parser.add_argument(
        "--stream",
        nargs="?",
        const="",
        default=None,
        metavar="DATE",
        help="With --paper, replay historical daily bars via the paper-trade "
        "stream (starts the stream server if needed). DATE is MM/DD/YYYY or "
        "YYYY-MM-DD; omitted DATE uses PAPERTRADE_STREAM_START_DATE. "
        "E.g. --stream=01/05/2026.",
    )
    parser.add_argument(
        "--export-trades-log",
        type=str,
        default=None,
        metavar="PATH",
        help="With --paper, write open+closed trades JSON to PATH on exit "
        "(same schema as the UI Export Trades button). "
        "E.g. --export-trades-log=output_trades.json.",
    )
    parser.add_argument(
        "--duration-days",
        type=int,
        default=None,
        metavar="N",
        help="With --paper, stop after N unique market sessions "
        "(stream replay: N daily bars). E.g. --duration-days=30.",
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
        const=50,
        type=int,
        default=None,
        metavar="N",
        help="Run paper trading on top N liquid symbols (default: 50) — live scan, "
        "simulated fills, no real broker. Combine with --symbols, --stream, "
        "--pattern-only, --collect-first, --duration-days, --export-trades-log.",
    )
    parser.add_argument(
        "--paper-reset",
        action="store_true",
        help="Wipe the saved paper-trading account and start fresh (use with --paper).",
    )
    parser.add_argument(
        "--kronos-test",
        nargs="?",
        const=20,
        type=int,
        default=None,
        metavar="N",
        help="Evaluate Kronos-base's +1 day / +3 trading-day close-price forecast accuracy "
        "on N randomly sampled symbols (default: 20) from stocks_history.",
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
        "stocks_history. Saves to ~/Kronos/finetuned/. Needs a CUDA GPU to "
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
        help="Run the paper trade stream server: replays daily bars from "
        "GET /api/history (https://33ai.edos.uk by default) over a local "
        "WebSocket so paper trading (--paper / --ui) can run with the "
        "'Use paper trade stream' option even when US markets are closed.",
    )
    parser.add_argument(
        "--papertrade-stream-start",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="With --papertrade-stream, start replaying each tape from this date "
        "(first bar on/after). Default: PAPERTRADE_STREAM_START_DATE, or near "
        "the end of each tape.",
    )
    parser.add_argument(
        "--check-db",
        action="store_true",
        help="Check the stock-history DB is current: global + per-symbol stats "
        "and freshness. Non-zero exit if stale.",
    )
    parser.add_argument(
        "--check-db-stale-days",
        type=int,
        default=7,
        metavar="N",
        help="With --check-db, flag a market as stale when its last bar is "
        "older than N days vs that market's last closed session (default: 7).",
    )
    parser.add_argument(
        "--check-db-market",
        choices=["us", "ph"],
        default=None,
        help="With --check-db, only report this market (default: both).",
    )
    parser.add_argument(
        "--update-db",
        action="store_true",
        help="Incremental daily update: Yahoo v8 / PSE Edge fetch for every symbol "
        "whose last bar is older than the last trading day. Unbounded — updates "
        "all symbols.",
    )
    parser.add_argument(
        "--update-db-no-fetch",
        action="store_true",
        help="With --update-db, skip the Yahoo/PSE fetch.",
    )
    parser.add_argument(
        "--update-db-fetch-limit",
        type=int,
        default=None,
        metavar="N",
        help="With --update-db, cap the fetch to the top N stale symbols "
        "(by recency). Default: all stale symbols (no cap).",
    )
    parser.add_argument(
        "--ingest-pse",
        action="store_true",
        help="Create local stocks_history if needed and load PSE Edge daily "
        "bars as TICKER.PS (market=ph).",
    )
    parser.add_argument(
        "--ingest-pse-symbols",
        type=str,
        default="",
        help="With --ingest-pse, comma-separated tickers (default: full Edge directory).",
    )
    parser.add_argument(
        "--ingest-pse-limit",
        type=int,
        default=None,
        metavar="N",
        help="With --ingest-pse, only the first N directory tickers (spike).",
    )
    parser.add_argument(
        "--export-pse",
        type=str,
        default=None,
        metavar="DIR",
        help="Write PH-only symbols.csv + daily_bars.csv to DIR.",
    )
    parser.add_argument(
        "--import-pse",
        type=str,
        default=None,
        metavar="DIR",
        help="Load PH CSV dump into stocks_history (refuses US / bare tickers).",
    )
    return parser.parse_args(argv)


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

        await run_stream_server(
            start_date=args.papertrade_stream_start, market=args.market,
        )
        return

    if args.check_db:
        from data.check import run_check

        rc = run_check(
            stale_days=args.check_db_stale_days,
            market=args.check_db_market,
        )
        raise SystemExit(rc)

    if args.update_db:
        from data.update import run_update

        run_update(
            fetch=not args.update_db_no_fetch,
            fetch_limit=args.update_db_fetch_limit,
        )
        return

    if args.ingest_pse:
        from data.pse_ingest import run_ingest

        extra = [s.strip() for s in (args.ingest_pse_symbols or "").split(",") if s.strip()]
        run_ingest(
            symbols=extra or None,
            limit=args.ingest_pse_limit,
        )
        return

    if args.export_pse:
        from pathlib import Path
        from data.pse_ingest import export_ph

        export_ph(Path(args.export_pse))
        return

    if args.import_pse:
        from pathlib import Path
        from data.pse_ingest import import_ph

        import_ph(Path(args.import_pse))
        return

    if args.stream is not None and args.paper is None:
        log.error("--stream requires --paper")
        raise SystemExit(2)
    if args.export_trades_log and args.paper is None:
        log.error("--export-trades-log requires --paper")
        raise SystemExit(2)
    if args.duration_days is not None and args.paper is None:
        log.error("--duration-days requires --paper")
        raise SystemExit(2)
    if args.duration_days is not None and args.duration_days < 1:
        log.error("--duration-days must be >= 1")
        raise SystemExit(2)
    if args.symbols is not None and args.symbols < 1:
        log.error("--symbols must be >= 1")
        raise SystemExit(2)

    collect_first, collect_first_top_n = _resolve_collect_first(args)
    stream_start = None
    use_stream = args.stream is not None
    if use_stream and args.stream:
        try:
            stream_start = parse_stream_date(args.stream)
        except argparse.ArgumentTypeError as exc:
            log.error(str(exc))
            raise SystemExit(2)

    if args.paper is not None:
        await run_paper(
            n_symbols=_resolve_n_symbols(args, args.paper),
            reset=args.paper_reset,
            kronos_gate=True if args.kronos_gate else None,
            volume_gate=True if args.volume_gate else None,
            pattern_only=args.pattern_only,
            market=args.market,
            collect_first=collect_first,
            collect_first_top_n=collect_first_top_n,
            use_stream=use_stream,
            stream_start=stream_start,
            export_trades_log=args.export_trades_log,
            duration_days=args.duration_days,
        )
    elif args.backtest is not None:
        await run_backtest(
            n_symbols=_resolve_n_symbols(args, args.backtest),
            pattern=args.pattern,
            kronos_gate=True if args.kronos_gate else None,
            volume_gate=True if args.volume_gate else None,
            volume_gate_compare=args.volume_gate_compare,
            pattern_only=args.pattern_only,
            market=args.market,
        )
    else:
        await run_scanner(
            n_symbols=_resolve_n_symbols(args),
            volume_gate=True if args.volume_gate else None,
            market=args.market,
            collect_first=collect_first,
            collect_first_top_n=collect_first_top_n,
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
