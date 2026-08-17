"""
ui/paper_dashboard.py — Paper trading dashboard for the tkinter UI.

Mirrors ui/backtest_dialog.py's shape, but for a long-running live session
instead of a one-shot run: Start/Stop, a live positions table (color-coded,
sortable, with unrealized P&L/R/risk/exposure), a closed trades table, a
performance summary + per-pattern breakdown (reusing BacktestResult — the
same stats the backtester already computes), and an equity curve chart.
Runs MarketScanner.run() (with a PaperAccount attached) in a background
thread with its own event loop.
"""

from __future__ import annotations

import asyncio
import csv
import io
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
import tkinter as tk
from typing import Optional

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from PIL import Image, ImageTk
from tkcalendar import DateEntry
import websockets

from config import settings, DISABLED_PATTERNS
from core.backtester import BacktestTrade
from core.market import default_market, format_money, get_market, session_label
from core.paper_trader import (
    PaperAccount, days_held, sim_days_held, bars_held, position_status,
    r_multiple, risk_dollars, unrealized_pct,
)
from core.scanner import MarketScanner
from data.stream_client import StreamClient
from data.tv_client import TVClient
from utils.logger import log

REPO_ROOT = Path(__file__).resolve().parent.parent

# Treeview tag -> foreground color, applied by _tag_for_pnl / fixed tags below.
COLOR_GAIN = "#1b7a1b"
COLOR_LOSS = "#c0392b"
COLOR_BUY = "#1b6fc0"
COLOR_SELL = "#c0392b"
COLOR_TRAILING = "#b8860b"
COLOR_BREAKEVEN = "#6a5acd"
COLOR_MUTED = "#666666"


def _pnl_tag(value: float) -> str:
    if value > 0:
        return "gain"
    if value < 0:
        return "loss"
    return "flat"


class _SortableTree(ttk.Treeview):
    """Treeview whose columns sort (toggling asc/desc) when the header is
    clicked; the actual sort key per column lives in the caller's
    _refresh_* method so it can sort by the underlying trade data, not the
    formatted cell text."""

    def __init__(self, master, columns: list[tuple[str, int, str]], on_sort, **kw):
        col_ids = [c[0] for c in columns]
        super().__init__(master, columns=col_ids, show="headings", **kw)
        for col_id, width, label in columns:
            self.heading(col_id, text=label, command=lambda c=col_id: on_sort(c))
            self.column(col_id, width=width)


