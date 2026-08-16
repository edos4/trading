"""
ui/backtest_dialog.py — Backtest launcher dialog for the tkinter UI.

Provides a Toplevel dialog with parameter forms (with descriptions per
field) for every Backtester constructor argument, a "Run Backtest" button
that runs the backtest in a background thread, a live progress bar, and
a results panel showing the summary + trade table.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable, Optional

from config import settings, DISABLED_PATTERNS
from core.backtester import Backtester, BacktestResult, discover_pattern_names
from core.engine_defaults import ENGINE
from core.market import default_market, get_market
from data.tv_client import TVClient
from utils.logger import log


# ── Parameter definitions ──────────────────────────────────────────────
# Each entry: (key, label, description, type, default, choices_or_None)
#   type: "entry" (free text), "spin" (numeric spinbox), "combo" (dropdown), "check" (checkbox)
#   For "spin": default is packed as (default_value, min, max, increment).
#   For the "pattern_filter" combo, choices is None here and filled in
#   dynamically at dialog build time from discover_pattern_names().
# Defaults come from ENGINE so the UI form matches CLI backtest + paper.

def _decimals_for_increment(inc: float) -> int:
    """Decimal places to display for a spinbox increment (1 -> 0, 0.001 -> 3)."""
    s = f"{inc:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


PARAMS: list[tuple[str, str, str, str, Any, Optional[list[str]]]] = [
    (
        "market", "Market",
        "US = NASDAQ/NYSE, USD, shorts allowed. PH = PSE, PHP, long-only, "
        "Manila session, higher round-trip costs. Separate paper ledgers.",
        "combo", default_market().id, ["us", "ph"],
    ),
    (
        "n_symbols", "Symbols (count)",
        "Number of symbols to backtest (US: top by market cap; PH: top by peso volume).",
        "spin", (100, 5, 5000, 1), None,
    ),
    (
        "extra_symbols", "Additional symbols",
        "Optional tickers to include besides the screener top-N (comma or space). "
        "Duplicates already in the screener list are skipped.",
        "entry", "", None,
    ),
    (
        "pattern_filter", "Pattern filter",
        "Filter to one pattern. Leave blank for all patterns.",
        "combo", "", None,
    ),
    (
        "disabled_patterns", "Disabled patterns",
        "Comma-separated pattern names to exclude from the default multi-pattern run "
        "(ignored if Pattern filter above targets one of them explicitly).",
        "entry", ",".join(DISABLED_PATTERNS), None,
    ),
    (
        "min_confidence", "Min confidence",
        "Minimum pattern confidence to act on a signal (0.0-1.0). Higher = fewer but higher-quality trades.",
        "spin", (ENGINE.min_confidence, 0.0, 1.0, 0.01), None,
    ),
    (
        "regime_filter", "Regime filter (SMA200)",
        "Only buy above 200-day SMA, only sell below it (1.5% hysteresis band). "
        "Filters counter-trend trades; near-misses within the band still pass.",
        "check", ENGINE.regime_filter, None,
    ),
    (
        "kronos_gate", "Kronos 1w gate",
        "Require Kronos-base +1 week forecast to agree with the pattern's BUY/SELL "
        "and clear KRONOS_MIN_MOVE_PCT before taking the trade. Fail-closed by "
        "default if weights are missing (set KRONOS_GATE_FAIL_OPEN=true to pass "
        "instead). Does not rewrite TP/SL unless KRONOS_GATE_ADJUST_EXITS=true.",
        "check", settings.kronos_gate_enabled, None,
    ),
    (
        "kronos_rank", "Kronos rank sleeve",
        "Cross-sectional top-K by predicted 1w return (pattern_kronos_rank). "
        "Runs beside Toby patterns — not a gate. Uses KRONOS_RANK_TOP_K / LONG_ONLY. "
        "GPU-heavy; rebalances every KRONOS_RANK_REBALANCE_BARS.",
        "check", settings.kronos_rank_enabled, None,
    ),
    (
        "volume_gate", "Volume gate (RVOL+OBV)",
        "Require relative volume ≥ VOLUME_GATE_RVOL_MIN and OBV slope agreeing with "
        "BUY/SELL. Off by default — 2026-07-26 A/B showed no expectancy edge "
        "(ON→0 trades). Fail-open on short history.",
        "check", settings.volume_gate_enabled, None,
    ),
    (
        "cooldown_bars", "Cooldown (bars)",
        "Bars to wait before re-entering the same symbol+pattern after a loss. Reduces re-entering into chop.",
        "spin", (ENGINE.cooldown_bars, 0, 200, 1), None,
    ),
    (
        "txn_cost_pct", "Txn cost (%)",
        "Per-trade transaction cost as a fraction of price (0.001 = 0.1%). Applied on entry + exit.",
        "spin", (ENGINE.txn_cost_pct, 0.0, 0.01, 0.0001), None,
    ),
    (
        "position_sizing", "Position sizing",
        "Sizing method: 'risk' risks a fixed % of account per trade based on stop distance; "
        "'pattern' uses pattern's qty; 'notional' uses fixed notional; 'atr' sizes by ATR.",
        "combo", ENGINE.position_sizing, ["risk", "pattern", "notional", "atr"],
    ),
    (
        "account_value", "Account value ($)",
        "Starting capital for the backtest.",
        "spin", (ENGINE.account_value, 1000.0, 50_000_000.0, 1000.0), None,
    ),
    (
        "risk_per_trade_pct", "Risk per trade (%)",
        "Fraction of account risked per trade when position_sizing='risk' (0.0075 = 0.75%).",
        "spin", (ENGINE.risk_per_trade_pct, 0.0, 0.1, 0.0005), None,
    ),
    (
        "max_position_pct", "Max position (%)",
        "Diversification ceiling: largest fraction of account any single position may "
        "occupy, regardless of sizing mode. If tighter than what risk_per_trade_pct "
        "implies for a given stop, every trade gets capped to this. 0.10 with 0.75% "
        "risk keeps names from becoming 33% of the book against a 6% hard stop.",
        "spin", (ENGINE.max_position_pct, 0.01, 1.0, 0.01), None,
    ),
    (
        "max_gross_exposure_pct", "Max gross exposure (%)",
        "Cap on long+short notional as a fraction of equity (1.0 = 100%). "
        "Blocks stacking 33% names into 160% gross. 0 = unlimited.",
        "spin", (ENGINE.max_gross_exposure_pct, 0.0, 3.0, 0.05), None,
    ),
    (
        "trailing_activation_default", "Trailing activation (%)",
        "Cushion of unrealized profit before trailing stop arms (0.02 = 2%). "
        "Prevents entry-day chop from stopping trades early. Only applies to "
        "patterns that don't set their own trailing_activation_pct.",
        "spin", (ENGINE.trailing_activation_default, 0.0, 0.1, 0.001), None,
    ),
    (
        "min_hold_bars", "Min hold (bars)",
        "Mandatory holding period before trailing/breakeven stops can fire. "
        "Static stop-loss and take-profit still work immediately.",
        "spin", (ENGINE.min_hold_bars, 0, 50, 1), None,
    ),
    (
        "breakeven_trigger_pct", "Breakeven trigger (%)",
        "Once a trade is ahead by this much, its floor is raised to ~entry. "
        "Aligns with trailing activation so any trade that arms trailing also arms breakeven. "
        "0 = disabled.",
        "spin", (ENGINE.breakeven_trigger_pct or 0.0, 0.0, 0.2, 0.001), None,
    ),
    (
        "breakeven_buffer_pct", "Breakeven buffer (%)",
        "How far above entry (longs) / below entry (shorts) the breakeven floor sits. "
        "Ensures round-trip exits clear txn costs and land as small wins. (0.003 = 0.3%)",
        "spin", (ENGINE.breakeven_buffer_pct, 0.0, 0.05, 0.0005), None,
    ),
    (
        "min_atr_stop_multiple", "Min ATR stop multiple",
        "Requires trailing distance to be at least N× recent ATR before taking the trade. "
        "Screens out setups where the stop is ordinary daily noise. 0 = disabled.",
        "spin", (ENGINE.min_atr_stop_multiple, 0.0, 5.0, 0.1), None,
    ),
    (
        "synthetic_stop_multiple", "Synthetic stop multiple",
        "Catastrophic gap-protection stop = N × trailing_stop_pct. "
        "Higher = stop acts as disaster backstop, not routine exit. 0 = disabled.",
        "spin", (ENGINE.synthetic_stop_multiple, 0.0, 5.0, 0.05), None,
    ),
    (
        "atr_stop_floor_multiple", "ATR stop floor multiple",
        "Widens (never tightens) a pattern's own stop_loss up to N× recent ATR when "
        "the pattern's structural stop is tighter than that. 0 = disabled.",
        "spin", (ENGINE.atr_stop_floor_multiple, 0.0, 5.0, 0.1), None,
    ),
    (
        "hard_stop_percentage", "Hard stop (%)",
        "Hard absolute-loss cap from entry, applied only when the pattern's own stop "
        "is looser (or unset). Catastrophic-tail backstop — keep wider than the "
        "synthetic/ATR-floor stops it backstops or it becomes the everyday stop "
        "instead of a tail case. 0 = disabled.",
        "spin", (ENGINE.hard_stop_percentage, 0.0, 0.5, 0.005), None,
    ),
    (
        "min_reward_risk_ratio", "Min reward:risk ratio",
        "Skips signals whose take_profit/stop_loss ratio is below this. "
        "Screens out low-quality setups while keeping high-R:R winners. 0 = disabled.",
        "spin", (ENGINE.min_reward_risk_ratio, 0.0, 10.0, 0.1), None,
    ),
    (
        "max_open_positions", "Max open positions",
        "Maximum concurrent positions across all symbols. 0 = unlimited.",
        "spin", (settings.max_open_positions, 0, 50, 1), None,
    ),
    (
        "max_workers", "CPU workers",
        f"Detected {os.cpu_count() or '?'} CPU cores. "
        f"Suggested: {max(1, (os.cpu_count() or 2) - 1)} "
        f"(leaves 1 core free for the UI/system). "
        "0 = use all cores. Higher = faster backtest but heavier load.",
        "spin", (max(1, (os.cpu_count() or 2) - 1), 0, 64, 1), None,
    ),
]


class BacktestDialog:
    """Backtest launcher dialog with parameter forms, progress, and results."""

    def __init__(self, parent: tk.Misc):
        self._closed = False
        self._busy = False
        self._top = tk.Toplevel(parent)
        self._top.title("Backtest Runner")
        self._top.geometry("1200x600")
        self._top.minsize(640, 600)
        self._top.protocol("WM_DELETE_WINDOW", self._on_close)

        self._vars: dict[str, tk.Variable] = {}
        self._start_time: float | None = None
        self._timer_running = False
        self._completed = 0
        self._total = 0
        self._build_params()
        self._build_controls()
        self._build_results()

    # ── Parameter forms ──────────────────────────────────────────────────
    def _build_params(self) -> None:
        params_frame = ttk.LabelFrame(self._top, text="Backtest Parameters", padding=10)
        params_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(8, 4))

        for c in range(8):
            if c % 2 == 1:
                params_frame.columnconfigure(c, weight=1)
            else:
                params_frame.columnconfigure(c, weight=0, pad=8)

        def place_param(key, label, desc, ptype, default, choices, col, row):
            if key == "pattern_filter":
                choices = [""] + discover_pattern_names()
            ttk.Label(params_frame, text=label, font=("TkDefaultFont", 9, "bold")).grid(
                row=row, column=col, sticky=tk.W, padx=(0, 4),
            )
            ttk.Label(params_frame, text=desc, wraplength=180,
                      font=("TkDefaultFont", 8)).grid(
                row=row + 1, column=col, columnspan=2, sticky=tk.W, padx=(0, 4),
            )
            var = self._make_widget(params_frame, key, ptype, default, choices, col, row)
            return var

        row = 0
        for i in range(0, len(PARAMS), 4):
            for j in range(4):
                idx = i + j
                if idx >= len(PARAMS):
                    break
                place_param(*PARAMS[idx], col=j * 2, row=row)
            row += 2
        if "market" in self._vars:
            self._vars["market"].trace_add("write", lambda *_: self._apply_market_defaults())

    def _apply_market_defaults(self) -> None:
        profile = get_market(self._vars["market"].get())
        mapping = {
            "n_symbols": profile.default_n_symbols,
            "txn_cost_pct": profile.txn_cost_pct,
            "account_value": profile.paper_initial_capital,
            "kronos_gate": profile.kronos_gate_default,
            "kronos_rank": profile.kronos_rank_default,
        }
        for key, value in mapping.items():
            if key in self._vars:
                self._vars[key].set(value)

    def _make_widget(self, parent, key, ptype, default, choices, col, grid_row):
        var = None
        if ptype == "spin":
            default_val, minv, maxv, inc = default
            var = tk.DoubleVar(value=default_val)
            decimals = _decimals_for_increment(inc)
            sp = ttk.Spinbox(
                parent, from_=minv, to=maxv, increment=inc,
                textvariable=var, width=12,
                format=f"%.{decimals}f",
            )
            sp.grid(row=grid_row, column=col + 1, sticky=tk.W, padx=(0, 8))
        elif ptype == "combo":
            var = tk.StringVar(value=default)
            ttk.Combobox(
                parent, textvariable=var, values=choices or [],
                state="readonly", width=18,
            ).grid(row=grid_row, column=col + 1, sticky=tk.W, padx=(0, 8))
        elif ptype == "check":
            var = tk.BooleanVar(value=default)
            ttk.Checkbutton(parent, variable=var).grid(
                row=grid_row, column=col + 1, sticky=tk.W, padx=(0, 8),
            )
        else:
            var = tk.StringVar(value=str(default))
            width = 28 if key == "extra_symbols" else 18
            ttk.Entry(parent, textvariable=var, width=width).grid(
                row=grid_row, column=col + 1, sticky=tk.W, padx=(0, 8),
            )
        self._vars[key] = var
        return var

    # ── Run button + progress ──────────────────────────────────────────
    def _build_controls(self) -> None:
        btn_frame = ttk.Frame(self._top)
        btn_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(4, 4))
        self._run_btn = ttk.Button(btn_frame, text="Run Backtest", command=self._run_backtest)
        self._run_btn.pack(side=tk.LEFT)

        self._ab_btn = ttk.Button(
            btn_frame, text="Compare A/B (Volume)", command=self._run_volume_ab,
        )
        self._ab_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._progress = ttk.Progressbar(btn_frame, mode="determinate", length=280)
        self._progress.pack(side=tk.LEFT, padx=(8, 4))

        self._pct_var = tk.StringVar(value="\u2014")
        ttk.Label(btn_frame, textvariable=self._pct_var, width=5, anchor=tk.CENTER).pack(side=tk.LEFT)

        self._elapsed_var = tk.StringVar(value="Elapsed: \u2014")
        ttk.Label(btn_frame, textvariable=self._elapsed_var).pack(side=tk.LEFT, padx=(4, 0))

        self._eta_var = tk.StringVar(value="ETA: \u2014")
        ttk.Label(btn_frame, textvariable=self._eta_var).pack(side=tk.LEFT, padx=(8, 0))

        self._status_var = tk.StringVar(value="Adjust parameters and click Run Backtest.")
        ttk.Label(btn_frame, textvariable=self._status_var).pack(side=tk.LEFT, padx=(8, 0))

    # ── Results panel ────────────────────────────────────────────────────
    def _build_results(self) -> None:
        results_frame = ttk.LabelFrame(self._top, text="Results", padding=10)
        results_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        self._summary_text = tk.Text(results_frame, height=12, wrap=tk.WORD, state=tk.DISABLED)
        self._summary_text.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))

        # Trade table
        ttk.Label(results_frame, text="Trades", font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W)
        cols = ("date", "action", "symbol", "tf", "entry", "exit", "pnl_pct", "reason", "pattern")
        self._tree = ttk.Treeview(results_frame, columns=cols, show="headings", height=10)
        for c, w in zip(cols, (95, 55, 65, 40, 75, 75, 65, 90, 160)):
            self._tree.heading(c, text=c.capitalize())
            self._tree.column(c, width=w, anchor=tk.W)
        tree_scroll = ttk.Scrollbar(results_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.config(yscrollcommand=tree_scroll.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.LEFT, fill=tk.Y)

        # Save button
        self._save_btn = ttk.Button(self._top, text="Save Results...", command=self._save_results, state=tk.DISABLED)
        self._save_btn.pack(side=tk.BOTTOM, padx=8, pady=(0, 8))
        self._last_result: Optional[BacktestResult] = None

    # ── Collect params from form ─────────────────────────────────────────
    def _collect_params(self) -> dict:
        p: dict[str, Any] = {}
        for key, label, desc, ptype, default, choices in PARAMS:
            var = self._vars[key]
            if ptype == "check":
                p[key] = bool(var.get())
            elif ptype == "spin":
                default_val, minv, maxv, inc = default
                val_str = str(var.get())
                try:
                    val = float(val_str)
                except (ValueError, tk.TclError):
                    val = default_val
                p[key] = val
            elif ptype == "combo":
                v = var.get()
                p[key] = v if v else None
            else:
                v = var.get().strip()
                p[key] = v if v else None
        # n_symbols / extra_symbols / market are not Backtester params
        n_symbols = int(p.pop("n_symbols"))
        extra_symbols = p.pop("extra_symbols", None) or ""
        market = p.pop("market", None) or default_market().id
        # Convert spinbox floats to ints where Backtester expects int
        for int_key in ("max_workers", "max_open_positions"):
            if int_key in p and p[int_key] is not None:
                p[int_key] = int(p[int_key])
        # pattern_filter maps to pattern arg, not constructor kwarg
        pattern_filter = p.pop("pattern_filter")
        # disabled_patterns is a comma-separated string in the form -> list
        disabled_raw = p.pop("disabled_patterns", None) or ""
        p["disabled_patterns"] = [
            name.strip() for name in disabled_raw.split(",") if name.strip()
        ]
        # Convert "disable" sentinels: spin values of 0 where None means disabled
        for opt_key in (
            "breakeven_trigger_pct", "min_atr_stop_multiple",
            "min_reward_risk_ratio", "hard_stop_percentage", "atr_stop_floor_multiple",
        ):
            if opt_key in p and p[opt_key] is not None and p[opt_key] <= 0:
                p[opt_key] = None
        if "synthetic_stop_multiple" in p and p["synthetic_stop_multiple"] <= 0:
            p["synthetic_stop_multiple"] = 0
        p["market"] = market
        p["long_only"] = get_market(market).long_only
        return {
            "n_symbols": n_symbols,
            "extra_symbols": extra_symbols,
            "pattern": pattern_filter,
            "kwargs": p,
            "market": market,
        }

    # ── Run backtest in background thread ─────────────────────────────────
    def _run_backtest(self) -> None:
        if self._busy:
            return
        params = self._collect_params()
        n_symbols = params["n_symbols"]
        extra_symbols = params.get("extra_symbols") or ""
        pattern = params["pattern"]
        kwargs = params["kwargs"]
        self._busy = True
        self._run_btn.config(state=tk.DISABLED)
        self._ab_btn.config(state=tk.DISABLED)
        self._progress["value"] = 0
        self._pct_var.set("0%")
        self._elapsed_var.set("Elapsed: 0s")
        self._eta_var.set("ETA: \u2014")
        self._status_var.set(f"Running backtest (top {n_symbols} symbols)...")
        self._summary_text.config(state=tk.NORMAL)
        self._summary_text.delete("1.0", tk.END)
        self._summary_text.insert(tk.END, "Running...\n")
        self._summary_text.config(state=tk.DISABLED)
        self._tree.delete(*self._tree.get_children())
        threading.Thread(
            target=self._run_backtest_thread,
            args=(n_symbols, extra_symbols, pattern, kwargs),
            daemon=True,
        ).start()

    def _run_volume_ab(self) -> None:
        """Run the same backtest twice — volume gate OFF then ON — and show deltas."""
        if self._busy:
            return
        params = self._collect_params()
        n_symbols = params["n_symbols"]
        extra_symbols = params.get("extra_symbols") or ""
        pattern = params["pattern"]
        kwargs = dict(params["kwargs"])
        # A/B forces both sides; ignore the form checkbox for the pair of runs.
        kwargs.pop("volume_gate", None)
        self._busy = True
        self._run_btn.config(state=tk.DISABLED)
        self._ab_btn.config(state=tk.DISABLED)
        self._progress["value"] = 0
        self._pct_var.set("0%")
        self._elapsed_var.set("Elapsed: 0s")
        self._eta_var.set("ETA: \u2014")
        self._status_var.set(f"Volume A/B compare (top {n_symbols} symbols)...")
        self._summary_text.config(state=tk.NORMAL)
        self._summary_text.delete("1.0", tk.END)
        self._summary_text.insert(tk.END, "Running volume gate OFF then ON...\n")
        self._summary_text.config(state=tk.DISABLED)
        self._tree.delete(*self._tree.get_children())
        threading.Thread(
            target=self._run_volume_ab_thread,
            args=(n_symbols, extra_symbols, pattern, kwargs),
            daemon=True,
        ).start()

    def _run_backtest_thread(
        self, n_symbols: int, extra_symbols: str, pattern: Optional[str], kwargs: dict,
    ) -> None:
        try:
            symbol_rows = TVClient.fetch_universe_cached(
                n_symbols, kwargs.get("market"), extra_symbols=extra_symbols,
            )
            if not symbol_rows:
                self._top.after(0, lambda: self._finish(None, "No symbols from screener or additional list."))
                return
            symbols = [s for s, _ex in symbol_rows]
            backtester = Backtester(symbols, pattern_filter=pattern, progress_callback=self._on_progress, **kwargs)
            result = asyncio.run(backtester.run())
            self._top.after(0, lambda: self._finish(result, None))
        except Exception as exc:
            err_msg = f"Backtest failed: {exc}"
            log.error(f"UI Backtest | {err_msg}")
            self._top.after(0, lambda: self._finish(None, err_msg))

    def _run_volume_ab_thread(
        self, n_symbols: int, extra_symbols: str, pattern: Optional[str], kwargs: dict,
    ) -> None:
        try:
            from analysis.price_volume import ab_metrics_from_result

            symbol_rows = TVClient.fetch_universe_cached(
                n_symbols, kwargs.get("market"), extra_symbols=extra_symbols,
            )
            if not symbol_rows:
                self._top.after(0, lambda: self._finish(None, "No symbols from screener or additional list."))
                return
            symbols = [s for s, _ex in symbol_rows]

            def progress(completed: int, total: int) -> None:
                # Two full passes — map into a 0–100 overall bar.
                # First pass reported as 0–50, second as 50–100 via phase flag.
                self._on_progress(completed, total)

            off_bt = Backtester(
                symbols, pattern_filter=pattern, volume_gate=False,
                progress_callback=progress, **kwargs,
            )
            result_off = asyncio.run(off_bt.run())
            on_bt = Backtester(
                symbols, pattern_filter=pattern, volume_gate=True,
                progress_callback=progress, **kwargs,
            )
            result_on = asyncio.run(on_bt.run())
            off_m = ab_metrics_from_result(result_off)
            on_m = ab_metrics_from_result(result_on)
            self._top.after(
                0,
                lambda: self._finish_ab(result_off, result_on, off_m, on_m, None),
            )
        except Exception as exc:
            err_msg = f"Volume A/B failed: {exc}"
            log.error(f"UI Backtest | {err_msg}")
            self._top.after(0, lambda: self._finish(None, err_msg))

    def _on_progress(self, completed: int, total: int) -> None:
        if self._closed or not self._busy:
            return
        self._completed = completed
        self._total = total
        self._start_timer()
        pct = (completed / total) * 100 if total > 0 else 0
        self._top.after(0, lambda: self._apply_progress(pct))

    def _apply_progress(self, pct: float) -> None:
        if self._closed:
            return
        self._progress["value"] = pct
        self._pct_var.set(f"{pct:.0f}%")

    def _start_timer(self) -> None:
        if self._start_time is None:
            self._start_time = __import__("time").time()
        if not self._timer_running:
            self._timer_running = True
            self._tick_timer()

    def _tick_timer(self) -> None:
        if self._closed or self._start_time is None:
            return
        if not self._busy:
            return
        elapsed = __import__("time").time() - self._start_time
        self._elapsed_var.set(f"Elapsed: {elapsed:.0f}s")
        if self._completed > 0 and self._total > 0:
            rate = self._completed / elapsed if elapsed > 0 else 0
            remaining = self._total - self._completed
            eta_s = remaining / rate if rate > 0 else 0
            label = f"ETA: {eta_s:.0f}s" if eta_s < 3600 else f"ETA: {eta_s / 60:.1f}m"
            self._eta_var.set(label)
        self._top.after(1000, self._tick_timer)

    def _finish(self, result: Optional[BacktestResult], error: Optional[str]) -> None:
        self._timer_running = False
        self._busy = False
        self._run_btn.config(state=tk.NORMAL)
        self._ab_btn.config(state=tk.NORMAL)
        if error:
            self._status_var.set(error)
            self._summary_text.config(state=tk.NORMAL)
            self._summary_text.delete("1.0", tk.END)
            self._summary_text.insert(tk.END, f"ERROR: {error}\n")
            self._summary_text.config(state=tk.DISABLED)
            return
        if result is None:
            self._status_var.set("No result.")
            return
        self._last_result = result
        self._save_btn.config(state=tk.NORMAL)
        self._progress["value"] = 100
        self._pct_var.set("100%")
        # Summary
        self._summary_text.config(state=tk.NORMAL)
        self._summary_text.delete("1.0", tk.END)
        self._summary_text.insert(tk.END, result.summary())
        self._summary_text.config(state=tk.DISABLED)
        # Trade table
        self._tree.delete(*self._tree.get_children())
        for t in sorted(result.trades, key=lambda t: t.entry_date):
            self._tree.insert(
                "", tk.END,
                values=(
                    t.entry_date.strftime("%Y-%m-%d"),
                    t.action,
                    t.symbol,
                    t.timeframe,
                    f"{t.entry_price:.2f}",
                    f"{t.exit_price:.2f}",
                    f"{t.pnl_pct:+.2f}%",
                    t.exit_reason,
                    t.pattern,
                ),
            )
        self._status_var.set(
            f"Done: {result.win_rate:.1%} win rate ({result.win_count}W / {result.loss_count}L / {len(result.trades)} total)"
        )

    def _finish_ab(
        self,
        result_off: BacktestResult,
        result_on: BacktestResult,
        off_m: dict,
        on_m: dict,
        error: Optional[str],
    ) -> None:
        self._timer_running = False
        self._busy = False
        self._run_btn.config(state=tk.NORMAL)
        self._ab_btn.config(state=tk.NORMAL)
        if error:
            self._finish(None, error)
            return
        self._last_result = result_on  # save ON side by default
        self._save_btn.config(state=tk.NORMAL)
        self._progress["value"] = 100
        self._pct_var.set("100%")

        keys = [
            "trades", "win_rate", "avg_r", "expectancy_pct",
            "profit_factor", "max_drawdown_pct", "account_weighted_pnl_pct",
            "total_signals",
        ]

        def _fmt(v: Any) -> str:
            if v is None:
                return "—"
            if isinstance(v, float):
                return f"{v:+.4f}" if abs(v) < 10 else f"{v:.4f}"
            return str(v)

        lines = [
            "=" * 60,
            "  VOLUME GATE A/B COMPARE",
            "=" * 60,
            f"  {'metric':28s}  {'OFF':>12s}  {'ON':>12s}  {'delta':>12s}",
            "-" * 60,
        ]
        for k in keys:
            a, b = off_m[k], on_m[k]
            if a is None or b is None:
                delta = "—"
            elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
                delta = _fmt(b - a)
            else:
                delta = "—"
            lines.append(f"  {k:28s}  {_fmt(a):>12s}  {_fmt(b):>12s}  {delta:>12s}")
        lines.append("=" * 60)
        lines.append("")
        lines.append("--- Gate OFF ---")
        lines.append(result_off.summary())
        lines.append("")
        lines.append("--- Gate ON ---")
        lines.append(result_on.summary())

        self._summary_text.config(state=tk.NORMAL)
        self._summary_text.delete("1.0", tk.END)
        self._summary_text.insert(tk.END, "\n".join(lines))
        self._summary_text.config(state=tk.DISABLED)

        # Show ON trades in the table
        self._tree.delete(*self._tree.get_children())
        for t in sorted(result_on.trades, key=lambda t: t.entry_date):
            self._tree.insert(
                "", tk.END,
                values=(
                    t.entry_date.strftime("%Y-%m-%d"),
                    t.action,
                    t.symbol,
                    t.timeframe,
                    f"{t.entry_price:.2f}",
                    f"{t.exit_price:.2f}",
                    f"{t.pnl_pct:+.2f}%",
                    t.exit_reason,
                    t.pattern,
                ),
            )
        self._status_var.set(
            f"A/B done: OFF n={off_m['trades']} exp={off_m['expectancy_pct']} | "
            f"ON n={on_m['trades']} exp={on_m['expectancy_pct']}"
        )

    # ── Save results ─────────────────────────────────────────────────────
    def _save_results(self) -> None:
        if self._last_result is None:
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile=f"backtest_results_{ts}.json",
            filetypes=[("JSON", "*.json"), ("Text", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            p = Path(path)
            if p.suffix.lower() == ".json":
                p.write_text(
                    json.dumps(self._last_result.to_dict(), indent=2),
                    encoding="utf-8",
                )
            else:
                self._last_result.save(str(p))
        except Exception as exc:
            from tkinter import messagebox
            messagebox.showerror("Save failed", str(exc))
            return
        self._status_var.set(f"Saved -> {path}")

    # ── Lifecycle ────────────────────────────────────────────────────────
    def _on_close(self) -> None:
        if self._busy:
            from tkinter import messagebox
            if not messagebox.askyesno(
                "Backtest running",
                "A backtest is still running. Close anyway?",
            ):
                return
        self._closed = True
        self._top.destroy()
