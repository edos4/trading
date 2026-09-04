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
        "US = NASDAQ/NYSE, USD, shorts allowed. PH = PSE, PHP, long-only.",
        "combo", default_market().id, ["us", "ph"],
    ),
    (
        "pattern_filter", "Pattern filter",
        "Filter to one pattern (case-insensitive substring). Blank = all patterns.",
        "combo", "", None,
    ),
    (
        "universe", "Universe",
        "Ticker list under data/universes/. Blank = the pattern's own .cjs "
        "universe when a Pattern filter is set, else 'default'.",
        "entry", "", None,
    ),
    (
        "barcache_dir", "Barcache dir",
        "Offline daily-bar cache (build with scripts/build_barcache.py).",
        "entry", "data/barcache", None,
    ),
    (
        "extra_symbols", "Additional symbols",
        "Optional extra tickers (comma or space separated).",
        "entry", "", None,
    ),
    (
        "txn_cost_pct", "Txn cost (per leg)",
        "0.0 = documented headline numbers; 0.001 matches the .cjs 'cost optional' mode.",
        "spin", (0.0, 0.0, 0.01, 0.0001), None,
    ),
    (
        "max_workers", "CPU workers",
        f"Detected {os.cpu_count() or '?'} cores. 0 = use all.",
        "spin", (max(1, (os.cpu_count() or 2) - 1), 0, 64, 1), None,
    ),
]

def _universe_for_pattern(pattern: Optional[str]) -> str:
    if not pattern:
        return "default"
    for key, uni in {
        "double_top": "double_top", "upward_channel": "upward_channel",
        "descending_channel": "upward_channel",
        "head_and_shoulders": "head_and_shoulders",
        "rounding_bottom": "rounding_bottom", "rounding_top": "rounding_bottom",
        "flag": "flag", "pennant": "pennant",
    }.items():
        if key in pattern.lower():
            return uni
    return "default"


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
        self._structure_widgets: dict[str, tk.Widget] = {}
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
        for key in ("kronos_gate", "kronos_rank"):
            if key in self._vars:
                self._vars[key].trace_add("write", lambda *_: self._sync_batch_kronos())
        self._sync_batch_kronos()
        if "pattern_only" in self._vars:
            self._vars["pattern_only"].trace_add(
                "write", lambda *_: self._sync_pattern_only(),
            )
        self._sync_pattern_only()

    def _sync_batch_kronos(self) -> None:
        gate = bool(self._vars.get("kronos_gate") and self._vars["kronos_gate"].get())
        rank = bool(self._vars.get("kronos_rank") and self._vars["kronos_rank"].get())
        widget = getattr(self, "_batch_kronos_widget", None)
        var = self._vars.get("kronos_batch")
        if widget is None or var is None:
            return
        if gate or rank:
            widget.configure(state=tk.NORMAL)
        else:
            var.set(False)
            widget.configure(state=tk.DISABLED)

    def _sync_pattern_only(self) -> None:
        on = bool(self._vars.get("pattern_only") and self._vars["pattern_only"].get())
        state = tk.DISABLED if on else tk.NORMAL
        for widget in self._structure_widgets.values():
            widget.configure(state=state)

    def _apply_market_defaults(self) -> None:
        profile = get_market(self._vars["market"].get())
        mapping = {
            "n_symbols": profile.default_n_symbols,
            "txn_cost_pct": profile.txn_cost_pct,
            "account_value": profile.paper_initial_capital,
            "kronos_gate": profile.kronos_gate_default,
            "kronos_rank": profile.kronos_rank_default,
            "breakeven_trigger_pct": profile.breakeven_trigger_pct or 0.0,
            "breakeven_buffer_pct": profile.breakeven_buffer_pct,
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
            if key in ("min_confidence", "cooldown_bars"):
                self._structure_widgets[key] = sp
        elif ptype == "combo":
            var = tk.StringVar(value=default)
            ttk.Combobox(
                parent, textvariable=var, values=choices or [],
                state="readonly", width=18,
            ).grid(row=grid_row, column=col + 1, sticky=tk.W, padx=(0, 8))
        elif ptype == "check":
            var = tk.BooleanVar(value=default)
            cb = ttk.Checkbutton(parent, variable=var)
            cb.grid(row=grid_row, column=col + 1, sticky=tk.W, padx=(0, 8))
            if key == "kronos_batch":
                self._batch_kronos_widget = cb
            if key == "regime_filter":
                self._structure_widgets[key] = cb
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
        extra_symbols = p.pop("extra_symbols", None) or ""
        market = p.pop("market", None) or default_market().id
        pattern_filter = p.pop("pattern_filter")
        universe = p.pop("universe", None)
        kwargs = {
            "barcache_dir": p.get("barcache_dir") or "data/barcache",
            "market": market,
            "txn_cost_pct": float(p.get("txn_cost_pct") or 0.0),
            "max_workers": int(p.get("max_workers") or 0),
            "disabled_patterns": list(DISABLED_PATTERNS),
        }
        return {
            "extra_symbols": extra_symbols,
            "pattern": pattern_filter,
            "universe": universe,
            "kwargs": kwargs,
            "market": market,
        }

    # ── Run backtest in background thread ─────────────────────────────────
    def _run_backtest(self) -> None:
        if self._busy:
            return
        params = self._collect_params()
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
        self._status_var.set("Running backtest...")
        self._summary_text.config(state=tk.NORMAL)
        self._summary_text.delete("1.0", tk.END)
        self._summary_text.insert(tk.END, "Running...\n")
        self._summary_text.config(state=tk.DISABLED)
        self._tree.delete(*self._tree.get_children())
        threading.Thread(
            target=self._run_backtest_thread,
            args=(params.get("universe"), extra_symbols, pattern, kwargs),
            daemon=True,
        ).start()

    _run_volume_ab = _run_backtest  # A/B volume gate retired

    def _run_backtest_thread(
        self, universe: Optional[str], extra_symbols: str,
        pattern: Optional[str], kwargs: dict,
    ) -> None:
        try:
            from data.universes import load as _load_universe
            name = universe or _universe_for_pattern(pattern)
            symbols = list(_load_universe(name))
            for extra in extra_symbols.replace(",", " ").split():
                u = extra.strip().upper()
                if u and u not in symbols:
                    symbols.append(u)
            backtester = Backtester(symbols, pattern_filter=pattern, progress_callback=self._on_progress, **kwargs)
            result = asyncio.run(backtester.run())
            self._top.after(0, lambda: self._finish(result, None))
        except Exception as exc:
            err_msg = f"Backtest failed: {exc}"
            log.error(f"UI Backtest | {err_msg}")
            self._top.after(0, lambda: self._finish(None, err_msg))

    def _run_volume_ab_thread(self, *a, **k) -> None:  # retired
        return

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
