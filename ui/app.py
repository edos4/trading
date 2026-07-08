"""
ui/app.py - Cross-platform Python-native GUI for exploring symbols, charts,
downloaded data, and detected patterns.

Built with tkinter (stdlib) so it runs unchanged on Windows, macOS, and Linux.
Data reuse:
  - TVClient.fetch_top_symbols_with_exchanges  -> symbol list
  - TVClient._fetch_history_screener           -> OHLCV history (no MCP needed)
  - ChartRenderer.render_with_ema              -> TradingView-style PNG
  - scanner pattern discovery                  -> runs every pattern's analyze()

Run:
    python main.py --ui
"""

from __future__ import annotations

import importlib
import io
import pkgutil
import queue
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

import matplotlib
import patterns as patterns_pkg
from PIL import Image, ImageTk

matplotlib.use("Agg", force=True)

from analysis.chart_renderer import ChartRenderer
from config import settings
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot, TVClient
from patterns.base_pattern import BasePattern, TradeSignal
from ui.backtest_dialog import BacktestDialog
from utils.logger import log

TIMEFRAMES = ["1d", "1W"]
DEFAULT_SYMBOL_COUNT = 50


def discover_patterns() -> list[BasePattern]:
    """Mirror core.scanner._discover_patterns - instantiate every pattern class."""
    found: list[BasePattern] = []
    for module_info in pkgutil.iter_modules(patterns_pkg.__path__):
        if module_info.name.startswith("_") or module_info.name == "base_pattern":
            continue
        try:
            module = importlib.import_module(f"patterns.{module_info.name}")
        except Exception as exc:
            log.warning(f"UI | Failed to import pattern {module_info.name}: {exc}")
            continue
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BasePattern)
                and attr is not BasePattern
            ):
                try:
                    found.append(attr())
                except Exception as exc:
                    log.warning(f"UI | Failed to instantiate {attr_name}: {exc}")
    return found


class TradingBotUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Trading Bot v2 - Symbol Explorer")
        root.geometry("1280x820")
        root.minsize(960, 640)
        try:
            root.tk.call("tk", "scaling", 1.2)
        except tk.TclError:
            pass

        self._tv = TVClient(
            settings.tv_screener,
            settings.tv_exchange,
            exchange_overrides=None,
        )
        self._renderer = ChartRenderer(save_to_disk=False)
        self._patterns = discover_patterns()
        self._store = OHLCVStore(
            window=max(365, settings.tv_history_days)
        )

        self._symbols: list[tuple[str, str]] = []
        self._filtered_rows: list[tuple[str, str]] = []
        self._current_symbol: Optional[str] = None
        self._current_df = None
        self._current_signals: list[TradeSignal] = []
        self._photo: Optional[ImageTk.PhotoImage] = None  # keep ref alive
        self._busy = False
        self._closed = False
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._drain_ui_queue)
        self.root.after(100, self._load_symbols_threaded)

    # Layout
    def _build_ui(self) -> None:
        # Top toolbar
        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="Refresh symbols", command=self._load_symbols_threaded).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Backtest", command=self._open_backtest_dialog).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(toolbar, text="Count:").pack(side=tk.LEFT, padx=(12, 2))
        self.count_var = tk.IntVar(value=DEFAULT_SYMBOL_COUNT)
        ttk.Spinbox(
            toolbar, from_=5, to=200, increment=5, width=5, textvariable=self.count_var
        ).pack(side=tk.LEFT)
        ttk.Label(toolbar, text="Timeframe:").pack(side=tk.LEFT, padx=(12, 2))
        self.tf_var = tk.StringVar(value=TIMEFRAMES[0])
        ttk.Combobox(
            toolbar, textvariable=self.tf_var, values=TIMEFRAMES,
            state="readonly", width=6,
        ).pack(side=tk.LEFT)
        ttk.Label(toolbar, text="Filter:").pack(side=tk.LEFT, padx=(12, 2))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        ttk.Entry(toolbar, textvariable=self.filter_var, width=10).pack(side=tk.LEFT)
        self.run_patterns_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="Run patterns", variable=self.run_patterns_var).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(toolbar, text="Download CSV", command=self._download_csv).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="Save chart PNG", command=self._save_chart_png).pack(side=tk.RIGHT, padx=(0, 6))

        # Main body: left list + right chart/info
        body = ttk.Frame(self.root)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Left: symbol list
        left = ttk.Frame(body, padding=(8, 4))
        left.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(left, text="Symbols", font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W)
        list_frame = ttk.Frame(left)
        list_frame.pack(fill=tk.Y, expand=True)
        self.listbox = tk.Listbox(
            list_frame, width=14, activestyle="dotbox",
            exportselection=False,
        )
        self.listbox.pack(side=tk.LEFT, fill=tk.Y, expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_symbol_select)
        list_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.listbox.yview)
        list_scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.listbox.config(yscrollcommand=list_scroll.set)

        # Right: chart + info
        right = ttk.Frame(body, padding=(4, 4))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.header_var = tk.StringVar(value="Select a symbol to load its chart.")
        ttk.Label(right, textvariable=self.header_var, font=("TkDefaultFont", 12, "bold")).pack(anchor=tk.W, pady=(0, 4))

        chart_outer = ttk.Frame(right)
        chart_outer.pack(fill=tk.BOTH, expand=True)
        self.chart_canvas = tk.Canvas(chart_outer, bg="#131722", highlightthickness=0)
        self.chart_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        chart_scroll = ttk.Scrollbar(chart_outer, orient=tk.VERTICAL, command=self.chart_canvas.yview)
        chart_scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.chart_canvas.config(yscrollcommand=chart_scroll.set)
        self.chart_inner = ttk.Frame(self.chart_canvas)
        self.chart_window_id = self.chart_canvas.create_window((0, 0), window=self.chart_inner, anchor=tk.NW)
        self.chart_inner.bind("<Configure>", lambda e: self.chart_canvas.config(scrollregion=self.chart_canvas.bbox("all")))
        self.chart_canvas.bind("<Configure>", self._on_canvas_resize)
        self._chart_label = ttk.Label(self.chart_inner, text="No chart yet", anchor=tk.CENTER)
        self._chart_label.pack(fill=tk.BOTH, expand=True)

        # Pattern info
        info_outer = ttk.Frame(right)
        info_outer.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        ttk.Label(info_outer, text="Detected patterns", font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W)
        cols = ("pattern", "action", "tf", "conf", "price", "notes")
        self.tree = ttk.Treeview(info_outer, columns=cols, show="headings", height=6)
        for c, w in zip(cols, (160, 60, 50, 60, 90, 320)):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w, anchor=tk.W)
        self.tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        info_scroll = ttk.Scrollbar(info_outer, orient=tk.VERTICAL, command=self.tree.yview)
        info_scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.config(yscrollcommand=info_scroll.set)

        # Status bar + progress bar
        status_frame = ttk.Frame(self.root)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(status_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress = ttk.Progressbar(
            status_frame, mode="indeterminate", length=120,
        )
        self.progress.pack(side=tk.RIGHT, padx=(4, 0))

    # Symbol loading
    def _load_symbols_threaded(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.status_var.set("Fetching symbol list from TradingView...")
        self.progress.start(10)
        n = self.count_var.get()
        threading.Thread(target=self._load_symbols, args=(n,), daemon=True).start()

    def _load_symbols(self, n: int) -> None:
        try:
            rows = TVClient.fetch_top_symbols_with_exchanges(n, settings.tv_screener)
        except Exception as exc:
            msg = f"Symbol fetch failed: {exc}"
            self._safe_after(lambda: self._fail(msg))
            return
        if not rows:
            self._safe_after(lambda: self._fail("No symbols returned by screener."))
            return
        self._symbols = sorted(rows, key=lambda x: x[0].upper())
        self._safe_after(lambda: self._populate_list())

    def _populate_list(self) -> None:
        self._apply_filter()
        self._busy = False
        self.progress.stop()
        self.status_var.set(f"Loaded {len(self._symbols)} symbols.")

    def _apply_filter(self) -> None:
        q = self.filter_var.get().strip().upper()
        self.listbox.delete(0, tk.END)
        self._filtered_rows = []
        for sym, exch in self._symbols:
            if q and q not in sym:
                continue
            self.listbox.insert(tk.END, sym)
            self._filtered_rows.append((sym, exch))

    # Symbol selection -> load chart + patterns
    def _on_symbol_select(self, _event) -> None:
        sel = self.listbox.curselection()
        if not sel or self._busy:
            return
        idx = sel[0]
        if idx >= len(self._filtered_rows):
            return
        symbol, exchange = self._filtered_rows[idx]
        self._current_symbol = symbol
        timeframe = self.tf_var.get()
        self.header_var.set(f"{symbol} | {timeframe} | {exchange} - loading...")
        self._busy = True
        self.status_var.set(f"Loading {symbol} {timeframe}...")
        self.progress.start(10)
        threading.Thread(
            target=self._load_symbol,
            args=(symbol, exchange, timeframe),
            daemon=True,
        ).start()

    def _load_symbol(self, symbol: str, exchange: str, timeframe: str) -> None:
        try:
            candles = self._tv._fetch_history_screener(symbol, exchange, timeframe)
        except Exception as exc:
            msg = f"History fetch failed for {symbol}: {exc}"
            self._safe_after(lambda: self._fail(msg))
            return
        if not candles:
            self._safe_after(lambda: self._fail(f"No history available for {symbol} {timeframe}."))
            return

        self._store.replace_all(symbol, timeframe, candles)
        df = self._store.get_df(symbol, timeframe, min_bars=2)
        if df is None:
            self._safe_after(lambda: self._fail(f"Insufficient bars for {symbol} {timeframe}."))
            return

        latest = candles[-1]
        snapshot = MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=datetime.now(timezone.utc),
            candle=latest,
            indicators={},
            summary={"RECOMMENDATION": "NEUTRAL"},
            oscillators={},
            moving_avgs={},
        )

        signals: list[TradeSignal] = []
        if self.run_patterns_var.get():
            for pattern in self._patterns:
                if timeframe not in pattern.timeframes:
                    continue
                try:
                    sig = pattern.analyze(snapshot, self._store)
                except Exception as exc:
                    log.warning(f"UI | {pattern.name} failed on {symbol} {timeframe}: {exc}")
                    continue
                if sig is not None:
                    signals.append(sig)

        annotations: list[dict] = []
        for s in signals:
            annotations.extend(s.chart_annotations)

        try:
            png = self._renderer.render_with_ema(
                symbol, timeframe, df, annotations=annotations or None,
            )
        except Exception as exc:
            msg = f"Chart render failed for {symbol}: {exc}"
            self._safe_after(lambda: self._fail(msg))
            return

        self._safe_after(lambda: self._render_result(symbol, timeframe, exchange, df, signals, png))

    def _render_result(self, symbol, timeframe, exchange, df, signals, png) -> None:
        self._current_df = df
        self._current_signals = signals
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last
        change = last["close"] - prev["close"]
        pct = (change / prev["close"] * 100) if prev["close"] else 0
        self.header_var.set(
            f"{symbol} | {timeframe} | {exchange}  -  "
            f"O {last['open']:.2f}  H {last['high']:.2f}  L {last['low']:.2f}  "
            f"C {last['close']:.2f}  {change:+.2f} ({pct:+.2f}%)  "
            f"bars={len(df)}"
        )

        image = Image.open(io.BytesIO(png))
        # fit width to canvas, keep aspect
        canvas_w = max(self.chart_canvas.winfo_width() - 12, 400)
        scale = canvas_w / image.width
        scaled = image.resize((canvas_w, int(image.height * scale)), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(scaled)
        self._chart_label.config(image=self._photo, text="")

        # pattern table
        self.tree.delete(*self.tree.get_children())
        for s in signals:
            self.tree.insert(
                "", tk.END,
                values=(
                    s.pattern, s.action, s.timeframe,
                    f"{s.confidence:.2f}", f"{s.price:.2f}", s.notes,
                ),
            )
        self.status_var.set(
            f"{symbol} {timeframe}: {len(df)} bars, {len(signals)} pattern(s) detected."
        )
        self.progress.stop()
        self._busy = False

    # Downloads / saves
    def _open_backtest_dialog(self) -> None:
        BacktestDialog(self.root)

    def _download_csv(self) -> None:
        if self._current_df is None or self._current_symbol is None:
            self.status_var.set("Load a symbol first.")
            return
        default = f"{self._current_symbol}_{self.tf_var.get()}.csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default,
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self._current_df.to_csv(path)
        except Exception as exc:
            messagebox.showerror("Download failed", str(exc))
            return
        self.status_var.set(f"Saved CSV -> {path}")

    def _save_chart_png(self) -> None:
        if self._current_symbol is None:
            self.status_var.set("Load a symbol first.")
            return
        default = f"{self._current_symbol}_{self.tf_var.get()}.png"
        path = filedialog.asksaveasfilename(
            defaultextension=".png", initialfile=default,
            filetypes=[("PNG", "*.png"), ("All files", "*.*")],
        )
        if not path:
            return
        df = self._current_df
        if df is None:
            return
        annotations: list[dict] = []
        for s in self._current_signals:
            annotations.extend(s.chart_annotations)
        try:
            png = self._renderer.render_with_ema(
                self._current_symbol, self.tf_var.get(), df,
                annotations=annotations or None,
            )
            with open(path, "wb") as f:
                f.write(png)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.status_var.set(f"Saved chart -> {path}")

    # Helpers
    def _safe_after(self, fn: Callable[[], None]) -> None:
        """Queue fn for the Tk main thread, tolerating a closed window."""
        if not self._closed:
            self._ui_queue.put(fn)

    def _drain_ui_queue(self) -> None:
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
            except Exception as exc:
                self._fail(f"UI update failed: {exc}")
        self.root.after(50, self._drain_ui_queue)

    def _on_close(self) -> None:
        self._closed = True
        self.root.destroy()

    def _on_canvas_resize(self, event) -> None:
        self.chart_canvas.itemconfig(self.chart_window_id, width=event.width)
        if self._current_symbol and not self._busy:
            # re-fit image without re-fetching
            self._busy = True
            self.root.after(150, lambda: self._refit_image(event.width - 12))

    def _refit_image(self, width: int) -> None:
        # Simplest refit: re-render current df. ChartRenderer is cheap enough.
        self._busy = False

    def _fail(self, msg: str) -> None:
        self._busy = False
        self.progress.stop()
        self.status_var.set(msg)
        self.header_var.set("Error")
        log.warning(f"UI | {msg}")


def run() -> None:
    root = tk.Tk()
    TradingBotUI(root)
    root.mainloop()


if __name__ == "__main__":
    run()
