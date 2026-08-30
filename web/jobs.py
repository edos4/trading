"""Background job managers for web backtest + paper trading."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Optional

from config import DISABLED_PATTERNS
from core.backtester import Backtester, BacktestResult, discover_pattern_names
from core.market import default_market, get_market
from core.paper_books import paper_books
from data.tv_client import TVClient
from ui.backtest_dialog import PARAMS
from utils.logger import log


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
    extra_symbols = p.pop("extra_symbols", None) or ""
    market = p.pop("market", None) or default_market().id
    if "max_workers" in p and p["max_workers"] is not None:
        p["max_workers"] = int(p["max_workers"])
    if "max_open_positions" in p and p["max_open_positions"] is not None:
        p["max_open_positions"] = int(p["max_open_positions"])
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
        "profit_take_pct",
    ):
        if opt_key in p and p[opt_key] is not None and p[opt_key] <= 0:
            p[opt_key] = None
    if "synthetic_stop_multiple" in p and p["synthetic_stop_multiple"] <= 0:
        p["synthetic_stop_multiple"] = 0
    p["market"] = market
    p["long_only"] = get_market(market).long_only
    if not p.get("kronos_gate") and not p.get("kronos_rank"):
        p["kronos_batch"] = False
    return {
        "n_symbols": n_symbols,
        "extra_symbols": extra_symbols,
        "pattern": pattern_filter,
        "kwargs": p,
        "market": market,
    }


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

    def start(
        self, n_symbols: int, pattern: Optional[str], kwargs: dict, *, ab: bool,
        extra_symbols: str = "",
    ) -> str | None:
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
            args=(n_symbols, extra_symbols, pattern, kwargs),
            daemon=True,
        ).start()
        return None

    def _run(self, n_symbols: int, extra_symbols: str, pattern: Optional[str], kwargs: dict) -> None:
        try:
            symbol_rows = TVClient.fetch_universe_cached(
                n_symbols, kwargs.get("market"), extra_symbols=extra_symbols,
            )
            if not symbol_rows:
                with self.lock:
                    self.error = "No symbols from screener or additional list."
                    self.status = self.error
                return
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
        except Exception:
            log.exception("Web Backtest | failed")
            with self.lock:
                self.error = "Backtest failed. Check server logs for details."
                self.status = "ERROR: backtest failed"
        finally:
            with self.lock:
                self.busy = False

    def _run_ab(self, n_symbols: int, extra_symbols: str, pattern: Optional[str], kwargs: dict) -> None:
        try:
            from analysis.price_volume import ab_metrics_from_result

            kwargs = dict(kwargs)
            kwargs.pop("volume_gate", None)
            symbol_rows = TVClient.fetch_universe_cached(
                n_symbols, kwargs.get("market"), extra_symbols=extra_symbols,
            )
            if not symbol_rows:
                with self.lock:
                    self.error = "No symbols from screener or additional list."
                    self.status = self.error
                return
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
        except Exception:
            log.exception("Web Backtest A/B | failed")
            with self.lock:
                self.error = "Backtest A/B failed. Check server logs for details."
                self.status = "ERROR: backtest A/B failed"
        finally:
            with self.lock:
                self.busy = False



backtest_job = BacktestJob()
