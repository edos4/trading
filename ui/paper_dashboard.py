"""
ui/paper_dashboard.py — Dual-book paper desk (US + PH).

Each market is a PaperBook thread owned by core.paper_books.paper_books.
This window is a view: Start/Stop/Reset per card, combined blotter below.
"""

from __future__ import annotations

import base64
import io
import json
from datetime import date, datetime, timedelta, timezone
from tkinter import ttk, filedialog, messagebox
import tkinter as tk
from typing import Optional

from PIL import Image, ImageTk
from tkcalendar import DateEntry

from config import settings
from core.market import format_money, get_market
from core.paper_books import BOOK_IDS, paper_books
from utils.trade_display import (
    format_exit_reason,
    format_hold,
    format_pattern_name,
    format_stamp,
)

COLOR_GAIN = "#1b7a1b"
COLOR_LOSS = "#c0392b"
COLOR_BUY = "#1b6fc0"
COLOR_SELL = "#c0392b"
COLOR_TRAILING = "#b8860b"
COLOR_BREAKEVEN = "#6a5acd"
COLOR_MUTED = "#666666"
COLOR_US = "#1b6fc0"
COLOR_PH = "#b8860b"


def _stamp(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        return format_stamp(value)
    try:
        return format_stamp(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return str(value)


def _pnl_tag(value: float) -> str:
    if value > 0:
        return "gain"
    if value < 0:
        return "loss"
    return "flat"


class _SortableTree(ttk.Treeview):
    def __init__(self, master, columns: list[tuple], on_sort, **kw):
        col_ids = [c[0] for c in columns]
        super().__init__(master, columns=col_ids, show="headings", **kw)
        self._col_labels = {}
        for spec in columns:
            col_id, width, label = spec[0], spec[1], spec[2]
            anchor = spec[3] if len(spec) > 3 else tk.W
            self._col_labels[col_id] = label
            self.heading(col_id, text=label, command=lambda c=col_id: on_sort(c))
            self.column(col_id, width=width, anchor=anchor, stretch=False)

    def set_sort(self, col: str, descending: bool) -> None:
        for cid, label in self._col_labels.items():
            mark = ""
            if cid == col:
                mark = " ▼" if descending else " ▲"
            self.heading(cid, text=f"{label}{mark}")


class MarketBookFrame(ttk.LabelFrame):
    def __init__(self, master, market: str):
        profile = get_market(market)
        super().__init__(master, text=profile.label)
        self.market = profile.id
        self.n_var = tk.IntVar(value=profile.default_n_symbols)
        self.extra_var = tk.StringVar(value="")
        self.stream_var = tk.BooleanVar(value=False)
        self.kronos_gate_var = tk.BooleanVar(value=profile.kronos_gate_default)
        self.kronos_rank_var = tk.BooleanVar(value=profile.kronos_rank_default)
        self.kronos_batch_var = tk.BooleanVar(value=settings.kronos_batch_enabled)
        self.volume_gate_var = tk.BooleanVar(value=settings.volume_gate_enabled)
        self.equity_var = tk.StringVar()
        self.exposure_var = tk.StringVar()
        self.scan_var = tk.StringVar(value="Last scan: —")
        self.status_var = tk.StringVar(value="Idle.")

        ttk.Label(self, textvariable=self.equity_var, font=("TkDefaultFont", 10, "bold")).pack(
            anchor=tk.W, padx=8, pady=(6, 0),
        )
        ttk.Label(self, textvariable=self.exposure_var, foreground=COLOR_MUTED).pack(
            anchor=tk.W, padx=8,
        )
        ttk.Label(self, textvariable=self.scan_var, foreground=COLOR_MUTED).pack(
            anchor=tk.W, padx=8,
        )
        ttk.Label(self, textvariable=self.status_var, foreground=COLOR_MUTED).pack(
            anchor=tk.W, padx=8, pady=(0, 4),
        )

        row = ttk.Frame(self)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Symbols:").pack(side=tk.LEFT)
        ttk.Spinbox(row, from_=5, to=5000, increment=5, width=6, textvariable=self.n_var).pack(
            side=tk.LEFT, padx=(4, 8),
        )
        ttk.Label(row, text="Additional:").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.extra_var, width=14).pack(side=tk.LEFT, padx=(4, 8))

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=8, pady=2)
        self.start_btn = ttk.Button(btns, text="Start")
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(btns, text="Stop", state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.reset_btn = ttk.Button(btns, text="Reset")
        self.reset_btn.pack(side=tk.LEFT, padx=(6, 0))

        flags = ttk.Frame(self)
        flags.pack(fill=tk.X, padx=8, pady=(2, 8))
        ttk.Checkbutton(
            flags, text="Kronos 3d gate", variable=self.kronos_gate_var,
            command=self._sync_batch_kronos,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            flags, text="Kronos rank", variable=self.kronos_rank_var,
            command=self._sync_batch_kronos,
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._batch_kronos_cb = ttk.Checkbutton(
            flags, text="Batch Kronos", variable=self.kronos_batch_var,
        )
        self._volume_gate_cb = ttk.Checkbutton(
            flags, text="Volume gate", variable=self.volume_gate_var,
        )
        self._volume_gate_cb.pack(side=tk.LEFT, padx=(8, 0))
        self.stream_check = ttk.Checkbutton(
            flags, text="Stream", variable=self.stream_var, command=self._on_stream_toggle,
        )
        self.stream_check.pack(side=tk.LEFT, padx=(8, 0))
        self._stream_start_frame = ttk.Frame(flags)
        ttk.Label(self._stream_start_frame, text="Start:").pack(side=tk.LEFT, padx=(8, 2))
        self.stream_start_picker = DateEntry(
            self._stream_start_frame, width=11, date_pattern="yyyy-mm-dd",
            **self._stream_start_date_kwargs(),
        )
        self.stream_start_picker.pack(side=tk.LEFT)
        self._sync_batch_kronos()

    def _sync_batch_kronos(self) -> None:
        if self.kronos_gate_var.get() or self.kronos_rank_var.get():
            self._batch_kronos_cb.pack(
                side=tk.LEFT, padx=(8, 0), before=self._volume_gate_cb,
            )
        else:
            self._batch_kronos_cb.pack_forget()

    @staticmethod
    def _stream_start_date_kwargs() -> dict:
        default = date.today() - timedelta(days=365)
        raw = settings.papertrade_stream_start_date
        if raw:
            try:
                default = date.fromisoformat(raw.strip())
            except ValueError:
                pass
        return {"year": default.year, "month": default.month, "day": default.day}

    def _on_stream_toggle(self) -> None:
        if self.stream_var.get():
            self._stream_start_frame.pack(side=tk.LEFT)
        else:
            self._stream_start_frame.pack_forget()

    def start_kwargs(self) -> dict:
        stream_start = None
        if self.stream_var.get():
            stream_start = self.stream_start_picker.get_date().isoformat()
        return {
            "n_symbols": int(self.n_var.get()),
            "extra_symbols": self.extra_var.get(),
            "use_stream": bool(self.stream_var.get()),
            "kronos_gate": bool(self.kronos_gate_var.get()),
            "kronos_rank": bool(self.kronos_rank_var.get()),
            "kronos_batch": bool(
                (self.kronos_gate_var.get() or self.kronos_rank_var.get())
                and self.kronos_batch_var.get()
            ),
            "volume_gate": bool(self.volume_gate_var.get()),
            "stream_start": stream_start,
        }

    def apply_snapshot(self, snap: dict) -> None:
        mkt = snap.get("market") or self.market
        sym = snap.get("currency_symbol") or get_market(mkt).currency_symbol
        equity = float(snap.get("equity") or 0)
        cash = float(snap.get("cash") or 0)
        metrics = snap.get("metrics") or {}
        total_pnl = float(metrics.get("total_pnl_dollars") or 0)
        total_pct = float(metrics.get("total_pnl_pct") or 0)
        self.equity_var.set(
            f"{sym}  Cash: {format_money(cash, mkt)}   "
            f"Equity: {format_money(equity, mkt)}   "
            f"P&L: {format_money(total_pnl, mkt, signed=True)} ({total_pct:+.2f}%)   "
            f"Open: {snap.get('open_count', 0)}   Closed: {snap.get('closed_count', 0)}"
        )
        exp = snap.get("exposure") or {}
        short_bit = (
            "Short: 0% · long-only" if snap.get("long_only")
            else f"Short: {float(exp.get('short_pct') or 0):.1f}%"
        )
        self.exposure_var.set(
            f"Long: {float(exp.get('long_pct') or 0):.1f}%   {short_bit}   "
            f"Net: {float(exp.get('net_pct') or 0):+.1f}%   "
            f"Gross: {float(exp.get('gross_pct') or 0):.1f}%"
        )
        stats = snap.get("scan_stats")
        if not stats:
            self.scan_var.set("Last scan: —")
        else:
            last = stats.get("last_scan_at")
            last_str = "—" if not last else datetime.fromisoformat(last).strftime("%H:%M:%S")
            self.scan_var.set(
                f"Last scan: {last_str}   Patterns: {stats.get('patterns_found')}   "
                f"Opened: {stats.get('trades_opened')}   Rejected: {stats.get('signals_rejected')}   "
                f"Scan: {float(stats.get('scan_duration_s') or 0):.1f}s"
            )
        self.status_var.set(snap.get("error") or snap.get("status") or "")
        running = bool(snap.get("running"))
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        blocked = snap.get("stream_blocked_by")
        self.stream_check.config(state=tk.DISABLED if blocked else tk.NORMAL)


class PaperDashboard:
    _instance: Optional["PaperDashboard"] = None

    def __init__(self, master: tk.Widget):
        if PaperDashboard._instance is not None:
            try:
                PaperDashboard._instance._top.lift()
                PaperDashboard._instance._top.focus_force()
                return
            except tk.TclError:
                PaperDashboard._instance = None
        self._top = tk.Toplevel(master)
        self._top.title("Paper Trading — US + PH")
        self._top.geometry("1440x780")
        self._top.minsize(1100, 640)
        self._top.protocol("WM_DELETE_WINDOW", self._on_close)
        PaperDashboard._instance = self

        self._closed = False
        self._photos: dict[str, ImageTk.PhotoImage] = {}
        self._pos_sort = ("unrl_pct", True)
        self._closed_sort = ("closed", True)
        self._log_sort = ("time", True)
        self._book_filter = tk.StringVar(value="All")
        self._pos_rows: dict[str, tuple[str, str]] = {}
        self._closed_rows: dict[str, tuple[str, int]] = {}
        self._envelope: dict = {"clocks": {}, "books": {}}

        strip = ttk.Frame(self._top, padding=(8, 6))
        strip.pack(side=tk.TOP, fill=tk.X)
        self._clock_us = tk.StringVar(value="US  —")
        self._clock_ph = tk.StringVar(value="PH  —")
        ttk.Label(strip, textvariable=self._clock_us, font=("TkDefaultFont", 11, "bold")).pack(side=tk.LEFT)
        ttk.Label(strip, text="    ").pack(side=tk.LEFT)
        ttk.Label(strip, textvariable=self._clock_ph, font=("TkDefaultFont", 11, "bold")).pack(side=tk.LEFT)
        ttk.Button(strip, text="Start both", command=self._start_both).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(strip, text="Stop both", command=self._stop_both).pack(side=tk.RIGHT)

        cards = ttk.PanedWindow(self._top, orient=tk.HORIZONTAL)
        cards.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 6))
        self._frames: dict[str, MarketBookFrame] = {}
        for mid in BOOK_IDS:
            frame = MarketBookFrame(cards, mid)
            self._frames[mid] = frame
            cards.add(frame, weight=1)
            frame.start_btn.config(command=lambda m=mid: self._start_book(m))
            frame.stop_btn.config(command=lambda m=mid: self._stop_book(m))
            frame.reset_btn.config(command=lambda m=mid: self._reset_book(m))

        filter_bar = ttk.Frame(self._top, padding=(8, 0, 8, 4))
        filter_bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(filter_bar, text="Filter:").pack(side=tk.LEFT)
        ttk.Combobox(
            filter_bar, textvariable=self._book_filter, values=["All", "US", "PH"],
            state="readonly", width=6,
        ).pack(side=tk.LEFT, padx=(4, 12))
        self._book_filter.trace_add("write", lambda *_: self._refresh_tables())
        self._sort_note = ttk.Label(
            filter_bar, text="P&L sort is per market — pick US or PH.",
            foreground=COLOR_MUTED,
        )
        self._sort_note.pack(side=tk.LEFT)

        notebook = ttk.Notebook(self._top)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        positions_tab = ttk.Frame(notebook)
        notebook.add(positions_tab, text="Positions")
        logs_tab = ttk.Frame(notebook)
        notebook.add(logs_tab, text="Logs")
        perf_tab = ttk.Frame(notebook)
        notebook.add(perf_tab, text="Performance")
        self._build_positions_tab(positions_tab)
        self._build_logs_tab(logs_tab)
        self._build_performance_tab(perf_tab)
        self._refresh_all()
        self._top.after(1000, self._poll)

    def _filter_id(self) -> str | None:
        raw = self._book_filter.get().strip().lower()
        if raw in ("us", "ph"):
            return raw
        return None

    def _visible_books(self) -> list[dict]:
        books = self._envelope.get("books") or {}
        want = self._filter_id()
        if want:
            snap = books.get(want)
            return [snap] if snap else []
        return [books[mid] for mid in BOOK_IDS if mid in books]

    def _build_positions_tab(self, parent: ttk.Frame) -> None:
        export_bar = ttk.Frame(parent)
        export_bar.pack(fill=tk.X, padx=4, pady=(4, 0))
        ttk.Label(
            export_bar,
            text="Open + closed trades (current filter) for LLM review.",
            foreground=COLOR_MUTED,
        ).pack(side=tk.LEFT)
        ttk.Button(export_bar, text="Export Trades", command=self._export_trades).pack(
            side=tk.RIGHT,
        )
        body = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        body.pack(fill=tk.BOTH, expand=True)
        pos_frame = ttk.LabelFrame(body, text="Open positions (double-click a row for chart)")
        body.add(pos_frame, weight=1)
        pos_cols = [
            ("market", 40, "Mkt"), ("opened", 125, "Opened"), ("symbol", 65, "Symbol"),
            ("status", 85, "Status"), ("action", 55, "Action"), ("entry", 75, "Entry"),
            ("current", 75, "Current"), ("unrl_pct", 70, "Unrl %"), ("r", 50, "R"),
            ("days", 68, "Days"), ("bars", 58, "Bars"),
            ("value", 85, "Value"), ("mtm", 85, "MTM"), ("port_pct", 60, "Port %"),
            ("pattern", 190, "Pattern"),
        ]
        self._pos_tree = self._add_scrollbar(pos_frame, _SortableTree(
            pos_frame, pos_cols, self._on_sort_positions, height=7,
        ))
        self._pos_tree.bind("<Double-1>", self._on_position_double_click)
        self._configure_color_tags(self._pos_tree)

        closed_frame = ttk.LabelFrame(body, text="Latest trades (double-click a row for chart)")
        body.add(closed_frame, weight=2)
        closed_meta = ttk.Frame(closed_frame)
        closed_meta.pack(fill=tk.X, padx=4, pady=(4, 0))
        self._closed_stats_var = tk.StringVar(value="No closed trades yet.")
        ttk.Label(closed_meta, textvariable=self._closed_stats_var, foreground=COLOR_MUTED).pack(side=tk.LEFT)
        closed_cols = [
            ("market", 40, "Mkt"), ("closed", 92, "Closed"), ("symbol", 62, "Symbol"),
            ("action", 50, "Side"), ("qty", 48, "Qty", tk.E), ("entry", 68, "Entry", tk.E),
            ("exit", 68, "Exit", tk.E), ("pnl_d", 88, "P&L", tk.E), ("pnl", 62, "P&L %", tk.E),
            ("r", 48, "R", tk.E), ("reason", 92, "Reason"), ("hold", 82, "Hold", tk.E),
            ("pattern", 170, "Pattern"),
        ]
        tree_wrap = ttk.Frame(closed_frame)
        tree_wrap.pack(fill=tk.BOTH, expand=True)
        self._closed_tree = self._add_scrollbar(tree_wrap, _SortableTree(
            tree_wrap, closed_cols, self._on_sort_closed, height=10,
        ))
        self._closed_tree.column("pattern", stretch=True)
        self._closed_tree.bind("<Double-1>", self._on_closed_double_click)
        self._configure_color_tags(self._closed_tree)

    def _build_logs_tab(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Signal log")
        frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        log_cols = [
            ("market", 40, "Mkt"), ("time", 145, "Time"), ("sim_bar", 105, "Sim Bar"),
            ("symbol", 70, "Symbol"), ("timeframe", 55, "TF"), ("action", 60, "Action"),
            ("pattern", 205, "Pattern"), ("confidence", 60, "Conf"), ("price", 80, "Price"),
            ("status", 85, "Status"), ("reason", 520, "Reason"),
        ]
        self._log_tree = _SortableTree(frame, log_cols, self._on_sort_logs, height=24)
        self._log_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._log_tree.yview)
        self._log_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_tree.tag_configure("accepted", foreground=COLOR_GAIN)
        self._log_tree.tag_configure("filled", foreground=COLOR_BUY)
        self._log_tree.tag_configure("rejected", foreground=COLOR_LOSS)

    def _build_performance_tab(self, parent: ttk.Frame) -> None:
        body = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        self._summary: dict[str, tk.Text] = {}
        self._chart_label: dict[str, ttk.Label] = {}
        for mid, title in (("us", "US equity"), ("ph", "PH equity")):
            pane = ttk.Frame(body)
            body.add(pane, weight=1)
            box = ttk.LabelFrame(pane, text=f"{title} / summary")
            box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
            text = tk.Text(box, height=12, width=42, state=tk.DISABLED, font=("TkFixedFont", 9))
            text.pack(fill=tk.X, padx=4, pady=4)
            self._summary[mid] = text
            lbl = ttk.Label(box)
            lbl.pack(fill=tk.BOTH, expand=True)
            self._chart_label[mid] = lbl

    @staticmethod
    def _add_scrollbar(parent: ttk.Frame, tree: ttk.Treeview) -> ttk.Treeview:
        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        return tree

    @staticmethod
    def _configure_color_tags(tree: ttk.Treeview) -> None:
        tree.tag_configure("gain", foreground=COLOR_GAIN)
        tree.tag_configure("loss", foreground=COLOR_LOSS)
        tree.tag_configure("flat", foreground=COLOR_MUTED)
        tree.tag_configure("buy", foreground=COLOR_BUY)
        tree.tag_configure("sell", foreground=COLOR_SELL)
        tree.tag_configure("book-us", foreground=COLOR_US)
        tree.tag_configure("book-ph", foreground=COLOR_PH)

    def _start_book(self, market: str) -> None:
        err = paper_books.start(market, **self._frames[market].start_kwargs())
        if err:
            messagebox.showerror("Paper trading", err, parent=self._top)

    def _stop_book(self, market: str) -> None:
        paper_books.stop(market)

    def _reset_book(self, market: str) -> None:
        if paper_books.books[market].running:
            messagebox.showinfo("Paper trading", f"Stop the {market.upper()} session before resetting.")
            return
        if not messagebox.askyesno("Reset account", f"Wipe the {market.upper()} paper account and signal log?"):
            return
        err = paper_books.reset(market)
        if err:
            messagebox.showerror("Paper trading", err, parent=self._top)
        self._refresh_all()

    def _start_both(self) -> None:
        errors = paper_books.start_both({
            mid: self._frames[mid].start_kwargs() for mid in BOOK_IDS
        })
        if errors:
            messagebox.showwarning(
                "Paper trading",
                "\n".join(f"{k.upper()}: {v}" for k, v in errors.items()),
                parent=self._top,
            )

    def _stop_both(self) -> None:
        paper_books.stop_all()

    def _on_sort_positions(self, col: str) -> None:
        cur, desc = self._pos_sort
        self._pos_sort = (col, not desc if col == cur else True)
        self._refresh_positions()

    def _on_sort_closed(self, col: str) -> None:
        cur, desc = self._closed_sort
        self._closed_sort = (col, not desc if col == cur else True)
        self._refresh_closed()

    def _on_sort_logs(self, col: str) -> None:
        cur, desc = self._log_sort
        self._log_sort = (col, not desc if col == cur else True)
        self._refresh_logs()

    def _open_chart(self, market: str, side: str, symbol: str | None, index: int | None) -> None:
        from ui.tv_chart import open_trade_viewer
        payload = paper_books.chart(market, side=side, symbol=symbol, index=index)
        if payload.get("error"):
            messagebox.showinfo("Chart", payload["error"], parent=self._top)
            return
        open_trade_viewer(self._top, payload)

    def _on_position_double_click(self, event) -> None:
        if self._pos_tree.identify_region(event.x, event.y) != "cell":
            return
        sel = self._pos_tree.selection()
        if not sel:
            return
        entry = self._pos_rows.get(sel[0])
        if entry is None:
            return
        market, symbol = entry
        self._open_chart(market, "open", symbol, None)

    def _on_closed_double_click(self, event) -> None:
        if self._closed_tree.identify_region(event.x, event.y) != "cell":
            return
        sel = self._closed_tree.selection()
        if not sel:
            return
        entry = self._closed_rows.get(sel[0])
        if entry is None:
            return
        market, index = entry
        self._open_chart(market, "closed", None, index)

    def _poll(self) -> None:
        if self._closed:
            return
        self._refresh_all()
        self._top.after(1000, self._poll)

    def _refresh_all(self) -> None:
        self._envelope = paper_books.snapshot_all()
        clocks = self._envelope.get("clocks") or {}
        books = self._envelope.get("books") or {}
        us = clocks.get("us") or {}
        ph = clocks.get("ph") or {}
        us_run = "●" if us.get("running") else "○"
        ph_run = "●" if ph.get("running") else "○"
        self._clock_us.set(
            f"US  {us.get('local_time', '—')} {us.get('tz_name', 'ET')}  ·  {us.get('session', '—')}  {us_run}"
        )
        self._clock_ph.set(
            f"PH  {ph.get('local_time', '—')} {ph.get('tz_name', 'PHT')}  ·  {ph.get('session', '—')}  {ph_run}"
        )
        for mid, frame in self._frames.items():
            frame.apply_snapshot(books.get(mid) or {})
        self._sort_note.configure(
            text="" if self._filter_id() else "P&L sort is per market — pick US or PH.",
        )
        self._refresh_tables()
        self._refresh_performance()

    def _refresh_tables(self) -> None:
        self._refresh_positions()
        self._refresh_closed()
        self._refresh_logs()

    def _refresh_positions(self) -> None:
        self._pos_tree.delete(*self._pos_tree.get_children())
        self._pos_rows = {}
        rows = []
        for book in self._visible_books():
            mkt = book.get("market")
            for p in book.get("positions") or []:
                rows.append({**p, "market": p.get("market") or mkt, "_sym": book.get("currency_symbol")})
        col, desc = self._pos_sort
        key_fn = {
            "market": lambda r: r.get("market") or "",
            "opened": lambda r: r.get("opened") or "",
            "symbol": lambda r: r.get("symbol") or "",
            "status": lambda r: r.get("status") or "",
            "action": lambda r: r.get("action") or "",
            "entry": lambda r: r.get("entry") or 0,
            "current": lambda r: r.get("current") or 0,
            "unrl_pct": lambda r: r.get("unrl_pct") or 0,
            "r": lambda r: r.get("r") if r.get("r") is not None else float("-inf"),
            "days": lambda r: r.get("days") or 0,
            "bars": lambda r: r.get("bars") if r.get("bars") is not None else -1,
            "value": lambda r: r.get("value") or 0,
            "mtm": lambda r: r.get("mtm") or 0,
            "port_pct": lambda r: r.get("port_pct") or 0,
            "pattern": lambda r: r.get("pattern") or "",
        }.get(col, lambda r: r.get("unrl_pct") or 0)
        rows.sort(key=key_fn, reverse=desc)
        for row in rows:
            mkt = row.get("market") or "us"
            item_id = self._pos_tree.insert(
                "", tk.END,
                values=(
                    mkt.upper(),
                    _stamp(row.get("opened")),
                    row.get("symbol"),
                    row.get("status"),
                    row.get("action"),
                    f"{float(row.get('entry') or 0):.2f}",
                    f"{float(row.get('current') or 0):.2f}",
                    f"{float(row.get('unrl_pct') or 0):+.2f}%",
                    f"{row['r']:+.2f}" if row.get("r") is not None else "-",
                    f"{float(row.get('days') or 0):.1f}",
                    "-" if row.get("bars") is None else str(row.get("bars")),
                    format_money(float(row.get("value") or 0), mkt),
                    format_money(float(row.get("mtm") or 0), mkt, signed=True),
                    f"{float(row.get('port_pct') or 0):.1f}%",
                    row.get("pattern"),
                ),
                tags=(_pnl_tag(float(row.get("unrl_pct") or 0)), f"book-{mkt}"),
            )
            self._pos_rows[item_id] = (mkt, row.get("symbol") or "")
        self._pos_tree.set_sort(*self._pos_sort)

    def _refresh_closed(self) -> None:
        self._closed_tree.delete(*self._closed_tree.get_children())
        self._closed_rows = {}
        rows = []
        for book in self._visible_books():
            mkt = book.get("market")
            for idx, t in enumerate(book.get("closed") or []):
                rows.append({**t, "market": t.get("market") or mkt, "_idx": idx, "_sym": book.get("currency_symbol")})
        col, desc = self._closed_sort
        if self._filter_id() is None and col in ("pnl_d", "pnl"):
            col = "closed"
        key_fn = {
            "market": lambda t: t.get("market") or "",
            "closed": lambda t: t.get("closed") or "",
            "symbol": lambda t: t.get("symbol") or "",
            "action": lambda t: t.get("action") or "",
            "qty": lambda t: t.get("qty") or 0,
            "entry": lambda t: t.get("entry") or 0,
            "exit": lambda t: t.get("exit") or 0,
            "pnl_d": lambda t: t.get("pnl") or 0,
            "pnl": lambda t: t.get("pnl_pct") or 0,
            "r": lambda t: t.get("r") if t.get("r") is not None else float("-inf"),
            "reason": lambda t: t.get("reason") or "",
            "hold": lambda t: t.get("days") or 0,
            "pattern": lambda t: t.get("pattern") or "",
        }.get(col, lambda t: t.get("closed") or "")
        rows.sort(key=key_fn, reverse=desc)
        self._closed_stats_var.set(
            f"{len(rows)} closed trades" if rows else "No closed trades yet. Start US, PH, or both."
        )
        for t in rows[:200]:
            mkt = t.get("market") or "us"
            dollars = float(t.get("pnl") or 0)
            r = t.get("r")
            item_id = self._closed_tree.insert(
                "", tk.END,
                values=(
                    mkt.upper(),
                    _stamp(t.get("closed")),
                    t.get("symbol"),
                    t.get("action"),
                    t.get("qty"),
                    f"{float(t.get('entry') or 0):.2f}",
                    f"{float(t.get('exit') or 0):.2f}",
                    format_money(dollars, mkt, signed=True),
                    f"{float(t.get('pnl_pct') or 0):+.2f}%",
                    f"{r:+.2f}" if r is not None else "—",
                    format_exit_reason(t.get("reason"), t.get("time_exit_bars_elapsed")),
                    format_hold(t.get("days"), t.get("bars")),
                    format_pattern_name(t.get("pattern")),
                ),
                tags=(_pnl_tag(dollars), f"book-{mkt}"),
            )
            self._closed_rows[item_id] = (mkt, int(t.get("_idx") or 0))
        self._closed_tree.set_sort(*self._closed_sort)

    def _refresh_logs(self) -> None:
        self._log_tree.delete(*self._log_tree.get_children())
        rows = []
        for book in self._visible_books():
            mkt = book.get("market")
            for row in book.get("signal_logs") or []:
                rows.append({**row, "market": row.get("market") or mkt})
        col, desc = self._log_sort
        key_fn = {
            "market": lambda r: r.get("market") or "",
            "time": lambda r: r.get("ts") or "",
            "sim_bar": lambda r: r.get("sim_bar") or "",
            "symbol": lambda r: r.get("symbol") or "",
            "timeframe": lambda r: r.get("timeframe") or "",
            "action": lambda r: r.get("action") or "",
            "pattern": lambda r: r.get("pattern") or "",
            "confidence": lambda r: float(r.get("confidence") or 0),
            "price": lambda r: float(r.get("price") or 0),
            "status": lambda r: r.get("status") or "",
            "reason": lambda r: r.get("reason") or "",
        }.get(col, lambda r: r.get("ts") or "")
        rows.sort(key=key_fn, reverse=desc)
        for idx, row in enumerate(rows):
            mkt = row.get("market") or "us"
            status = str(row.get("status") or "")
            conf = row.get("confidence")
            price = row.get("price")
            self._log_tree.insert(
                "", tk.END, iid=f"log-{idx}",
                values=(
                    mkt.upper(),
                    _stamp(row.get("ts")),
                    _stamp(row.get("sim_bar")) if row.get("sim_bar") else "—",
                    row.get("symbol") or "",
                    row.get("timeframe") or "",
                    row.get("action") or "",
                    row.get("pattern") or "",
                    f"{float(conf):.2f}" if conf is not None else "—",
                    f"{float(price):.2f}" if price is not None else "—",
                    status,
                    row.get("reason") or "",
                ),
                tags=(status, f"book-{mkt}"),
            )
        self._log_tree.set_sort(*self._log_sort)

    def _refresh_performance(self) -> None:
        books = self._envelope.get("books") or {}
        for mid in BOOK_IDS:
            snap = books.get(mid) or {}
            text = self._summary[mid]
            text.config(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.insert(tk.END, snap.get("summary") or "No closed trades yet.\n")
            text.config(state=tk.DISABLED)
            b64 = snap.get("equity_png_b64")
            lbl = self._chart_label[mid]
            if not b64:
                lbl.config(text="Not enough points for an equity curve.", image="")
                continue
            image = Image.open(io.BytesIO(base64.b64decode(b64)))
            photo = ImageTk.PhotoImage(image)
            self._photos[mid] = photo
            lbl.config(image=photo, text="")

    def _export_trades(self) -> None:
        filt = self._filter_id() or "all"
        try:
            payload = paper_books.export_trades(None if filt == "all" else filt)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        path = filedialog.asksaveasfilename(
            title="Export trades for LLM review",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile=(
                f"paper_trades_{filt}_"
                f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
            ),
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        messagebox.showinfo("Paper trading", f"Saved to {path}")

    def _on_close(self) -> None:
        if paper_books.any_running():
            if not messagebox.askyesno(
                "Paper trading running",
                "US and/or PH paper is active. Stop both and close?",
            ):
                return
            paper_books.stop_all()
        self._closed = True
        PaperDashboard._instance = None
        self._top.destroy()
