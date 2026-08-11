"""Background job managers for web backtest + paper trading."""

from __future__ import annotations

import asyncio
import base64
import io
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
import websockets

from config import settings, DISABLED_PATTERNS
from core.backtester import Backtester, BacktestResult, discover_pattern_names
from core.paper_trader import (
    PaperAccount,
    days_held,
    position_status,
    r_multiple,
    risk_dollars,
    unrealized_pct,
)
from core.scanner import MarketScanner
from data.stream_client import StreamClient
from data.tv_client import TVClient
from ui.backtest_dialog import PARAMS
from utils.logger import log

REPO_ROOT = Path(__file__).resolve().parent.parent


def backtest_param_schema() -> list[dict[str, Any]]:
    """JSON-friendly copy of BacktestDialog PARAMS for the web form."""
    out: list[dict[str, Any]] = []
    for key, label, desc, ptype, default, choices in PARAMS:
        entry: dict[str, Any] = {
            "key": key,
            "label": label,
            "description": desc,
            "type": ptype,
        }
        if key == "pattern_filter":
            entry["choices"] = [""] + discover_pattern_names()
            entry["default"] = default
        elif ptype == "spin":
            default_val, minv, maxv, inc = default
            entry["default"] = default_val
            entry["min"] = minv
            entry["max"] = maxv
            entry["step"] = inc
        elif ptype == "combo":
            entry["default"] = default
            entry["choices"] = choices or []
        elif ptype == "check":
            entry["default"] = bool(default)
        else:
            entry["default"] = default
        out.append(entry)
    return out


def normalize_backtest_form(raw: dict[str, Any]) -> dict[str, Any]:
    """Mirror BacktestDialog._collect_params from posted form values."""
    p: dict[str, Any] = {}
    for key, _label, _desc, ptype, default, _choices in PARAMS:
        if key not in raw and ptype != "check":
            # checkboxes omit unchecked fields
            if ptype == "spin":
                p[key] = default[0]
            else:
                p[key] = default
            continue
        if ptype == "check":
            val = raw.get(key)
            p[key] = str(val).lower() in ("1", "true", "on", "yes")
        elif ptype == "spin":
            default_val, _minv, _maxv, _inc = default
            try:
                p[key] = float(raw.get(key, default_val))
            except (TypeError, ValueError):
                p[key] = default_val
        elif ptype == "combo":
            v = str(raw.get(key) or "").strip()
            p[key] = v if v else None
        else:
            v = str(raw.get(key) or "").strip()
            p[key] = v if v else None

    n_symbols = int(p.pop("n_symbols"))
    if "max_workers" in p and p["max_workers"] is not None:
        p["max_workers"] = int(p["max_workers"])
    pattern_filter = p.pop("pattern_filter")
    disabled_raw = p.pop("disabled_patterns", None)
    if disabled_raw is None:
        # Field omitted from POST — fall back to config DISABLED_PATTERNS
        # (same default the form schema advertises).
        disabled_raw = ",".join(DISABLED_PATTERNS)
    p["disabled_patterns"] = [
        name.strip() for name in str(disabled_raw).split(",") if name.strip()
    ]
    for opt_key in (
        "breakeven_trigger_pct",
        "min_atr_stop_multiple",
        "min_reward_risk_ratio",
        "hard_stop_percentage",
        "atr_stop_floor_multiple",
    ):
        if opt_key in p and p[opt_key] is not None and p[opt_key] <= 0:
            p[opt_key] = None
    if "synthetic_stop_multiple" in p and p["synthetic_stop_multiple"] <= 0:
        p["synthetic_stop_multiple"] = 0
    return {"n_symbols": n_symbols, "pattern": pattern_filter, "kwargs": p}