class PaperDashboard:
    def __init__(self, master: tk.Widget):
        self._top = tk.Toplevel(master)
        self._top.title("Paper Trading")
        self._top.geometry("1180x640")
        self._top.protocol("WM_DELETE_WINDOW", self._on_close)

        self._account = PaperAccount.load(market=default_market().id)
        self._scanner: Optional[MarketScanner] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._closed = False
        self._photo = None  # keep a ref so PhotoImage isn't GC'd
        self._stream_proc: Optional[subprocess.Popen] = None  # server we auto-launched, if any

        self._pos_sort = ("unrl_pct", True)     # (column, descending)
        self._closed_sort = ("closed", True)

        top_bar = ttk.Frame(self._top, padding=(8, 6))
        top_bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top_bar, text="Market:").pack(side=tk.LEFT)
        self._market_var = tk.StringVar(value=default_market().id)
        self._market_combo = ttk.Combobox(
            top_bar, textvariable=self._market_var, values=["us", "ph"],
            state="readonly", width=6,
        )
        self._market_combo.pack(side=tk.LEFT, padx=(4, 12))
        self._market_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_market_change())

        ttk.Label(top_bar, text="Symbols:").pack(side=tk.LEFT)
        self._n_var = tk.IntVar(value=default_market().default_n_symbols)
        ttk.Spinbox(top_bar, from_=5, to=5000, increment=5, width=6, textvariable=self._n_var).pack(side=tk.LEFT, padx=(4, 8))

        ttk.Label(top_bar, text="Additional:").pack(side=tk.LEFT)
        self._extra_var = tk.StringVar(value="")
        ttk.Entry(top_bar, textvariable=self._extra_var, width=18).pack(side=tk.LEFT, padx=(4, 12))

        self._start_btn = ttk.Button(top_bar, text="Start", command=self._start)
        self._start_btn.pack(side=tk.LEFT)
        self._stop_btn = ttk.Button(top_bar, text="Stop", command=self._stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._reset_btn = ttk.Button(top_bar, text="Reset account", command=self._reset)
        self._reset_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._save_btn = ttk.Button(top_bar, text="Save results...", command=self._save_results)
        self._save_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._stream_var = tk.BooleanVar(value=False)
        self._stream_check = ttk.Checkbutton(
            top_bar, text="Use paper trade stream", variable=self._stream_var,
            command=self._on_stream_toggle,
        )
        self._stream_check.pack(side=tk.LEFT, padx=(12, 0))

        self._stream_start_frame = ttk.Frame(top_bar)
        ttk.Label(self._stream_start_frame, text="Start:").pack(side=tk.LEFT, padx=(8, 2))
        self._stream_start_picker = DateEntry(
            self._stream_start_frame,
            width=11,
            date_pattern="yyyy-mm-dd",
            **self._stream_start_date_kwargs(),
        )
        self._stream_start_picker.pack(side=tk.LEFT)

        self._kronos_gate_var = tk.BooleanVar(value=default_market().kronos_gate_default)
        self._kronos_check = ttk.Checkbutton(
            top_bar, text="Kronos 1w gate", variable=self._kronos_gate_var,
        )
        self._kronos_check.pack(side=tk.LEFT, padx=(12, 0))

        self._kronos_rank_var = tk.BooleanVar(value=default_market().kronos_rank_default)
        ttk.Checkbutton(
            top_bar, text="Kronos rank sleeve", variable=self._kronos_rank_var,
        ).pack(side=tk.LEFT, padx=(12, 0))

        self._volume_gate_var = tk.BooleanVar(value=settings.volume_gate_enabled)
        ttk.Checkbutton(
            top_bar, text="Volume gate", variable=self._volume_gate_var,
        ).pack(side=tk.LEFT, padx=(12, 0))

        equity_bar = ttk.Frame(self._top, padding=(8, 0))
        equity_bar.pack(side=tk.TOP, fill=tk.X)
        self._equity_var = tk.StringVar()
        ttk.Label(equity_bar, textvariable=self._equity_var, font=("TkDefaultFont", 11, "bold")).pack(side=tk.LEFT)

        exposure_bar = ttk.Frame(self._top, padding=(8, 0))
        exposure_bar.pack(side=tk.TOP, fill=tk.X)
        self._exposure_var = tk.StringVar()
        ttk.Label(exposure_bar, textvariable=self._exposure_var, foreground=COLOR_MUTED).pack(side=tk.LEFT)

        scan_bar = ttk.Frame(self._top, padding=(8, 0, 8, 6))
        scan_bar.pack(side=tk.TOP, fill=tk.X)
        self._scan_stats_var = tk.StringVar(value="Last scan: —   Patterns found: —   Trades opened: —   Rejected: —   Scan time: —")
        ttk.Label(scan_bar, textvariable=self._scan_stats_var, foreground=COLOR_MUTED).pack(side=tk.LEFT)

        status_bar = ttk.Frame(self._top, padding=(8, 0, 8, 6))
        status_bar.pack(side=tk.TOP, fill=tk.X)
        self._status_var = tk.StringVar(value="Stopped.")
        ttk.Label(status_bar, textvariable=self._status_var, foreground=COLOR_MUTED).pack(side=tk.LEFT)

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

    # ── Positions tab ─────────────────────────────────────────────────────
    def _build_positions_tab(self, parent: ttk.Frame) -> None:
        body = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        body.pack(fill=tk.BOTH, expand=True)

        pos_frame = ttk.LabelFrame(body, text="Open positions (click a header to sort, double-click a row for chart)")
        body.add(pos_frame, weight=1)
        pos_cols = [
            ("opened", 125, "Opened"), ("symbol", 65, "Symbol"), ("status", 85, "Status"),
            ("action", 55, "Action"), ("entry", 75, "Entry"), ("current", 75, "Current"),
            ("unrl_pct", 70, "Unrl %"), ("r", 50, "R"),
            ("days", 68, "Cal Days"), ("bars", 58, "Bars"),
            ("signal_bars", 75, "Signal Bars"),
            ("stop", 110, "Stop"), ("target", 110, "Target"),
            ("value", 85, "Value"), ("mtm", 85, "MTM"), ("port_pct", 60, "Port %"), ("risk", 70, "Risk"),
            ("pattern", 190, "Pattern"),
        ]
        self._pos_tree = self._add_scrollbar(pos_frame, _SortableTree(
            pos_frame, pos_cols, self._on_sort_positions, height=7,
        ))
        self._pos_tree.bind("<Double-1>", self._on_position_double_click)
        self._configure_color_tags(self._pos_tree)
        self._pos_rows: dict[str, tuple[str, BacktestTrade]] = {}

        closed_frame = ttk.LabelFrame(body, text="Closed trades (double-click a row for chart)")
        body.add(closed_frame, weight=2)
        closed_cols = [
            ("opened", 125, "Opened"), ("closed", 125, "Closed"),
            ("held", 68, "Cal Days"), ("bars", 58, "Held Bars"),
            ("stop_bars", 68, "Stop Bars"), ("symbol", 65, "Symbol"),
            ("action", 55, "Action"), ("entry", 75, "Entry"), ("exit", 75, "Exit"),
            ("pnl", 65, "P&L"), ("r", 50, "R"), ("reason", 100, "Reason"),
            ("pattern", 190, "Pattern"),
        ]
        self._closed_tree = self._add_scrollbar(closed_frame, _SortableTree(
            closed_frame, closed_cols, self._on_sort_closed, height=10,
        ))
        self._closed_tree.bind("<Double-1>", self._on_closed_double_click)
        self._configure_color_tags(self._closed_tree)
        self._closed_rows: dict[str, BacktestTrade] = {}

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
        tree.tag_configure("trailing", foreground=COLOR_TRAILING)
        tree.tag_configure("breakeven", foreground=COLOR_BREAKEVEN)

    # ── Logs tab ──────────────────────────────────────────────────────────
    def _build_logs_tab(self, parent: ttk.Frame) -> None:
        """Mirror the web Paper Logs tab: show the scanner's newest signal
        decisions, including the exact gate/rejection reason.

        The scanner keeps a thread-safe ring buffer, so this is safe to poll
        from Tk's main thread while the asyncio scanner runs in its worker
        thread.  Keep the same columns/order as web/templates/paper.html so
        the two UIs tell the same story.
        """
        frame = ttk.LabelFrame(parent, text="Signal log")
        frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        filters = ttk.Frame(frame)
        filters.pack(fill=tk.X, padx=6, pady=(4, 2))
        ttk.Label(filters, text="Symbol").pack(side=tk.LEFT)
        self._log_symbol_var = tk.StringVar()
        ttk.Entry(filters, textvariable=self._log_symbol_var, width=10).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(filters, text="Pattern").pack(side=tk.LEFT)
        self._log_pattern_var = tk.StringVar()
        ttk.Entry(filters, textvariable=self._log_pattern_var, width=24).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(filters, text="Status").pack(side=tk.LEFT)
        self._log_status_var = tk.StringVar(value="ALL")
        ttk.Combobox(
            filters, textvariable=self._log_status_var,
            values=["ALL", "accepted", "filled", "rejected"],
            state="readonly", width=10,
        ).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(filters, text="Search").pack(side=tk.LEFT)
        self._log_search_var = tk.StringVar()
        ttk.Entry(filters, textvariable=self._log_search_var, width=28).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(filters, text="Clear", command=self._clear_log_filters).pack(side=tk.LEFT)

        ttk.Label(
            frame,
            text="Detected pattern signals with accept/reject outcome and reason.",
            foreground=COLOR_MUTED,
        ).pack(anchor=tk.W, padx=6, pady=(4, 2))

        log_cols = [
            ("time", 145, "Time"),
            ("sim_bar", 105, "Sim Bar"),
            ("symbol", 70, "Symbol"),
            ("timeframe", 55, "TF"),
            ("action", 60, "Action"),
            ("pattern", 205, "Pattern"),
            ("confidence", 60, "Conf"),
            ("price", 80, "Price"),
            ("status", 85, "Status"),
            ("reason", 520, "Reason"),
        ]
        self._log_tree = _SortableTree(
            frame, log_cols, self._on_sort_logs, height=24,
        )
        self._log_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._log_tree.configure(show="headings")
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._log_tree.yview)
        self._log_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=self._log_tree.xview)
        self._log_tree.configure(xscrollcommand=hsb.set)
        hsb.pack(fill=tk.X, padx=4, pady=(0, 4))

        self._log_tree.tag_configure("accepted", foreground=COLOR_GAIN)
        self._log_tree.tag_configure("filled", foreground=COLOR_BUY)
        self._log_tree.tag_configure("rejected", foreground=COLOR_LOSS)
        self._log_rows: dict[str, dict] = {}
        self._log_sort = ("time", True)
        for var in (
            self._log_symbol_var, self._log_pattern_var,
            self._log_status_var, self._log_search_var,
        ):
            var.trace_add("write", lambda *_args: self._refresh_logs())

    # ── Performance tab ───────────────────────────────────────────────────
    def _build_performance_tab(self, parent: ttk.Frame) -> None:
        body = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(body)
        body.add(left, weight=1)

        summary_frame = ttk.LabelFrame(left, text="Summary")
        summary_frame.pack(fill=tk.X, padx=4, pady=4)
        self._summary_text = tk.Text(summary_frame, height=14, width=42, state=tk.DISABLED, font=("TkFixedFont", 9))
        self._summary_text.pack(fill=tk.BOTH, expand=True)

        pattern_frame = ttk.LabelFrame(left, text="By pattern")
        pattern_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._pattern_tree = ttk.Treeview(
            pattern_frame,
            columns=("pattern", "trades", "win_pct", "avg_r", "avg_pnl", "pf", "hold", "max_dd"),
            show="headings", height=8,
        )
        pat_cols = [
            ("pattern", 190, "Pattern"), ("trades", 50, "Trades"), ("win_pct", 55, "Win %"),
            ("avg_r", 55, "Avg R"), ("avg_pnl", 65, "Avg %"), ("pf", 50, "PF"),
            ("hold", 60, "Hold(d)"), ("max_dd", 65, "Max DD"),
        ]
        for col, w, label in pat_cols:
            self._pattern_tree.heading(col, text=label)
            self._pattern_tree.column(col, width=w)
        self._pattern_tree.pack(fill=tk.BOTH, expand=True)
        self._configure_color_tags(self._pattern_tree)

        right = ttk.LabelFrame(body, text="Equity curve")
        body.add(right, weight=2)
        self._equity_chart_label = ttk.Label(right)
        self._equity_chart_label.pack(fill=tk.BOTH, expand=True)

    # ── Start / stop ────────────────────────────────────────────────────
    @staticmethod
    def _stream_start_date_kwargs() -> dict:
        """Default date for the stream start picker (config, else ~1y ago)."""
        default = date.today() - timedelta(days=365)
        raw = settings.papertrade_stream_start_date
        if raw:
            try:
                default = date.fromisoformat(raw.strip())
            except ValueError:
                pass
        return {"year": default.year, "month": default.month, "day": default.day}

    def _on_stream_toggle(self) -> None:
        if self._stream_var.get():
            self._stream_start_frame.pack(
                side=tk.LEFT, before=self._kronos_check,
            )
        else:
            self._stream_start_frame.pack_forget()

    def _on_market_change(self) -> None:
        if self._running:
            return
        profile = get_market(self._market_var.get())
        self._n_var.set(profile.default_n_symbols)
        self._kronos_gate_var.set(profile.kronos_gate_default)
        self._kronos_rank_var.set(profile.kronos_rank_default)
        self._account = PaperAccount.load(market=profile.id)
        self._refresh_all()

    def _start(self) -> None:
        if self._running:
            return
        self._running = True
        self._start_btn.config(state=tk.DISABLED)
        self._stop_btn.config(state=tk.NORMAL)
        try:
            self._market_combo.configure(state=tk.DISABLED)
        except Exception:
            pass
        self._status_var.set("Fetching symbols...")
        stream_start = None
        if self._stream_var.get():
            stream_start = self._stream_start_picker.get_date().isoformat()
        threading.Thread(
            target=self._run_thread,
            args=(
                int(self._n_var.get()),
                self._extra_var.get(),
                self._stream_var.get(),
                self._kronos_gate_var.get(),
                self._kronos_rank_var.get(),
                self._volume_gate_var.get(),
                stream_start,
                self._market_var.get(),
            ),
            daemon=True,
        ).start()

    @staticmethod
    def _port_open(host: str, port: int) -> bool:
        # A bare TCP connect+close (no WebSocket handshake) makes the
        # websockets server log a scary "opening handshake failed"
        # traceback for what is actually a harmless health check — do a
        # real (tiny) handshake instead so the server sees a clean connect.
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
        """Force the stream server's port free before (re)launching.

        A port-open check alone can't tell "healthy server" from "stale
        process left over from a previous UI session, still running
        pre-fix code and stuck serving corrupted replay data" — both look
        identical from the outside. Always killing and relaunching removes
        that ambiguity entirely.
        """
        try:
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    def _ensure_stream_server(self, start_date: Optional[str] = None) -> Optional[str]:
        """(Re)launch `main.py --papertrade-stream` fresh every time, so a
        stale/outdated server process is never silently reused. Returns an
        error message on failure, else None."""
        host, port = settings.papertrade_stream_host, settings.papertrade_stream_port
        self._kill_whatever_is_on(port)
        time.sleep(0.3)  # let the kernel release the port before rebinding
        status = "Starting paper trade stream server..."
        if start_date:
            status = f"Starting paper trade stream server (from {start_date})..."
        self._top.after(0, lambda s=status: self._status_var.set(s))
        cmd = [sys.executable, "main.py", "--papertrade-stream"]
        if start_date:
            cmd.extend(["--papertrade-stream-start", start_date])
        self._stream_proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
        )
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
        volume_gate: bool,
        stream_start: Optional[str] = None,
        market: str = "us",
    ) -> None:
        data_feed = None
        profile = get_market(market)
        # Load the ledger before launching a historical stream so a restarted
        # paper session can resume after the last simulated bar it actually
        # processed. The old server always restarted at the configured
        # stream_start date, which reset market data while the account kept its
        # old positions/equity.
        self._account = PaperAccount.load(market=profile.id)
        effective_stream_start = stream_start
        if use_stream and self._account.sim_now() is not None:
            resume_from = self._account.sim_now()
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
                    # Start on/after the last processed session. The scanner's
                    # persisted bar identity will reject a duplicate last bar,
                    # then the stream advances to the next available bar.
                    effective_stream_start = resume_date.isoformat()

        if use_stream:
            error = self._ensure_stream_server(start_date=effective_stream_start)
            if error:
                self._top.after(0, lambda m=error: self._finish(m))
                return
            data_feed = StreamClient()

        symbol_rows = TVClient.fetch_universe_cached(
            n_symbols, profile.id, extra_symbols=extra_symbols,
        )
        if not symbol_rows:
            self._top.after(0, lambda: self._finish("No symbols from screener or additional list."))
            return
        symbols = [s for s, _ex in symbol_rows]
        exchange_overrides = dict(symbol_rows)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        scanner = MarketScanner(
            symbols=symbols,
            exchange_overrides=exchange_overrides,
            paper_account=self._account,
            disabled_patterns=DISABLED_PATTERNS,
            data_feed=data_feed,
            scan_interval_seconds=(
                settings.papertrade_stream_interval_seconds if use_stream else profile.scan_interval_seconds
            ),
            kronos_gate=kronos_gate,
            kronos_rank=kronos_rank,
            volume_gate=volume_gate,
            market=profile.id,
        )
        self._scanner = scanner
        self._task = loop.create_task(scanner.run())
        interval = settings.papertrade_stream_interval_seconds if use_stream else profile.scan_interval_seconds
        stream_note = (
            f", stream from {effective_stream_start}"
            if use_stream and effective_stream_start else ""
        )
        self._top.after(0, lambda: self._status_var.set(
            f"Running — {profile.label}, {len(symbols)} symbols, scanning every {interval}s"
            f"{stream_note}"
            f", Kronos gate={'ON' if kronos_gate else 'OFF'}"
            f", Kronos rank={'ON' if kronos_rank else 'OFF'}"
            f", Volume gate={'ON' if volume_gate else 'OFF'}"
            f", session={session_label(profile.id)}"
        ))
        error_msg: Optional[str] = None
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            root = exc
            while getattr(root, "exceptions", None):
                root = root.exceptions[0]
            error_msg = f"Crashed: {root}"
            log.error(f"Paper UI | scanner crashed: {root}", exc_info=root)
        finally:
            self._account.save()
            loop.close()
            self._top.after(0, lambda m=error_msg: self._finish(m))

    def _stop(self) -> None:
        if not self._running or self._loop is None or self._task is None:
            return
        self._status_var.set("Stopping...")
        self._loop.call_soon_threadsafe(self._task.cancel)

    def _finish(self, error: Optional[str]) -> None:
        self._running = False
        self._start_btn.config(state=tk.NORMAL)
        self._stop_btn.config(state=tk.DISABLED)
        try:
            self._market_combo.configure(state="readonly")
        except Exception:
            pass
        self._status_var.set(error or "Stopped.")

    def _reset(self) -> None:
        if self._running:
            messagebox.showinfo("Paper trading", "Stop the session before resetting.")
            return
        if not messagebox.askyesno("Reset account", "Wipe the paper trading account and start fresh?"):
            return
        self._account = PaperAccount(market=get_market(self._market_var.get()).id)
        self._account.save()
        self._refresh_all()

    def _save_results(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save paper trading results",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"paper_trading_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
        )
        if not path:
            return

        columns = [
            "status", "symbol", "pattern", "timeframe", "action",
            "entry_date", "exit_date", "entry_price", "exit_price",
            "pnl_pct", "r", "days_held", "exit_reason", "confidence", "qty",
        ]
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            result = self._account.to_result()
            pf = result.profit_factor
            writer.writerow(["# Cash", f"{self._account.cash:.2f}"])
            writer.writerow(["# Equity", f"{self._account.equity():.2f}"])
            writer.writerow(["# Closed trades", len(result.trades)])
            writer.writerow(["# Win rate", f"{result.win_rate:.1%}"])
            writer.writerow(["# Profit factor", "inf" if pf == float("inf") else f"{pf:.2f}"])
            writer.writerow([])
            writer.writerow(columns)

            now = datetime.now(timezone.utc)
            for sym, p in self._account.positions_snapshot():
                current = self._account.last_price(sym, p.entry_price)
                writer.writerow([
                    "OPEN", sym, p.pattern, p.timeframe, p.action,
                    p.entry_date.isoformat(), "", f"{p.entry_price:.4f}", f"{current:.4f}",
                    f"{unrealized_pct(p, current):.4f}",
                    f"{r_multiple(p, current):.4f}" if r_multiple(p, current) is not None else "",
                    f"{days_held(p, now):.2f}", "", f"{p.confidence:.4f}", p.qty,
                ])
            for t in self._account.closed_snapshot():
                r = r_multiple(t, t.exit_price)
                writer.writerow([
                    "CLOSED", t.symbol, t.pattern, t.timeframe, t.action,
                    t.entry_date.isoformat(), t.exit_date.isoformat(),
                    f"{t.entry_price:.4f}", f"{t.exit_price:.4f}", f"{t.pnl_pct:.4f}",
                    f"{r:.4f}" if r is not None else "",
                    f"{days_held(t):.2f}", t.exit_reason, f"{t.confidence:.4f}", t.qty,
                ])

        messagebox.showinfo("Paper trading", f"Saved to {path}")

    # ── Sorting ───────────────────────────────────────────────────────────
    def _on_sort_positions(self, col: str) -> None:
        cur_col, cur_desc = self._pos_sort
        self._pos_sort = (col, not cur_desc if col == cur_col else True)
        self._refresh_positions()

    def _on_sort_closed(self, col: str) -> None:
        cur_col, cur_desc = self._closed_sort
        self._closed_sort = (col, not cur_desc if col == cur_col else True)
        self._refresh_closed()

    def _on_sort_logs(self, col: str) -> None:
        cur_col, cur_desc = self._log_sort
        self._log_sort = (col, not cur_desc if col == cur_col else True)
        self._refresh_logs()

    def _clear_log_filters(self) -> None:
        self._log_symbol_var.set("")
        self._log_pattern_var.set("")
        self._log_status_var.set("ALL")
        self._log_search_var.set("")

    # ── Row detail popups ─────────────────────────────────────────────────
    def _show_trade_details(self, t: BacktestTrade, current_price: Optional[float]) -> None:
        from analysis.chart_renderer import build_trade_viewer_payload
        from data.db import load_daily_ohlcv_df
        from ui.tv_chart import open_trade_viewer

        timeframe = t.timeframe or "1d"
        df = None
        if self._scanner is not None:
            df = self._scanner.ohlcv_frame(t.symbol, timeframe, min_bars=2)
        if df is None or len(df) < 2:
            df = load_daily_ohlcv_df(t.symbol)
        if df is None or len(df) < 2:
            messagebox.showinfo(
                "Chart",
                f"No OHLCV available for {t.symbol} {timeframe}.",
                parent=self._top,
            )
            return
        payload = build_trade_viewer_payload(
            df,
            symbol=t.symbol,
            timeframe=timeframe,
            pattern=t.pattern,
            action=t.action,
            session_tz=get_market(self._account.market).session_tz,
            entry=t.entry_price,
            stop=t.stop_loss,
            target=t.take_profit,
            exit_price=t.exit_price if current_price is None else None,
            exit_reason=t.exit_reason if current_price is None else None,
            current=current_price,
            entry_time=t.sim_entry_date or t.entry_date,
            exit_time=None if current_price is not None else (t.sim_exit_date or t.exit_date),
        )
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
        sym, t = entry
        self._show_trade_details(t, self._account.last_price(sym, t.entry_price))

    def _on_closed_double_click(self, event) -> None:
        if self._closed_tree.identify_region(event.x, event.y) != "cell":
            return
        sel = self._closed_tree.selection()
        if not sel:
            return
        t = self._closed_rows.get(sel[0])
        if t is None:
            return
        self._show_trade_details(t, None)

    # ── Polling / refresh ─────────────────────────────────────────────────
    def _poll(self) -> None:
        if self._closed:
            return
        self._refresh_all()
        self._top.after(1000, self._poll)

    def _refresh_all(self) -> None:
        self._refresh_header()
        self._refresh_positions()
        self._refresh_closed()
        self._refresh_logs()
        self._refresh_performance()

    def _refresh_header(self) -> None:
        mkt = self._account.market
        equity = self._account.equity()
        total_pnl = equity - self._account.initial_capital
        self._equity_var.set(
            f"Market: {get_market(mkt).label}   "
            f"Cash: {format_money(self._account.cash, mkt)}   "
            f"Equity: {format_money(equity, mkt)}   "
            f"Total P&L: {format_money(total_pnl, mkt, signed=True)} "
            f"({total_pnl / self._account.initial_capital * 100:+.2f}%)   "
            f"Open: {len(self._account.positions)}   Closed: {len(self._account.closed)}"
            f"   Session: {session_label(mkt)}"
        )
        exp = self._account.exposure()
        realized = self._account.realized_pnl_dollars()
        unrealized = self._account.unrealized_pnl_dollars()
        self._exposure_var.set(
            f"Long: {exp['long_pct']:.1f}%   Short: {exp['short_pct']:.1f}%   "
            f"Net: {exp['net_pct']:+.1f}%   Gross: {exp.get('gross_pct', 0):.1f}%   "
            f"Realized: {format_money(realized, mkt, signed=True)}   "
            f"Unrealized: {format_money(unrealized, mkt, signed=True)}"
        )
        stats = self._scanner.stats if self._scanner is not None else None
        if stats is None:
            self._scan_stats_var.set("Last scan: —   Patterns found: —   Trades opened: —   Rejected: —   Scan time: —")
            return
        last = stats["last_scan_at"]
        last_str = "—" if not last else datetime.fromisoformat(last).strftime("%H:%M:%S")
        sim_days_str = f"   Sim days: {stats['sim_days']}" if self._stream_var.get() else ""
        rejection = stats.get("rejection_by_gate") or {}
        rejection_str = ", ".join(
            f"{k}={v}" for k, v in sorted(
                rejection.items(), key=lambda kv: (-kv[1], kv[0])
            )[:4]
        ) or "none"
        self._scan_stats_var.set(
            f"Last scan: {last_str}   Patterns found: {stats['patterns_found']}   "
            f"Trades opened: {stats['trades_opened']}   Rejected: {stats['signals_rejected']}   "
            f"Vol reject: {stats.get('volume_gate_rejected', 0)}   "
            f"Data: {stats.get('symbols_with_snapshot', 0)}/{stats.get('symbols_total', 0)} symbols   "
            f"New bars: {stats.get('new_bars', 0)}   "
            f"Pattern evals: {stats.get('pattern_evaluations', 0)}   "
            f"Replay skew: {stats.get('daily_date_skew', 0)}   "
            f"Scan time: {stats['scan_duration_s']:.1f}s{sim_days_str}   "
            f"Reject gates: {rejection_str}"
        )

    def _refresh_positions(self) -> None:
        self._pos_tree.delete(*self._pos_tree.get_children())
        self._pos_rows = {}
        sim_now = self._account.sim_now() or datetime.now(timezone.utc)
        equity = self._account.equity()
        mkt = self._account.market

        rows = []
        for sym, p in self._account.positions_snapshot():
            current = self._account.last_price(sym, p.entry_price)
            r = r_multiple(p, current)
            risk = risk_dollars(p)
            value = current * p.qty
            mtm = (current - p.entry_price) * p.qty if p.action == "BUY" else (p.entry_price - current) * p.qty
            port_pct = (value / equity * 100) if equity > 0 else 0.0
            stop_dist = (p.stop_loss - current) / current * 100 if p.stop_loss else None
            target_dist = (p.take_profit - current) / current * 100 if p.take_profit else None
            rows.append({
                "sym": sym, "p": p, "current": current, "r": r, "risk": risk,
                "value": value, "mtm": mtm, "port_pct": port_pct,
                "stop_dist": stop_dist, "target_dist": target_dist,
                "unrl": unrealized_pct(p, current),
                "days": sim_days_held(
                    p,
                    self._account.sim_now(p.timeframe) or sim_now,
                ),
                "bars": bars_held(
                    p,
                    self._account.bar_count(sym, p.timeframe),
                ),
                "signal_bars": (
                    self._account.bar_count(sym, p.timeframe) - p.neckline_break_bar_idx
                    if p.neckline_break_bar_idx is not None else None
                ),
            })

        sort_col, desc = self._pos_sort
        sort_key = {
            "opened": lambda r: r["p"].entry_date,
            "symbol": lambda r: r["sym"],
            "status": lambda r: position_status(r["p"]),
            "action": lambda r: r["p"].action,
            "entry": lambda r: r["p"].entry_price,
            "current": lambda r: r["current"],
            "unrl_pct": lambda r: r["unrl"],
            "r": lambda r: r["r"] if r["r"] is not None else float("-inf"),
            "days": lambda r: r["days"],
            "bars": lambda r: r["bars"] if r["bars"] is not None else -1,
            "signal_bars": lambda r: r["signal_bars"] if r["signal_bars"] is not None else -1,
            "stop": lambda r: r["stop_dist"] if r["stop_dist"] is not None else float("inf"),
            "target": lambda r: r["target_dist"] if r["target_dist"] is not None else float("-inf"),
            "value": lambda r: r["value"],
            "mtm": lambda r: r["mtm"],
            "port_pct": lambda r: r["port_pct"],
            "risk": lambda r: r["risk"] if r["risk"] is not None else 0.0,
            "pattern": lambda r: r["p"].pattern,
        }.get(sort_col, lambda r: r["unrl"])
        rows.sort(key=sort_key, reverse=desc)

        for row in rows:
            sym, p, current, r = row["sym"], row["p"], row["current"], row["r"]
            status = position_status(p)
            status_tag = {"TRAILING": "trailing", "BREAKEVEN": "breakeven"}.get(status, "flat")
            action_tag = "buy" if p.action == "BUY" else "sell"
            stop_str = f"{p.stop_loss:.2f} ({row['stop_dist']:+.1f}%)" if p.stop_loss else "-"
            target_str = f"{p.take_profit:.2f} ({row['target_dist']:+.1f}%)" if p.take_profit else "-"
            item_id = self._pos_tree.insert(
                "", tk.END,
                values=(
                    p.entry_date.strftime("%Y-%m-%d %H:%M:%S"), sym, status, p.action,
                    f"{p.entry_price:.2f}", f"{current:.2f}", f"{row['unrl']:+.2f}%",
                    f"{r:+.2f}" if r is not None else "-", f"{row['days']:.1f}",
                    str(row["bars"]) if row["bars"] is not None else "-",
                    str(row["signal_bars"]) if row["signal_bars"] is not None else "-",
                    stop_str, target_str,
                    format_money(row["value"], mkt, signed=False),
                    format_money(row["mtm"], mkt, signed=True),
                    f"{row['port_pct']:.1f}%",
                    format_money(row["risk"], mkt) if row["risk"] is not None else "-",
                    p.pattern,
                ),
                tags=(_pnl_tag(row["unrl"]), status_tag, action_tag),
            )
            self._pos_rows[item_id] = (sym, p)

    def _refresh_closed(self) -> None:
        self._closed_tree.delete(*self._closed_tree.get_children())
        self._closed_rows = {}

        sort_col, desc = self._closed_sort
        sort_key = {
            "opened": lambda t: t.entry_date,
            "closed": lambda t: t.exit_date,
            "held": lambda t: days_held(t),
            "bars": lambda t: bars_held(t) if bars_held(t) is not None else -1,
            "stop_bars": lambda t: (
                t.time_exit_bars_elapsed
                if t.time_exit_bars_elapsed is not None else -1
            ),
            "symbol": lambda t: t.symbol,
            "action": lambda t: t.action,
            "entry": lambda t: t.entry_price,
            "exit": lambda t: t.exit_price,
            "pnl": lambda t: t.pnl_pct,
            "r": lambda t: (r_multiple(t, t.exit_price) or float("-inf")),
            "reason": lambda t: t.exit_reason,
            "pattern": lambda t: t.pattern,
        }.get(sort_col, lambda t: t.exit_date)
        trades = sorted(self._account.closed_snapshot(), key=sort_key, reverse=desc)[:200]

        for t in trades:
            r = r_multiple(t, t.exit_price)
            held_days = days_held(t)
            held_str = f"{held_days:.1f}d" if held_days >= 1 else f"{held_days * 24:.1f}h"
            action_tag = "buy" if t.action == "BUY" else "sell"
            item_id = self._closed_tree.insert(
                "", tk.END,
                values=(
                    t.entry_date.strftime("%Y-%m-%d %H:%M:%S"),
                    t.exit_date.strftime("%Y-%m-%d %H:%M:%S"),
                    held_str,
                    str(bars_held(t)) if bars_held(t) is not None else "-",
                    (
                        str(t.time_exit_bars_elapsed)
                        if t.time_exit_bars_elapsed is not None
                        else "-"
                    ),
                    t.symbol, t.action,
                    f"{t.entry_price:.2f}", f"{t.exit_price:.2f}",
                    f"{t.pnl_pct:+.2f}%",
                    f"{r:+.2f}" if r is not None else "-",
                    t.exit_reason, t.pattern,
                ),
                tags=(_pnl_tag(t.pnl_pct), action_tag),
            )
            self._closed_rows[item_id] = t

    def _refresh_logs(self) -> None:
        """Refresh the Logs tab from the same scanner signal buffer used by
        the --web Paper Logs tab. Newest entries are shown first, matching
        web/static/app.js."""
        self._log_tree.delete(*self._log_tree.get_children())
        self._log_rows = {}

        if self._scanner is None:
            return

        rows = list(reversed(self._scanner.signal_log_snapshot()))
        symbol_filter = self._log_symbol_var.get().strip().lower()
        pattern_filter = self._log_pattern_var.get().strip().lower()
        status_filter = self._log_status_var.get().strip().lower()
        search_filter = self._log_search_var.get().strip().lower()
        if symbol_filter or pattern_filter or status_filter != "all" or search_filter:
            filtered = []
            for row in rows:
                if symbol_filter and symbol_filter not in str(row.get("symbol") or "").lower():
                    continue
                if pattern_filter and pattern_filter not in str(row.get("pattern") or "").lower():
                    continue
                if status_filter != "all" and str(row.get("status") or "").lower() != status_filter:
                    continue
                if search_filter:
                    haystack = " ".join(
                        str(row.get(k) or "") for k in
                        ("symbol", "timeframe", "action", "pattern", "status", "reason")
                    ).lower()
                    if search_filter not in haystack:
                        continue
                filtered.append(row)
            rows = filtered
        sort_col, desc = self._log_sort

        def _ts(row: dict) -> datetime:
            raw = row.get("ts")
            if not raw:
                return datetime.min.replace(tzinfo=timezone.utc)
            try:
                return datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                return datetime.min.replace(tzinfo=timezone.utc)

        def _sim_ts(row: dict) -> datetime | None:
            raw = row.get("sim_bar")
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                return None

        def _numeric(row: dict, key: str) -> float:
            value = row.get(key)
            try:
                return float(value)
            except (TypeError, ValueError):
                return float("-inf")

        sort_key = {
            "time": _ts,
            "sim_bar": lambda r: _sim_ts(r) or datetime.min.replace(tzinfo=timezone.utc),
            "symbol": lambda r: str(r.get("symbol") or ""),
            "timeframe": lambda r: str(r.get("timeframe") or ""),
            "action": lambda r: str(r.get("action") or ""),
            "pattern": lambda r: str(r.get("pattern") or ""),
            "confidence": lambda r: _numeric(r, "confidence"),
            "price": lambda r: _numeric(r, "price"),
            "status": lambda r: str(r.get("status") or ""),
            "reason": lambda r: str(r.get("reason") or ""),
        }.get(sort_col, _ts)
        rows.sort(key=sort_key, reverse=desc)

        for idx, row in enumerate(rows):
            ts = _ts(row)
            ts_str = ts.astimezone().strftime("%Y-%m-%d %H:%M:%S") if ts != datetime.min.replace(tzinfo=timezone.utc) else "—"
            confidence = row.get("confidence")
            price = row.get("price")
            status = str(row.get("status") or "")
            values = (
                ts_str,
                (
                    _sim_ts(row).astimezone().strftime("%Y-%m-%d")
                    if _sim_ts(row) is not None else "—"
                ),
                row.get("symbol") or "",
                row.get("timeframe") or "",
                row.get("action") or "",
                row.get("pattern") or "",
                f"{float(confidence):.2f}" if confidence is not None else "—",
                f"{float(price):.2f}" if price is not None else "—",
                status,
                row.get("reason") or "",
            )
            item_id = self._log_tree.insert(
                "", tk.END, iid=f"log-{idx}", values=values,
                tags=(status,) if status in {"accepted", "filled", "rejected"} else (),
            )
            self._log_rows[item_id] = row

    # ── Performance tab ───────────────────────────────────────────────────
    def _refresh_performance(self) -> None:
        result = self._account.to_result()
        self._summary_text.config(state=tk.NORMAL)
        self._summary_text.delete("1.0", tk.END)
        if not result.trades:
            self._summary_text.insert(tk.END, "No closed trades yet.\n")
        else:
            pf = result.profit_factor
            pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
            total_pnl = self._account.equity() - result.initial_capital
            realized = self._account.realized_pnl_dollars()
            unrealized = self._account.unrealized_pnl_dollars()
            exits = ", ".join(
                f"{reason}={count}" for reason, count in result.exit_reason_breakdown.items()
            ) or "—"
            self._summary_text.insert(tk.END, (
                f"Equity:         {format_money(self._account.equity(), self._account.market)}\n"
                f"Total P&L:      {format_money(total_pnl, self._account.market, signed=True)} "
                f"({(total_pnl / result.initial_capital * 100):+.2f}%)\n"
                f"Realized P&L:   {format_money(realized, self._account.market, signed=True)}\n"
                f"Unrealized P&L: {format_money(unrealized, self._account.market, signed=True)}\n"
                f"Trades:         {len(result.trades)}\n"
                f"Win rate:       {result.win_rate:.1%}\n"
                f"Avg winner:     {result.avg_win_pct:+.2f}%\n"
                f"Avg loser:      {result.avg_loss_pct:+.2f}%\n"
                f"Profit factor:  {pf_str}\n"
                f"Expectancy:     {result.expectancy_pct:+.2f}%/trade\n"
                f"Avg R / Median: {result.avg_r:+.2f} / {result.median_r:+.2f}\n"
                f"Avg hold:       {result.avg_hold_bars:.1f} bars\n"
                f"Max drawdown:   {result.max_drawdown_pct:+.2f}%\n"
                f"Sharpe:         {result.sharpe_ratio:.2f}\n"
                f"Exit reasons:   {exits}\n"
            ))
        self._summary_text.config(state=tk.DISABLED)

        self._pattern_tree.delete(*self._pattern_tree.get_children())
        for pattern, s in result.pattern_breakdown().items():
            pf_str = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "inf"
            avg_r_str = f"{s['avg_r']:+.2f}" if s["avg_r"] is not None else "-"
            self._pattern_tree.insert(
                "", tk.END,
                values=(
                    pattern, s["trades"], f"{s['win_rate']:.0%}", avg_r_str,
                    f"{s['avg_pnl_pct']:+.2f}%", pf_str,
                    f"{s['avg_hold_days']:.1f}", f"{s['max_drawdown_pct']:+.2f}%",
                ),
                tags=(_pnl_tag(s["avg_pnl_pct"]),),
            )

        self._refresh_equity_chart()

    def _refresh_equity_chart(self) -> None:
        curve = self._account.equity_curve_snapshot()
        if len(curve) < 2:
            self._equity_chart_label.config(text="Not enough closed trades yet for an equity curve.", image="")
            return
        xs = list(range(len(curve)))
        ys = [pt[1] for pt in curve]
        fig, ax = plt.subplots(figsize=(5, 3.4), dpi=100)
        ax.plot(xs, ys, color="#2962ff", linewidth=1.5)
        ax.axhline(self._account.initial_capital, color="#888", linestyle="--", linewidth=0.8)
        ax.set_title("Account equity", fontsize=10)
        ax.set_xlabel("Market session", fontsize=8)
        ax.tick_params(labelsize=7)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)
        image = Image.open(buf)
        self._photo = ImageTk.PhotoImage(image)
        self._equity_chart_label.config(image=self._photo, text="")

    # ── Lifecycle ────────────────────────────────────────────────────────
    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno("Paper trading running", "A paper trading session is active. Stop and close?"):
                return
            self._stop()
        self._closed = True
        self._account.save()
        if self._stream_proc is not None and self._stream_proc.poll() is None:
            self._stream_proc.terminate()
        self._top.destroy()
