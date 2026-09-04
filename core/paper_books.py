"""Dual-market paper books: one US thread, one PH thread.

`--ui` and `--web` both drive this manager. Ledgers stay separate
(`paper_account.json` vs `paper_account_ph.json`). Currencies never mix.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt

from config import settings, DISABLED_PATTERNS
from core.market import (
    MARKET_PH,
    MARKET_US,
    clock_payload,
    get_market,
    session_label,
    session_window,
)
from core.paper_trader import (
    PaperAccount,
    days_held,
    sim_days_held,
    bars_held,
    position_status,
    r_multiple,
    risk_dollars,
    unrealized_pct,
)
from core.scanner import MarketScanner
from core.signal_log_store import load_signal_log, reset_signal_log
from data.edgar_client import set_skip_edgar
from data.stream_client import StreamClient
from data.tv_client import TVClient
from utils.logger import log

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOK_IDS = (MARKET_US, MARKET_PH)


class PaperBook:
    """One market's paper session: own thread, asyncio loop, scanner, ledger."""

    def __init__(self, market: str) -> None:
        profile = get_market(market)
        self.market = profile.id
        self.lock = threading.Lock()
        self.account = PaperAccount.load(market=self.market)
        self.scanner: Optional[MarketScanner] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.status = "Idle"
        self.use_stream = False
        self._stream_proc: Optional[subprocess.Popen] = None
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._chart_key: tuple[int, float] | None = None
        self._chart_b64: Optional[str] = None

    def snapshot(self, *, stream_blocked_by: Optional[str] = None) -> dict[str, Any]:
        with self.lock:
            account = self.account
            scanner = self.scanner
            use_stream = self.use_stream
            status = self.status
            running = self.running
            error = self.error

        profile = get_market(account.market)
        clock = clock_payload(profile.id)
        now = datetime.now(timezone.utc)
        # Single atomic read: equity, exposure, per-position MTM, and the
        # realized/unrealized totals below all derive from this one frozen
        # snapshot so the header figures and the position-row figures can
        # never disagree, even while a scan thread is actively repricing
        # positions in the background (see snapshot_metrics docstring).
        snap = account.snapshot_metrics()
        equity = snap["equity"]
        exp = snap["exposure"]
        positions = []
        for sym, p, current, mtm in snap["positions"]:
            r = r_multiple(p, current)
            risk = risk_dollars(p)
            value = current * p.qty
            positions.append(
                {
                    "market": profile.id,
                    "symbol": sym,
                    "status": position_status(p),
                    "action": p.action,
                    "pattern": p.pattern,
                    "qty": p.qty,
                    "entry": p.entry_price,
                    "current": current,
                    "unrl_pct": unrealized_pct(p, current),
                    "r": r,
                    "days": sim_days_held(p, account.sim_now() or now),
                    "bars": bars_held(p, account.bar_count(sym)),
                    "value": value,
                    "mtm": mtm,
                    "port_pct": (value / equity * 100) if equity > 0 else 0.0,
                    "risk": risk,
                    "stop": p.stop_loss,
                    "target": p.take_profit,
                    "opened": p.entry_date.isoformat(),
                    "sim_opened": p.sim_entry_date.isoformat() if p.sim_entry_date else None,
                    "timeframe": p.timeframe,
                    "daily_marks": list(p.position_marks or []),
                }
            )

        closed = []
        for t in snap["closed"]:
            exit_px = t.exit_price if t.exit_price is not None else t.entry_price
            closed.append(
                {
                    "market": profile.id,
                    "symbol": t.symbol,
                    "action": t.action,
                    "pattern": t.pattern,
                    "qty": t.qty,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "pnl_pct": t.pnl_pct,
                    "pnl": t.pnl * t.qty,
                    "r": r_multiple(t, exit_px),
                    "days": days_held(t),
                    "bars": bars_held(t),
                    "time_exit_bars_elapsed": t.time_exit_bars_elapsed,
                    "time_exit_bars_configured": t.exit_bars_after_neckline_break,
                    "reason": t.exit_reason,
                    "stop": t.stop_loss,
                    "target": t.take_profit,
                    "opened": t.entry_date.isoformat() if t.entry_date else "",
                    "closed": t.exit_date.isoformat() if t.exit_date else "",
                    "sim_opened": t.sim_entry_date.isoformat() if t.sim_entry_date else None,
                    "sim_closed": t.sim_exit_date.isoformat() if t.sim_exit_date else None,
                    "timeframe": t.timeframe,
                    "daily_marks": list(t.position_marks or []),
                }
            )

        result = account.to_result()
        stats = scanner.stats if scanner is not None else None
        signal_logs = []
        for row in reversed(load_signal_log(profile.id)):
            entry = dict(row)
            entry["market"] = profile.id
            signal_logs.append(entry)
        curve_b64 = self._equity_chart_b64(account)

        total_pnl = snap["total_pnl_dollars"]
        metrics = {
            "total_pnl_dollars": total_pnl,
            "total_pnl_pct": (total_pnl / snap["initial_capital"] * 100)
            if snap["initial_capital"] else 0.0,
            "realized_pnl_dollars": snap["realized_pnl_dollars"],
            "unrealized_pnl_dollars": snap["unrealized_pnl_dollars"],
            "avg_r": result.avg_r,
            "median_r": result.median_r,
            "avg_hold_bars": result.avg_hold_bars,
            "exit_reason_breakdown": result.exit_reason_breakdown,
            "max_drawdown_pct": result.max_drawdown_pct,
            "sharpe_ratio": result.sharpe_ratio,
        }
        window = session_window(profile.id)
        session_idle = (
            profile.id == MARKET_PH
            and running
            and window == "closed"
            and not use_stream
        )
        display_status = status
        if session_idle and not error:
            display_status = "PH session closed — scanner idle until AM/PM"
        return {
            "running": running,
            "status": display_status,
            "error": error,
            "use_stream": use_stream,
            "cash": snap["cash"],
            "equity": equity,
            "open_count": len(snap["positions"]),
            "closed_count": len(snap["closed"]),
            "exposure": exp,
            "scan_stats": stats,
            "positions": positions,
            "closed": closed,
            "signal_logs": signal_logs,
            "summary": result.summary() if result.trades else "No closed trades yet.",
            "metrics": metrics,
            "equity_png_b64": curve_b64,
            "market": profile.id,
            "label": profile.label,
            "currency": profile.currency,
            "currency_symbol": profile.currency_symbol,
            "session": clock["session"],
            "session_open": clock["session_open"],
            "session_idle": session_idle,
            "local_time": clock["local_time"],
            "tz_name": clock["tz_name"],
            "long_only": profile.long_only,
            "stream_blocked_by": stream_blocked_by,
            "defaults": {
                "kronos_gate": profile.kronos_gate_default,
                "kronos_rank": profile.kronos_rank_default,
                "kronos_batch": settings.kronos_batch_enabled,
                "volume_gate": settings.volume_gate_enabled,
                "n_symbols": profile.default_n_symbols,
            },
        }

    def _equity_chart_b64(self, account: PaperAccount) -> Optional[str]:
        curve = account.equity_curve_snapshot()
        if not curve or len(curve) < 2:
            self._chart_key = None
            self._chart_b64 = None
            return None
        try:
            ys: list[float] = []
            for point in curve:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    ys.append(float(point[1]))
                else:
                    ys.append(float(point))
            if len(ys) < 2:
                return None
            key = (len(ys), round(ys[-1], 6))
            if key == self._chart_key:
                return self._chart_b64
            fig, ax = plt.subplots(figsize=(6, 2.8), dpi=100)
            xs = list(range(len(ys)))
            ax.plot(xs, ys, color="#1b6fc0", linewidth=1.5)
            title = f"{get_market(account.market).label} equity"
            ax.set_title(title, fontsize=10)
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            buf = io.BytesIO()
            fig.savefig(buf, format="png")
            plt.close(fig)
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            self._chart_key = key
            self._chart_b64 = encoded
            return encoded
        except Exception:
            log.exception("PaperBook | equity chart failed")
            return None

    def render_trade_chart(
        self,
        *,
        side: str,
        symbol: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        from analysis.chart_renderer import build_trade_viewer_payload

        with self.lock:
            account = self.account
            scanner = self.scanner

        trade = None
        current = None
        view_side = side
        if side == "open":
            if not symbol:
                return {"error": "symbol is required for open charts"}
            trade = account.positions.get(symbol.upper()) or account.positions.get(symbol)
            if trade is None:
                return {"error": f"no open position in {symbol}"}
            current = account.last_price(trade.symbol, trade.entry_price)
        elif side == "closed":
            closed = account.closed
            if index is None or index < 0 or index >= len(closed):
                return {"error": "closed chart needs a valid row index"}
            trade = closed[index]
        elif side == "log":
            if not symbol:
                return {"error": "symbol is required for log charts"}
            trade = account.positions.get(symbol.upper()) or account.positions.get(symbol)
            if trade is not None:
                current = account.last_price(trade.symbol, trade.entry_price)
                view_side = "open"
            else:
                needle = symbol.upper()
                for closed_trade in reversed(account.closed):
                    if str(closed_trade.symbol).upper() == needle:
                        trade = closed_trade
                        view_side = "closed"
                        break
                if trade is None:
                    return self._chart_from_log_symbol(account, scanner, symbol)
        else:
            return {"error": "side must be open, closed, or log"}

        timeframe = trade.timeframe or "1d"
        df = None
        session_tz = get_market(account.market).session_tz
        if scanner is not None:
            df = scanner.ohlcv_frame(trade.symbol, timeframe, min_bars=2)
        if df is None or len(df) < 2:
            from data.history import load_daily_ohlcv_df
            df = load_daily_ohlcv_df(
                trade.symbol, tv_fallback=False, market=account.market,
            )
        if df is None or len(df) < 2:
            return {"error": f"no OHLCV for {trade.symbol} {timeframe}"}

        try:
            return build_trade_viewer_payload(
                df,
                symbol=trade.symbol,
                timeframe=timeframe,
                pattern=trade.pattern,
                action=trade.action,
                session_tz=session_tz,
                entry=trade.entry_price,
                stop=trade.stop_loss,
                target=trade.take_profit,
                exit_price=trade.exit_price if view_side == "closed" else None,
                exit_reason=trade.exit_reason if view_side == "closed" else None,
                current=current,
                entry_time=trade.sim_entry_date or trade.entry_date,
                exit_time=(
                    None if view_side == "open"
                    else (trade.sim_exit_date or trade.exit_date)
                ),
            )
        except Exception as exc:
            log.exception("PaperBook | trade chart payload failed")
            return {"error": f"chart data failed: {exc}"}

    def _chart_from_log_symbol(
        self,
        account: PaperAccount,
        scanner: Optional[MarketScanner],
        symbol: str,
    ) -> dict[str, Any]:
        from analysis.chart_renderer import build_trade_viewer_payload

        needle = symbol.upper()
        log_row: dict[str, Any] = {}
        for row in reversed(load_signal_log(account.market)):
            if str(row.get("symbol") or "").upper() == needle:
                log_row = row
                break
        ticker = str(log_row.get("symbol") or symbol)
        timeframe = str(log_row.get("timeframe") or "1d")
        df = None
        session_tz = get_market(account.market).session_tz
        if scanner is not None:
            df = scanner.ohlcv_frame(ticker, timeframe, min_bars=2)
        if df is None or len(df) < 2:
            from data.history import load_daily_ohlcv_df
            df = load_daily_ohlcv_df(
                ticker, tv_fallback=False, market=account.market,
            )
        if df is None or len(df) < 2:
            return {"error": f"no OHLCV for {ticker} {timeframe}"}
        price = log_row.get("price")
        try:
            entry = float(price) if price is not None else None
        except (TypeError, ValueError):
            entry = None
        try:
            return build_trade_viewer_payload(
                df,
                symbol=ticker,
                timeframe=timeframe,
                pattern=log_row.get("pattern"),
                action=log_row.get("action"),
                session_tz=session_tz,
                entry=entry,
                entry_time=log_row.get("sim_bar") or log_row.get("ts"),
            )
        except Exception as exc:
            log.exception("PaperBook | trade chart payload failed")
            return {"error": f"chart data failed: {exc}"}

    def start(
        self,
        n_symbols: int,
        *,
        extra_symbols: str = "",
        use_stream: bool,
        kronos_gate: bool,
        kronos_rank: bool,
        kronos_batch: bool = False,
        volume_gate: bool,
        pattern_only: bool = False,
        collect_first: bool = False,
        collect_first_top_n: int = 4,
        stream_start: Optional[str] = None,
    ) -> str | None:
        with self.lock:
            if self.running:
                return f"{self.market.upper()} paper session already running."
            self.running = True
            self.error = None
            self.use_stream = use_stream
            self.status = "Fetching symbols..."
        self._thread = threading.Thread(
            target=self._run_thread,
            args=(
                n_symbols, extra_symbols, use_stream, kronos_gate, kronos_rank,
                kronos_batch, volume_gate, pattern_only, collect_first,
                collect_first_top_n, stream_start,
            ),
            name=f"paper-{self.market}",
            daemon=True,
        )
        self._thread.start()
        return None

    def stop(self) -> None:
        with self.lock:
            loop, task = self.loop, self.task
            if not self.running or loop is None or task is None:
                return
            self.status = "Stopping..."
        loop.call_soon_threadsafe(task.cancel)

    def reset(self) -> str | None:
        with self.lock:
            if self.running:
                return f"Stop the {self.market.upper()} session before resetting."
            profile = get_market(self.market)
            self.account = PaperAccount(market=profile.id)
            self.account.save()
            self.status = f"{profile.label} account reset."
            self.error = None
            scanner = self.scanner
        reset_signal_log(self.market)
        if scanner is not None:
            scanner.clear_signal_log_memory()
        return None

    def reset_logs(self) -> None:
        reset_signal_log(self.market)
        with self.lock:
            scanner = self.scanner
        if scanner is not None:
            scanner.clear_signal_log_memory()

    @staticmethod
    def _port_open(host: str, port: int) -> bool:
        """TCP probe — must stay sync; callers already sit on an asyncio loop."""
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    @staticmethod
    def _kill_whatever_is_on(port: int) -> None:
        try:
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    def _ensure_stream_server(self, start_date: Optional[str] = None) -> Optional[str]:
        host, port = settings.papertrade_stream_host, settings.papertrade_stream_port
        self._kill_whatever_is_on(port)
        time.sleep(0.3)
        with self.lock:
            self.status = (
                f"Starting paper trade stream server (from {start_date})..."
                if start_date
                else "Starting paper trade stream server..."
            )
        cmd = [
            sys.executable, "main.py", "--papertrade-stream",
            "--market", self.market,
        ]
        if start_date:
            cmd.extend(["--papertrade-stream-start", start_date])
        env = os.environ.copy()
        env["MARKET"] = self.market
        url = (settings.stocks_history_url or "").strip()
        if url:
            env["STOCKS_HISTORY_URL"] = url
        self._stream_proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env)
        for _ in range(20):
            if self._port_open(host, port):
                return None
            if self._stream_proc.poll() is not None:
                return "Paper trade stream server exited immediately — check logs."
            time.sleep(0.5)
        return f"Paper trade stream server didn't come up on {host}:{port} in time."

    def _run_thread(
        self,
        n_symbols: int,
        extra_symbols: str,
        use_stream: bool,
        kronos_gate: bool,
        kronos_rank: bool,
        kronos_batch: bool,
        volume_gate: bool,
        pattern_only: bool,
        collect_first: bool,
        collect_first_top_n: int,
        stream_start: Optional[str],
    ) -> None:
        data_feed = None
        profile = get_market(self.market)
        set_skip_edgar(profile.skip_edgar)
        with self.lock:
            self.account = PaperAccount.load(market=profile.id)
        effective_stream_start = stream_start
        if use_stream and self.account.sim_now() is not None:
            resume_from = self.account.sim_now()
            if resume_from is not None:
                resume_date = resume_from.astimezone(
                    ZoneInfo(profile.session_tz)
                ).date()
                configured_date = None
                if stream_start:
                    try:
                        configured_date = datetime.strptime(
                            stream_start, "%Y-%m-%d"
                        ).date()
                    except ValueError:
                        configured_date = None
                if configured_date is None or configured_date <= resume_date:
                    effective_stream_start = resume_date.isoformat()

        if use_stream:
            error = self._ensure_stream_server(start_date=effective_stream_start)
            if error:
                with self.lock:
                    self.running = False
                    self.error = error
                    self.status = error
                return
            data_feed = StreamClient()

        symbol_rows = TVClient.fetch_universe_cached(
            n_symbols, profile.id, extra_symbols=extra_symbols,
        )
        if not symbol_rows:
            with self.lock:
                self.running = False
                self.error = "No symbols from screener or additional list."
                self.status = self.error
            return
        symbols = [s for s, _ex in symbol_rows]
        exchange_overrides = dict(symbol_rows)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self.lock:
            self.loop = loop
            self.account = PaperAccount.load(market=profile.id)
            self.account.assume_session_open = bool(use_stream)
            scanner = MarketScanner(
                symbols=symbols,
                exchange_overrides=exchange_overrides,
                paper_account=self.account,
                disabled_patterns=DISABLED_PATTERNS,
                data_feed=data_feed,
                scan_interval_seconds=(
                    settings.papertrade_stream_interval_seconds if use_stream else profile.scan_interval_seconds
                ),
                kronos_gate=kronos_gate,
                kronos_rank=kronos_rank,
                kronos_batch=kronos_batch,
                volume_gate=volume_gate,
                pattern_only=pattern_only,
                collect_first=collect_first,
                collect_first_top_n=collect_first_top_n,
                market=profile.id,
            )
            self.scanner = scanner
            self.task = loop.create_task(scanner.run())
            interval = (
                settings.papertrade_stream_interval_seconds
                if use_stream
                else profile.scan_interval_seconds
            )
            stream_note = (
                f", stream from {effective_stream_start}"
                if use_stream and effective_stream_start else ""
            )
            if use_stream and interval <= 0:
                pace = "scan-paced replay"
            else:
                pace = f"scanning every {interval}s"
            self.status = (
                f"Running — {profile.label}, {len(symbols)} symbols, {pace}"
                f"{stream_note}"
                f", Kronos gate={'ON' if kronos_gate else 'OFF'}"
                f", Kronos rank={'ON' if kronos_rank else 'OFF'}"
                f", Kronos batch={'ON' if kronos_batch else 'OFF'}"
                f", Volume gate={'ON' if volume_gate else 'OFF'}"
                f", Pattern-only={'ON' if pattern_only else 'OFF'}"
                f", Collect-first={'ON' if collect_first else 'OFF'}"
                f", session={session_label(profile.id)}"
            )

        error_msg: Optional[str] = None
        try:
            loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except BaseException:
            log.exception(f"PaperBook | {profile.id} scanner crashed")
            error_msg = "Scanner crashed. Check server logs for details."
        finally:
            self.account.save()
            loop.close()
            with self.lock:
                self.running = False
                self.loop = None
                self.task = None
                self.error = error_msg
                self.status = error_msg or "Stopped."
                if self._stream_proc is not None and self._stream_proc.poll() is None:
                    self._stream_proc.terminate()
                self._stream_proc = None