def _result_to_payload(result: BacktestResult) -> dict[str, Any]:
    trades = []
    for t in sorted(result.trades, key=lambda x: x.entry_date):
        trades.append(
            {
                "date": t.entry_date.strftime("%Y-%m-%d"),
                "action": t.action,
                "symbol": t.symbol,
                "tf": t.timeframe,
                "entry": round(t.entry_price, 4),
                "exit": round(t.exit_price, 4),
                "pnl_pct": round(t.pnl_pct, 4),
                "reason": t.exit_reason,
                "pattern": t.pattern,
            }
        )
    return {
        "summary": result.summary(),
        "win_rate": result.win_rate,
        "win_count": result.win_count,
        "loss_count": result.loss_count,
        "trade_count": len(result.trades),
        "trades": trades,
        "dict": result.to_dict(),
    }


class BacktestJob:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.busy = False
        self.mode = ""  # "run" | "ab"
        self.status = "Idle"
        self.completed = 0
        self.total = 0
        self.started_at: Optional[float] = None
        self.error: Optional[str] = None
        self.result: Optional[dict[str, Any]] = None
        self.ab: Optional[dict[str, Any]] = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            elapsed = (time.time() - self.started_at) if self.started_at else 0.0
            pct = (self.completed / self.total * 100) if self.total else 0.0
            eta = None
            if self.busy and self.completed > 0 and elapsed > 0 and self.total:
                rate = self.completed / elapsed
                remaining = self.total - self.completed
                eta = remaining / rate if rate > 0 else None
            return {
                "busy": self.busy,
                "mode": self.mode,
                "status": self.status,
                "completed": self.completed,
                "total": self.total,
                "pct": pct,
                "elapsed_s": elapsed,
                "eta_s": eta,
                "error": self.error,
                "result": self.result,
                "ab": self.ab,
            }

    def _on_progress(self, completed: int, total: int) -> None:
        with self.lock:
            self.completed = completed
            self.total = total

    def start(self, n_symbols: int, pattern: Optional[str], kwargs: dict, *, ab: bool) -> str | None:
        with self.lock:
            if self.busy:
                return "Backtest already running."
            self.busy = True
            self.mode = "ab" if ab else "run"
            self.status = (
                f"Volume A/B compare (top {n_symbols})..."
                if ab
                else f"Running backtest (top {n_symbols})..."
            )
            self.completed = 0
            self.total = 0
            self.started_at = time.time()
            self.error = None
            self.result = None
            self.ab = None
        threading.Thread(
            target=self._run_ab if ab else self._run,
            args=(n_symbols, pattern, kwargs),
            daemon=True,
        ).start()
        return None

    def _run(self, n_symbols: int, pattern: Optional[str], kwargs: dict) -> None:
        try:
            symbol_rows = TVClient.fetch_top_symbols_with_exchanges_cached(
                n_symbols, settings.tv_screener,
            )
            if not symbol_rows:
                raise RuntimeError("No symbols returned by screener.")
            symbols = [s for s, _ex in symbol_rows]
            backtester = Backtester(
                symbols,
                pattern_filter=pattern,
                progress_callback=self._on_progress,
                **kwargs,
            )
            result = asyncio.run(backtester.run())
            with self.lock:
                self.result = _result_to_payload(result)
                self.status = (
                    f"Done: {result.win_rate:.1%} win rate "
                    f"({result.win_count}W / {result.loss_count}L / {len(result.trades)} total)"
                )
                self.completed = self.total or self.completed
        except Exception as exc:
            log.error(f"Web Backtest | {exc}")
            with self.lock:
                self.error = str(exc)
                self.status = f"ERROR: {exc}"
        finally:
            with self.lock:
                self.busy = False

    def _run_ab(self, n_symbols: int, pattern: Optional[str], kwargs: dict) -> None:
        try:
            from analysis.price_volume import ab_metrics_from_result

            kwargs = dict(kwargs)
            kwargs.pop("volume_gate", None)
            symbol_rows = TVClient.fetch_top_symbols_with_exchanges_cached(
                n_symbols, settings.tv_screener,
            )
            if not symbol_rows:
                raise RuntimeError("No symbols returned by screener.")
            symbols = [s for s, _ex in symbol_rows]

            off_bt = Backtester(
                symbols,
                pattern_filter=pattern,
                volume_gate=False,
                progress_callback=self._on_progress,
                **kwargs,
            )
            result_off = asyncio.run(off_bt.run())
            on_bt = Backtester(
                symbols,
                pattern_filter=pattern,
                volume_gate=True,
                progress_callback=self._on_progress,
                **kwargs,
            )
            result_on = asyncio.run(on_bt.run())
            off_m = ab_metrics_from_result(result_off)
            on_m = ab_metrics_from_result(result_on)
            with self.lock:
                self.result = _result_to_payload(result_on)
                self.ab = {"off": off_m, "on": on_m}
                self.status = "Volume A/B complete."
        except Exception as exc:
            log.error(f"Web Backtest A/B | {exc}")
            with self.lock:
                self.error = str(exc)
                self.status = f"ERROR: {exc}"
        finally:
            with self.lock:
                self.busy = False


