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
from core.backtest_params import PARAMS, _universe_for_pattern  # noqa: F401 - shared with headless web
from core.backtester import Backtester, BacktestResult, discover_pattern_names
from core.engine_defaults import ENGINE
from core.market import default_market, get_market
from data.tv_client import TVClient
from utils.logger import log


# ── Parameter definitions ──────────────────────────────────────────────
# Shared, UI-independent definitions live in core.backtest_params so headless
# web startup never imports Tkinter. Re-exported here for existing callers.

def _decimals_for_increment(inc: float) -> int:
    """Decimal places to display for a spinbox increment (1 -> 0, 0.001 -> 3)."""
    s = f"{inc:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


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
        self._stream_entries: list = []
        self._stream_running = False
        self._stream_run_id: Optional[str] = None
        self._stream_service = None
        self._stream_catalog: dict[str, str] = {}
        self._stream_presets: dict[str, str] = {}
        self._build_params()
        self._build_stream_panel()
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

    # ── Pinned-version / historical-stream panel ─────────────────────────
    STREAM_SKIP = ("mode", "timeframe")

    def _build_stream_panel(self) -> None:
        """Shared-service controls. Same request contract the web uses."""
        from core.backtest_params import REPLAY_PARAMS

        frame = ttk.LabelFrame(
            self._top, text="Pinned versions / historical stream", padding=10,
        )
        frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(4, 4))
        for c in range(10):
            frame.columnconfigure(c, weight=0, pad=8)

        entries = [
            ("mode", "Mode",
             "offline replays the full tape; historical-stream replays session-by-session like paper.",
             "combo", "offline", ["offline", "historical-stream"]),
        ] + [e for e in REPLAY_PARAMS if e[0] not in self.STREAM_SKIP]
        self._stream_entries = entries
        row = 0
        for i, entry in enumerate(entries):
            if i and i % 5 == 0:
                row += 2
            col = (i % 5) * 2
            key, label, desc, ptype, default, choices = entry
            ttk.Label(frame, text=label, font=("TkDefaultFont", 9, "bold")).grid(
                row=row, column=col, sticky=tk.W, padx=(0, 4))
            ttk.Label(frame, text=desc, wraplength=170,
                      font=("TkDefaultFont", 8)).grid(
                row=row + 1, column=col, columnspan=2, sticky=tk.W, padx=(0, 4))
            self._make_widget(frame, "sr_" + key, ptype, default, choices, col, row)

        row += 2
        ttk.Label(frame, text="Versions (one per pattern)",
                  font=("TkDefaultFont", 9, "bold")).grid(row=row, column=0, sticky=tk.W)
        self._stream_versions = tk.Listbox(frame, selectmode=tk.EXTENDED,
                                           height=6, width=46)
        self._stream_versions.grid(row=row + 1, column=0, columnspan=5, sticky=tk.W)

        ttk.Label(frame, text="Saved preset",
                  font=("TkDefaultFont", 9, "bold")).grid(row=row, column=6, sticky=tk.W)
        self._stream_preset = ttk.Combobox(frame, width=30, state="readonly")
        self._stream_preset.grid(row=row + 1, column=6, columnspan=2, sticky=tk.W)
        ttk.Label(frame, text="Preset name").grid(row=row + 2, column=6, sticky=tk.W)
        self._stream_preset_name = ttk.Entry(frame, width=30)
        self._stream_preset_name.grid(row=row + 3, column=6, columnspan=2, sticky=tk.W)

        controls = ttk.Frame(frame)
        controls.grid(row=row + 2, column=0, columnspan=4, sticky=tk.W)
        self._stream_run_btn = ttk.Button(
            controls, text="Run pinned backtest", command=self._run_stream)
        self._stream_run_btn.pack(side=tk.LEFT)
        self._stream_cancel_btn = ttk.Button(
            controls, text="Cancel run", command=self._cancel_stream, state=tk.DISABLED)
        self._stream_cancel_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._stream_status_var = tk.StringVar(value="Idle.")
        ttk.Label(controls, textvariable=self._stream_status_var).pack(
            side=tk.LEFT, padx=(8, 0))
        self._refresh_stream_choices()

    def _refresh_stream_choices(self) -> None:
        """Catalog/preset lists are optional: a DB outage leaves them empty."""
        try:
            from core.backtest_service import BacktestService

            service = BacktestService()
            self._stream_catalog = {
                row["id"]: row["active"] for row in service.versions.catalog()
                if row["enabled"] and row["active"]
            }
            self._stream_presets = {p.name: p.preset_id for p in service.list_presets()}
        except Exception as exc:  # noqa: BLE001 - surfaced on Run instead
            self._stream_status_var.set(f"Catalog unavailable: {exc}")
            self._stream_catalog = {}
            self._stream_presets = {}
        self._stream_versions.delete(0, tk.END)
        for pattern, version in sorted(self._stream_catalog.items()):
            self._stream_versions.insert(tk.END, f"{pattern} -> {version[:8]}")
            self._stream_versions.selection_set(tk.END)
        self._stream_preset["values"] = [""] + sorted(self._stream_presets)

    def _collect_stream_values(self) -> dict:
        from core.market import parse_extra_symbols

        values: dict = {}
        for key, _label, _desc, ptype, default, _choices in self._stream_entries:
            var = self._vars["sr_" + key]
            if ptype == "check":
                values[key] = bool(var.get())
            elif ptype == "spin":
                try:
                    values[key] = float(var.get())
                except (TypeError, ValueError, tk.TclError):
                    values[key] = float(default[0])
            elif ptype == "combo":
                values[key] = str(var.get() or default)
            else:
                text = str(var.get()).strip()
                values[key] = text or None
        values["market"] = self._vars["market"].get() or default_market().id
        values["symbols"] = parse_extra_symbols(self._vars["extra_symbols"].get())
        values["universe"] = (self._vars["universe"].get() or "").strip() or None
        # The shared form owns the transaction cost; keep the panel consistent.
        try:
            values["txn_cost_pct"] = float(self._vars["txn_cost_pct"].get())
        except (KeyError, TypeError, ValueError, tk.TclError):
            values["txn_cost_pct"] = 0.0
        return values

    def _selected_stream_versions(self) -> dict[str, str]:
        patterns = sorted(self._stream_catalog)
        chosen = {}
        for index in self._stream_versions.curselection():
            if index < len(patterns):
                pattern = patterns[index]
                chosen[pattern] = self._stream_catalog[pattern]
        return chosen

    def _run_stream(self) -> None:
        if self._stream_running:
            return
        from core.backtest_service import BacktestService, request_from_values

        self._stream_status_var.set("Submitting…")
        self._top.update_idletasks()
        try:
            service = BacktestService()
            preset_name = self._stream_preset.get()
            request = request_from_values(
                service, self._collect_stream_values(),
                versions=self._selected_stream_versions() or None,
                preset_id=self._stream_presets.get(preset_name) if preset_name else None,
                preset_name=self._stream_preset_name.get() or None,
            )
            job = service.submit(request)
        except Exception as exc:  # noqa: BLE001 - shown in the status bar
            self._stream_status_var.set(f"Error: {exc}")
            return
        self._stream_running = True
        self._stream_run_id = job["id"]
        self._stream_service = service
        self._stream_run_btn.config(state=tk.DISABLED)
        self._stream_cancel_btn.config(state=tk.NORMAL)
        self._stream_status_var.set(f"Run {job['id'][:8]}: queued")
        threading.Thread(target=self._stream_worker, args=(service, job["id"]),
                         daemon=True, name="ui-stream-backtest").start()
        self._top.after(1000, self._poll_stream)

    def _stream_worker(self, service, run_id: str) -> None:
        try:
            service.execute(run_id)
        except Exception as exc:  # noqa: BLE001 - poll reports the durable state
            log.warning(f"UI stream backtest | run {run_id}: {exc}")

    def _cancel_stream(self) -> None:
        if not self._stream_run_id:
            return
        try:
            self._stream_service.cancel(self._stream_run_id)
            self._stream_status_var.set("Cancellation requested…")
        except Exception as exc:  # noqa: BLE001
            self._stream_status_var.set(f"Error: {exc}")

    def _poll_stream(self) -> None:
        if self._closed or not self._stream_run_id:
            return
        service = self._stream_service
        try:
            status = service.status(self._stream_run_id)
        except Exception as exc:  # noqa: BLE001
            self._stream_status_var.set(f"Error: {exc}")
            self._finish_stream()
            return
        progress = status.get("progress") or {}
        done, total = progress.get("completed_units", 0), progress.get("total_units")
        self._stream_status_var.set(
            f"Run {self._stream_run_id[:8]}: {status['state']} "
            f"({done}/{total if total is not None else '?'} {progress.get('unit', 'units')})")
        if status["state"] in ("completed", "failed", "cancelled", "blocked", "interrupted"):
            self._render_stream_result(service, status)
            self._finish_stream()
            return
        self._top.after(1000, self._poll_stream)

    def _render_stream_result(self, service, status: dict) -> None:
        lines = [f"Run {self._stream_run_id}", f"State: {status['state']}"]
        error = status.get("error")
        if error:
            lines.append(f"Error [{error.get('code')}]: {error.get('message')}")
        payload = service.result(self._stream_run_id) if status["state"] == "completed" else None
        if payload is not None:
            m = payload["result"].metrics
            settings = payload["inputs"].request.preset.settings
            lines += [
                f"Mode:        {settings.mode}",
                f"End policy:  {settings.execution.end_policy}",
                "",
                f"Trades:      {m.trade_count}"
                + ("  (zero trades is a valid result, not a failure)" if m.trade_count == 0 else ""),
                f"Win rate:    {'n/a' if m.win_rate is None else f'{m.win_rate:.1%}'}",
                f"Realized:    {m.realized_pnl:,.2f}",
                f"Unrealized:  {m.unrealized_pnl:,.2f}",
                f"Net P&L:     {m.net_pnl:,.2f}",
                f"Fees:        {m.fees:,.4f}",
                f"Max DD %:    {m.max_drawdown_pct:.2f}",
                f"Open:        {m.open_position_count}",
                f"Currency:    {m.currency}",
            ]
        self._summary_text.config(state=tk.NORMAL)
        self._summary_text.delete("1.0", tk.END)
        self._summary_text.insert(tk.END, "\n".join(lines) + "\n")
        self._summary_text.config(state=tk.DISABLED)
        if payload is not None:
            self._tree.delete(*self._tree.get_children())
            for trade in payload["trades"].get("trades", []):
                self._tree.insert("", tk.END, values=(
                    str(trade.get("entryDate", ""))[:10], trade.get("action"),
                    trade.get("sym"), trade.get("timeframe"), trade.get("entryPrice"),
                    trade.get("exitPrice"), trade.get("pnlPct"),
                    trade.get("exitReason"), trade.get("pattern")))

    def _finish_stream(self) -> None:
        self._stream_running = False
        self._stream_run_id = None
        self._stream_run_btn.config(state=tk.NORMAL)
        self._stream_cancel_btn.config(state=tk.DISABLED)

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