class PaperBookManager:
    """US + PH books. Start/stop/reset are per-market; stream is exclusive."""

    def __init__(self) -> None:
        self.books: dict[str, PaperBook] = {
            MARKET_US: PaperBook(MARKET_US),
            MARKET_PH: PaperBook(MARKET_PH),
        }

    def _book(self, market: str | None) -> PaperBook:
        profile = get_market(market)
        return self.books[profile.id]

    def stream_holder(self) -> Optional[str]:
        for mid, book in self.books.items():
            if book.running and book.use_stream:
                return mid
        return None

    def any_running(self) -> bool:
        return any(b.running for b in self.books.values())

    def snapshot(self, market: str) -> dict[str, Any]:
        holder = self.stream_holder()
        book = self._book(market)
        blocked = holder if holder and holder != book.market else None
        return book.snapshot(stream_blocked_by=blocked)

    def snapshot_all(self) -> dict[str, Any]:
        holder = self.stream_holder()
        books = {}
        clocks = {}
        for mid in BOOK_IDS:
            blocked = holder if holder and holder != mid else None
            snap = self.books[mid].snapshot(stream_blocked_by=blocked)
            books[mid] = snap
            clocks[mid] = {
                "local_time": snap["local_time"],
                "tz_name": snap["tz_name"],
                "session": snap["session"],
                "session_open": snap["session_open"],
                "running": snap["running"],
            }
        return {"clocks": clocks, "books": books}

    def lamps(self) -> dict[str, Any]:
        """Nav-lamp payload: running flags only — no matplotlib or blotter."""
        books = {}
        for mid in BOOK_IDS:
            book = self.books[mid]
            with book.lock:
                running = book.running
            books[mid] = {"running": running}
        return {"books": books}

    def start(
        self,
        market: str,
        n_symbols: int,
        *,
        extra_symbols: str = "",
        use_stream: bool,
        kronos_gate: bool,
        kronos_rank: bool,
        kronos_batch: bool = False,
        volume_gate: bool,
        pattern_only: bool = False,
        collect_first: bool = False,
        collect_first_top_n: int = 4,
        stream_start: Optional[str] = None,
    ) -> str | None:
        book = self._book(market)
        if use_stream:
            holder = self.stream_holder()
            if holder and holder != book.market:
                return (
                    f"Paper stream is already in use by {holder.upper()}. "
                    f"Stop {holder.upper()} or run {book.market.upper()} live."
                )
        return book.start(
            n_symbols,
            extra_symbols=extra_symbols,
            use_stream=use_stream,
            kronos_gate=kronos_gate,
            kronos_rank=kronos_rank,
            kronos_batch=kronos_batch,
            volume_gate=volume_gate,
            pattern_only=pattern_only,
            collect_first=collect_first,
            collect_first_top_n=collect_first_top_n,
            stream_start=stream_start,
        )

    def start_both(self, specs: dict[str, dict[str, Any]]) -> dict[str, str]:
        """Start each market present in specs. Returns {market: error} for failures."""
        errors: dict[str, str] = {}
        for mid in BOOK_IDS:
            payload = specs.get(mid)
            if not payload:
                continue
            err = self.start(
                mid,
                int(payload.get("n_symbols") or get_market(mid).default_n_symbols),
                extra_symbols=payload.get("extra_symbols") or "",
                use_stream=bool(payload.get("use_stream")),
                kronos_gate=bool(payload.get("kronos_gate")),
                kronos_rank=bool(payload.get("kronos_rank")),
                kronos_batch=bool(payload.get("kronos_batch")),
                volume_gate=bool(payload.get("volume_gate")),
                pattern_only=bool(payload.get("pattern_only")),
                collect_first=bool(payload.get("collect_first")),
                collect_first_top_n=int(payload.get("collect_first_top_n") or 4),
                stream_start=payload.get("stream_start"),
            )
            if err:
                errors[mid] = err
        return errors

    def stop(self, market: str | None = "all") -> None:
        if market in (None, "", "all"):
            self.stop_all()
            return
        self._book(market).stop()

    def stop_all(self) -> None:
        for book in self.books.values():
            book.stop()

    def reset(self, market: str) -> str | None:
        return self._book(market).reset()

    def reset_logs(self, market: str | None = "all") -> None:
        if market in (None, "", "all"):
            for book in self.books.values():
                book.reset_logs()
            return
        self._book(market).reset_logs()

    def chart(
        self,
        market: str,
        *,
        side: str,
        symbol: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        return self._book(market).render_trade_chart(
            side=side, symbol=symbol, index=index,
        )

    def export_trades(self, market: str | None = None) -> dict[str, Any]:
        from utils.trade_export import build_paper_trade_export
        return build_paper_trade_export(self.snapshot_all(), market=market)


paper_books = PaperBookManager()
