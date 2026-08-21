"""Tk dialog: ticker + horizon → Kronos overlay on the trade chart viewer."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from core.kronos_forecast import DEFAULT_PRED_DAYS, MAX_PRED_DAYS, MIN_PRED_DAYS
from core.market import default_market
from ui.tv_chart import TradingViewChart
from utils.logger import log


class KronosPredictDialog:
    def __init__(self, parent: tk.Misc, *, market: str | None = None):
        self._closed = False
        self._busy = False
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()

        self.win = tk.Toplevel(parent)
        self.win.title("Kronos predict")
        self.win.geometry("1120x740")
        self.win.minsize(720, 480)
        self.win.transient(parent)

        bar = ttk.Frame(self.win, padding=(10, 8))
        bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(bar, text="Symbol").pack(side=tk.LEFT)
        self.symbol_var = tk.StringVar(value="AAPL")
        ttk.Entry(bar, textvariable=self.symbol_var, width=12).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(bar, text="Days").pack(side=tk.LEFT)
        self.days_var = tk.IntVar(value=DEFAULT_PRED_DAYS)
        ttk.Spinbox(
            bar, from_=MIN_PRED_DAYS, to=MAX_PRED_DAYS, increment=1,
            width=5, textvariable=self.days_var,
        ).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(bar, text="Market").pack(side=tk.LEFT)
        self.market_var = tk.StringVar(value=market or default_market().id)
        ttk.Combobox(
            bar, textvariable=self.market_var, values=["us", "ph"],
            state="readonly", width=6,
        ).pack(side=tk.LEFT, padx=(4, 12))

        self._run_btn = ttk.Button(bar, text="Predict", command=self._run_threaded)
        self._run_btn.pack(side=tk.LEFT)
        self.status_var = tk.StringVar(value="Enter a ticker and how many trading days to forecast.")
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.LEFT, padx=(12, 0))

        self.chart = TradingViewChart(self.win)
        self.chart.pack(fill=tk.BOTH, expand=True)

        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        self.win.after(50, self._drain)
        self.win.bind("<Return>", lambda _e: self._run_threaded())
        self.win.focus_set()

    def _run_threaded(self) -> None:
        if self._busy:
            return
        symbol = self.symbol_var.get()
        try:
            days = int(self.days_var.get())
        except (tk.TclError, TypeError, ValueError):
            self.status_var.set("Days must be an integer.")
            return
        market = self.market_var.get()
        self._busy = True
        self._run_btn.configure(state=tk.DISABLED)
        self.status_var.set(f"Running Kronos on {symbol.strip().upper()} ({days}d)…")
        threading.Thread(
            target=self._run, args=(symbol, days, market), daemon=True,
        ).start()

    def _run(self, symbol: str, days: int, market: str) -> None:
        from core.kronos_forecast import forecast_symbol

        try:
            payload = forecast_symbol(symbol, days, market=market)
        except ValueError as exc:
            msg = str(exc)
            self._safe(lambda: self._fail(msg))
            return
        except Exception as exc:
            log.exception("UI | Kronos predict failed")
            msg = f"Kronos prediction failed: {exc}"
            self._safe(lambda: self._fail(msg))
            return
        self._safe(lambda: self._show(payload))

    def _show(self, payload: dict) -> None:
        self.chart.set_payload(payload)
        pred = payload.get("pred") or {}
        last = pred.get("last_close")
        close = pred.get("pred_close")
        pct = pred.get("pred_return_pct")
        days = pred.get("days")
        origin = pred.get("origin")
        if last is None or close is None or pct is None:
            self.status_var.set("Forecast ready.")
        else:
            self.status_var.set(
                f"{payload.get('symbol')} from {origin}: last {last:.2f} → "
                f"Kronos {days}d {close:.2f} ({pct:+.2f}%)"
            )
        self._busy = False
        self._run_btn.configure(state=tk.NORMAL)

    def _fail(self, msg: str) -> None:
        self._busy = False
        self._run_btn.configure(state=tk.NORMAL)
        self.status_var.set(msg)

    def _safe(self, fn: Callable[[], None]) -> None:
        if not self._closed:
            self._ui_queue.put(fn)

    def _drain(self) -> None:
        if self._closed:
            return
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except tk.TclError:
                self._closed = True
                return
        self.win.after(50, self._drain)

    def _on_close(self) -> None:
        self._closed = True
        self.win.destroy()