class PaperSession:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.account = PaperAccount.load()
        self.scanner: Optional[MarketScanner] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.status = "Idle"
        self.use_stream = False
        self._stream_proc: Optional[subprocess.Popen] = None
        self.error: Optional[str] = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            account = self.account
            scanner = self.scanner
            use_stream = self.use_stream
            status = self.status
            running = self.running
            error = self.error

        now = datetime.now(timezone.utc)
        equity = account.equity()
        exp = account.exposure()
        positions = []
        for sym, p in account.positions_snapshot():
            current = account.last_price(sym, p.entry_price)
            r = r_multiple(p, current)
            risk = risk_dollars(p)
            value = current * p.qty
            mtm = (
                (current - p.entry_price) * p.qty
                if p.action == "BUY"
                else (p.entry_price - current) * p.qty
            )
            positions.append(
                {
                    "symbol": sym,
                    "status": position_status(p),
                    "action": p.action,
                    "pattern": p.pattern,
                    "qty": p.qty,
                    "entry": p.entry_price,
                    "current": current,
                    "unrl_pct": unrealized_pct(p, current),
                    "r": r,
                    "days": days_held(p, now),
                    "value": value,
                    "mtm": mtm,
                    "port_pct": (value / equity * 100) if equity > 0 else 0.0,
                    "risk": risk,
                    "stop": p.stop_loss,
                    "target": p.take_profit,
                    "opened": p.entry_date.isoformat(),
                }
            )

        closed = []
        for t in account.closed:
            exit_px = t.exit_price if t.exit_price is not None else t.entry_price
            closed.append(
                {
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
                    "reason": t.exit_reason,
                    "opened": t.entry_date.strftime("%Y-%m-%d") if t.entry_date else "",
                    "closed": t.exit_date.strftime("%Y-%m-%d") if t.exit_date else "",
                }
            )

        result = account.to_result()
        stats = scanner.stats if scanner is not None else None
        signal_logs = (
            list(reversed(scanner.signal_log_snapshot()))
            if scanner is not None
            else []
        )
        curve_b64 = self._equity_chart_b64(account)

        return {
            "running": running,
            "status": status,
            "error": error,
            "use_stream": use_stream,
            "cash": account.cash,
            "equity": equity,
            "open_count": len(account.positions),
            "closed_count": len(account.closed),
            "exposure": exp,
            "scan_stats": stats,
            "positions": positions,
            "closed": closed,
            "signal_logs": signal_logs,
            "summary": result.summary() if result.trades else "No closed trades yet.",
            "equity_png_b64": curve_b64,
            "defaults": {
                "kronos_gate": settings.kronos_gate_enabled,
                "kronos_rank": settings.kronos_rank_enabled,
                "volume_gate": settings.volume_gate_enabled,
                "n_symbols": 100,
            },
        }

    @staticmethod
    def _equity_chart_b64(account: PaperAccount) -> Optional[str]:
        # equity_curve is list[(iso_ts, equity)] — plot values only.
        curve = account.equity_curve_snapshot()
        if not curve or len(curve) < 2:
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
            fig, ax = plt.subplots(figsize=(6, 2.8), dpi=100)
            xs = list(range(len(ys)))
            ax.plot(xs, ys, color="#1b6fc0", linewidth=1.5)
            ax.set_title("Account equity", fontsize=10)
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            buf = io.BytesIO()
            fig.savefig(buf, format="png")
            plt.close(fig)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            log.exception("Web Paper | equity chart failed")
            return None

    def start(
        self,
        n_symbols: int,
        *,
        use_stream: bool,
        kronos_gate: bool,
        kronos_rank: bool,
        volume_gate: bool,
        stream_start: Optional[str] = None,
    ) -> str | None:
        with self.lock:
            if self.running:
                return "Paper session already running."
            self.running = True
            self.error = None
            self.use_stream = use_stream
            self.status = "Fetching symbols..."
        threading.Thread(
            target=self._run_thread,
            args=(n_symbols, use_stream, kronos_gate, kronos_rank, volume_gate, stream_start),
            daemon=True,
        ).start()
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
                return "Stop the session before resetting."
            self.account = PaperAccount()
            self.account.save()
            self.status = "Account reset."
            self.error = None
        return None

    @staticmethod
    def _port_open(host: str, port: int) -> bool:
        async def _probe() -> bool:
            try:
                async with websockets.connect(f"ws://{host}:{port}", open_timeout=0.5):
                    return True
            except OSError:
                return False

        try:
            return asyncio.run(_probe())
        except OSError:
            return False

    @staticmethod
    def _kill_whatever_is_on(port: int) -> None:
        try:
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
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
        cmd = [sys.executable, "main.py", "--papertrade-stream"]
        if start_date:
            cmd.extend(["--papertrade-stream-start", start_date])
        self._stream_proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT))
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
        use_stream: bool,
        kronos_gate: bool,
        kronos_rank: bool,
        volume_gate: bool,
        stream_start: Optional[str],
    ) -> None:
        data_feed = None
        if use_stream:
            error = self._ensure_stream_server(start_date=stream_start)
            if error:
                with self.lock:
                    self.running = False
                    self.error = error
                    self.status = error
                return
            data_feed = StreamClient()

        symbol_rows = TVClient.fetch_top_symbols_with_exchanges_cached(
            n_symbols, settings.tv_screener,
        )
        if not symbol_rows:
            with self.lock:
                self.running = False
                self.error = "No symbols returned by screener."
                self.status = self.error
            return
        symbols = [s for s, _ex in symbol_rows]
        exchange_overrides = dict(symbol_rows)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self.lock:
            self.loop = loop
            self.account = PaperAccount.load()
            scanner = MarketScanner(
                symbols=symbols,
                exchange_overrides=exchange_overrides,
                paper_account=self.account,
                disabled_patterns=DISABLED_PATTERNS,
                data_feed=data_feed,
                scan_interval_seconds=(
                    settings.papertrade_stream_interval_seconds if use_stream else None
                ),
                kronos_gate=kronos_gate,
                kronos_rank=kronos_rank,
                volume_gate=volume_gate,
            )
            self.scanner = scanner
            self.task = loop.create_task(scanner.run())
            interval = (
                settings.papertrade_stream_interval_seconds
                if use_stream
                else settings.scan_interval_seconds
            )
            stream_note = f", stream from {stream_start}" if use_stream and stream_start else ""
            self.status = (
                f"Running — {len(symbols)} symbols, scanning every {interval}s"
                f"{stream_note}"
                f", Kronos gate={'ON' if kronos_gate else 'OFF'}"
                f", Kronos rank={'ON' if kronos_rank else 'OFF'}"
                f", Volume gate={'ON' if volume_gate else 'OFF'}"
            )

        error_msg: Optional[str] = None
        try:
            loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            root = exc
            while getattr(root, "exceptions", None):
                root = root.exceptions[0]
            error_msg = f"Crashed: {root}"
            log.error(f"Web Paper | scanner crashed: {root}", exc_info=root)
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


backtest_job = BacktestJob()
paper_session = PaperSession()
