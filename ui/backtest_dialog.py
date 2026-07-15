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

from config import settings
from core.backtester import Backtester, BacktestResult, discover_pattern_names
from data.tv_client import TVClient
from utils.logger import log


# ── Parameter definitions ──────────────────────────────────────────────
# Each entry: (key, label, description, type, default, choices_or_None)
#   type: "entry" (free text), "spin" (numeric spinbox), "combo" (dropdown), "check" (checkbox)
#   For "spin": default is packed as (default_value, min, max, increment).
#   For the "pattern_filter" combo, choices is None here and filled in
#   dynamically at dialog build time from discover_pattern_names().

def _decimals_for_increment(inc: float) -> int:
    """Decimal places to display for a spinbox increment (1 -> 0, 0.001 -> 3)."""
    s = f"{inc:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


PARAMS: list[tuple[str, str, str, str, Any, Optional[list[str]]]] = [
    (
        "n_symbols", "Symbols (count)",
        "Number of top-market-cap symbols to backtest (fetched from TradingView screener).",
        "spin", (100, 5, 200, 1), None,
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
        "entry", "pattern_009_flag_pattern,pattern_006_upward_channel", None,
    ),
    (
        "min_confidence", "Min confidence",
        "Minimum pattern confidence to act on a signal (0.0-1.0). Higher = fewer but higher-quality trades.",
        "spin", (0.6, 0.0, 1.0, 0.01), None,
    ),
    (
        "regime_filter", "Regime filter (SMA200)",
        "Only buy above 200-day SMA, only sell below it. Filters counter-trend trades.",
        "check", True, None,
    ),
    (
        "cooldown_bars", "Cooldown (bars)",
        "Bars to wait before re-entering the same symbol+pattern after a loss. Reduces re-entering into chop.",
        "spin", (10, 0, 200, 1), None,
    ),
    (
        "txn_cost_pct", "Txn cost (%)",
        "Per-trade transaction cost as a fraction of price (0.001 = 0.1%). Applied on entry + exit.",
        "spin", (0.001, 0.0, 0.01, 0.0001), None,
    ),
    (
        "position_sizing", "Position sizing",
        "Sizing method: 'risk' risks a fixed % of account per trade based on stop distance; "
        "'pattern' uses pattern's qty; 'notional' uses fixed notional; 'atr' sizes by ATR.",
        "combo", "risk", ["risk", "pattern", "notional", "atr"],
    ),
    (
        "account_value", "Account value ($)",
        "Starting capital for the backtest.",
        "spin", (100000.0, 1000.0, 1000000.0, 1000.0), None,
    ),
    (
        "risk_per_trade_pct", "Risk per trade (%)",
        "Fraction of account risked per trade when position_sizing='risk' (0.02 = 2%).",
        "spin", (0.02, 0.0, 0.1, 0.001), None,
    ),
    (
        "max_position_pct", "Max position (%)",
        "Diversification ceiling: largest fraction of account any single position may "
        "occupy, regardless of sizing mode. If tighter than what risk_per_trade_pct "
        "implies for a given stop, every trade gets capped to this and "
        "risk_per_trade_pct stops mattering.",
        "spin", (0.10, 0.01, 1.0, 0.01), None,
    ),
    (
        "trailing_activation_default", "Trailing activation (%)",
        "Cushion of unrealized profit before trailing stop arms (0.01 = 1%). "
        "Prevents entry-day chop from stopping trades early.",
        "spin", (0.01, 0.0, 0.1, 0.001), None,
    ),
    (
        "min_hold_bars", "Min hold (bars)",
        "Mandatory holding period before trailing/breakeven stops can fire. "
        "Static stop-loss and take-profit still work immediately.",
        "spin", (2, 0, 50, 1), None,
    ),
    (
        "breakeven_trigger_pct", "Breakeven trigger (%)",
        "Once a trade is ahead by this much, its floor is raised to ~entry. "
        "Aligns with trailing activation so any trade that arms trailing also arms breakeven. "
        "0 = disabled.",
        "spin", (0.0, 0.0, 0.2, 0.001), None,
    ),
    (
        "breakeven_buffer_pct", "Breakeven buffer (%)",
        "How far above entry (longs) / below entry (shorts) the breakeven floor sits. "
        "Ensures round-trip exits clear txn costs and land as small wins. (0.003 = 0.3%)",
        "spin", (0.003, 0.0, 0.05, 0.0005), None,
    ),
    (
        "min_atr_stop_multiple", "Min ATR stop multiple",
        "Requires trailing distance to be at least N× recent ATR before taking the trade. "
        "Screens out setups where the stop is ordinary daily noise. 0 = disabled.",
        "spin", (1.0, 0.0, 5.0, 0.1), None,
    ),
    (
        "synthetic_stop_multiple", "Synthetic stop multiple",
        "Catastrophic gap-protection stop = N × trailing_stop_pct. "
        "Higher = stop acts as disaster backstop, not routine exit. 0 = disabled.",
        "spin", (2.0, 0.0, 5.0, 0.05), None,
    ),
    (
        "atr_stop_floor_multiple", "ATR stop floor multiple",
        "Widens (never tightens) a pattern's own stop_loss up to N× recent ATR when "
        "the pattern's structural stop is tighter than that. 0 = disabled.",
        "spin", (1.2, 0.0, 5.0, 0.1), None,
    ),
    (
        "hard_stop_percentage", "Hard stop (%)",
        "Hard absolute-loss cap from entry, applied only when the pattern's own stop "
        "is looser (or unset). Catastrophic-tail backstop. 0 = disabled.",
        "spin", (0.03, 0.0, 0.5, 0.005), None,
    ),
    (
        "min_reward_risk_ratio", "Min reward:risk ratio",
        "Skips signals whose take_profit/stop_loss ratio is below this. "
        "Screens out low-quality setups while keeping high-R:R winners. 0 = disabled.",
        "spin", (1.5, 0.0, 10.0, 0.1), None,
    ),
    (
        "max_open_positions", "Max open positions",
        "Maximum concurrent positions across all symbols.",
        "spin", (settings.max_open_positions, 1, 50, 1), None,
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
            ttk.Entry(parent, textvariable=var, width=18).grid(
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
        # n_symbols is not a Backtester param — extract it
        n_symbols = int(p.pop("n_symbols"))
        # Convert spinbox floats to ints where Backtester expects int
        for int_key in ("max_workers",):
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
        return {"n_symbols": n_symbols, "pattern": pattern_filter, "kwargs": p}

    # ── Run backtest in background thread ─────────────────────────────────
    def _run_backtest(self) -> None:
        if self._busy:
            return
        params = self._collect_params()
        n_symbols = params["n_symbols"]
        pattern = params["pattern"]
        kwargs = params["kwargs"]
        self._busy = True
        self._run_btn.config(state=tk.DISABLED)
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
            args=(n_symbols, pattern, kwargs),
            daemon=True,
        ).start()

    def _run_backtest_thread(self, n_symbols: int, pattern: Optional[str], kwargs: dict) -> None:
        try:
            symbol_rows = TVClient.fetch_top_symbols_with_exchanges_cached(
                n_symbols, settings.tv_screener,
            )
            if not symbol_rows:
                self._top.after(0, lambda: self._finish(None, "No symbols returned by screener."))
                return
            symbols = [s for s, _ex in symbol_rows]
            backtester = Backtester(symbols, pattern_filter=pattern, progress_callback=self._on_progress, **kwargs)
            result = asyncio.run(backtester.run())
            self._top.after(0, lambda: self._finish(result, None))
        except Exception as exc:
            err_msg = f"Backtest failed: {exc}"
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
