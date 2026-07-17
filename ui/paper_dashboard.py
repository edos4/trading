"""
ui/paper_dashboard.py — Paper trading dashboard for the tkinter UI.

Mirrors ui/backtest_dialog.py's shape, but for a long-running live session
instead of a one-shot run: Start/Stop, a live positions table, a closed
trades table, a performance summary + per-pattern breakdown (reusing
BacktestResult — the same stats the backtester already computes), and an
equity curve chart. Runs MarketScanner.run() (with a PaperAccount attached)
in a background thread with its own event loop.
"""

from __future__ import annotations

import asyncio
import io
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import ttk, messagebox
from typing import Optional

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from PIL import Image, ImageTk

from config import settings
from core.paper_trader import PaperAccount, days_held, r_multiple, risk_dollars, unrealized_pct
from core.scanner import MarketScanner
from data.tv_client import TVClient
from utils.logger import log


class PaperDashboard:
    def __init__(self, master: tk.Widget):
        self._top = tk.Toplevel(master)
        self._top.title("Paper Trading")
        self._top.geometry("980x620")
        self._top.protocol("WM_DELETE_WINDOW", self._on_close)

        self._account = PaperAccount.load()
        self._scanner: Optional[MarketScanner] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._closed = False
        self._photo = None  # keep a ref so PhotoImage isn't GC'd

        top_bar = ttk.Frame(self._top, padding=(8, 6))
        top_bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top_bar, text="Symbols:").pack(side=tk.LEFT)
        self._n_var = tk.IntVar(value=100)
        ttk.Spinbox(top_bar, from_=5, to=500, increment=5, width=6, textvariable=self._n_var).pack(side=tk.LEFT, padx=(4, 12))

        self._start_btn = ttk.Button(top_bar, text="Start", command=self._start)
        self._start_btn.pack(side=tk.LEFT)
        self._stop_btn = ttk.Button(top_bar, text="Stop", command=self._stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._reset_btn = ttk.Button(top_bar, text="Reset account", command=self._reset)
        self._reset_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._status_var = tk.StringVar(value="Stopped.")
        ttk.Label(top_bar, textvariable=self._status_var).pack(side=tk.LEFT, padx=(16, 0))

        equity_bar = ttk.Frame(self._top, padding=(8, 0))
        equity_bar.pack(side=tk.TOP, fill=tk.X)
        self._equity_var = tk.StringVar()
        ttk.Label(equity_bar, textvariable=self._equity_var, font=("TkDefaultFont", 11, "bold")).pack(side=tk.LEFT)

        scan_bar = ttk.Frame(self._top, padding=(8, 0, 8, 6))
        scan_bar.pack(side=tk.TOP, fill=tk.X)
        self._scan_stats_var = tk.StringVar(value="Last scan: —   Patterns found: —   Trades opened: —   Rejected: —   Scan time: —")
        ttk.Label(scan_bar, textvariable=self._scan_stats_var, foreground="#555").pack(side=tk.LEFT)

        notebook = ttk.Notebook(self._top)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        positions_tab = ttk.Frame(notebook)
        notebook.add(positions_tab, text="Positions")
        perf_tab = ttk.Frame(notebook)
        notebook.add(perf_tab, text="Performance")

        self._build_positions_tab(positions_tab)
        self._build_performance_tab(perf_tab)

        self._refresh_all()
        self._top.after(1000, self._poll)

    # ── Positions tab ─────────────────────────────────────────────────────
    def _build_positions_tab(self, parent: ttk.Frame) -> None:
        body = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        body.pack(fill=tk.BOTH, expand=True)

        pos_frame = ttk.LabelFrame(body, text="Open positions")
        body.add(pos_frame, weight=1)
        self._pos_tree = ttk.Treeview(
            pos_frame,
            columns=("opened", "symbol", "action", "entry", "current", "unrl_pct", "r", "days", "stop", "target", "risk", "pattern"),
            show="headings", height=7,
        )
        pos_cols = [
            ("opened", 130), ("symbol", 70), ("action", 55), ("entry", 80),
            ("current", 80), ("unrl_pct", 75), ("r", 55), ("days", 50),
            ("stop", 80), ("target", 80), ("risk", 70), ("pattern", 190),
        ]
        for col, w in pos_cols:
            self._pos_tree.heading(col, text=col.replace("_", " ").capitalize())
            self._pos_tree.column(col, width=w)
        self._pos_tree.pack(fill=tk.BOTH, expand=True)

        closed_frame = ttk.LabelFrame(body, text="Closed trades")
        body.add(closed_frame, weight=2)
        self._closed_tree = ttk.Treeview(
            closed_frame,
            columns=("opened", "closed", "held", "symbol", "action", "entry", "exit", "pnl", "r", "reason", "pattern"),
            show="headings", height=10,
        )
        closed_cols = [
            ("opened", 130), ("closed", 130), ("held", 70), ("symbol", 70),
            ("action", 55), ("entry", 80), ("exit", 80), ("pnl", 70),
            ("r", 55), ("reason", 100), ("pattern", 190),
        ]
        for col, w in closed_cols:
            self._closed_tree.heading(col, text=col.capitalize())
            self._closed_tree.column(col, width=w)
        self._closed_tree.pack(fill=tk.BOTH, expand=True)

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
            columns=("pattern", "trades", "win_pct", "avg_pnl", "pf"),
            show="headings", height=8,
        )
        for col, w in [("pattern", 220), ("trades", 55), ("win_pct", 60), ("avg_pnl", 70), ("pf", 55)]:
            self._pattern_tree.heading(col, text=col.replace("_", " ").capitalize())
            self._pattern_tree.column(col, width=w)
        self._pattern_tree.pack(fill=tk.BOTH, expand=True)

        right = ttk.LabelFrame(body, text="Equity curve")
        body.add(right, weight=2)
        self._equity_chart_label = ttk.Label(right)
        self._equity_chart_label.pack(fill=tk.BOTH, expand=True)

    # ── Start / stop ────────────────────────────────────────────────────
    def _start(self) -> None:
        if self._running:
            return
        self._running = True
        self._start_btn.config(state=tk.DISABLED)
        self._stop_btn.config(state=tk.NORMAL)
        self._status_var.set("Fetching symbols...")
        threading.Thread(target=self._run_thread, args=(int(self._n_var.get()),), daemon=True).start()

    def _run_thread(self, n_symbols: int) -> None:
        symbol_rows = TVClient.fetch_top_symbols_with_exchanges_cached(n_symbols, settings.tv_screener)
        if not symbol_rows:
            self._top.after(0, lambda: self._finish("No symbols returned by screener."))
            return
        symbols = [s for s, _ex in symbol_rows]
        exchange_overrides = dict(symbol_rows)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        scanner = MarketScanner(symbols=symbols, exchange_overrides=exchange_overrides, paper_account=self._account)
        self._scanner = scanner
        self._task = loop.create_task(scanner.run())
        self._top.after(0, lambda: self._status_var.set(f"Running — {len(symbols)} symbols, scanning every {settings.scan_interval_seconds}s"))
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
        self._status_var.set(error or "Stopped.")

    def _reset(self) -> None:
        if self._running:
            messagebox.showinfo("Paper trading", "Stop the session before resetting.")
            return
        if not messagebox.askyesno("Reset account", "Wipe the paper trading account and start fresh?"):
            return
        self._account = PaperAccount()
        self._account.save()
        self._refresh_all()

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
        self._refresh_performance()

    def _refresh_header(self) -> None:
        self._equity_var.set(
            f"Cash: ${self._account.cash:,.2f}   Equity: ${self._account.equity():,.2f}   "
            f"Open: {len(self._account.positions)}   Closed: {len(self._account.closed)}"
        )
        stats = self._scanner.stats if self._scanner is not None else None
        if stats is None:
            self._scan_stats_var.set("Last scan: —   Patterns found: —   Trades opened: —   Rejected: —   Scan time: —")
            return
        last = stats["last_scan_at"]
        last_str = "—"
        if last:
            last_str = datetime.fromisoformat(last).strftime("%H:%M:%S")
        self._scan_stats_var.set(
            f"Last scan: {last_str}   Patterns found: {stats['patterns_found']}   "
            f"Trades opened: {stats['trades_opened']}   Rejected: {stats['signals_rejected']}   "
            f"Scan time: {stats['scan_duration_s']:.1f}s"
        )

    def _refresh_positions(self) -> None:
        self._pos_tree.delete(*self._pos_tree.get_children())
        now = datetime.now(timezone.utc)
        for sym, p in self._account.positions.items():
            current = self._account.last_price(sym, p.entry_price)
            r = r_multiple(p, current)
            risk = risk_dollars(p)
            self._pos_tree.insert(
                "", tk.END,
                values=(
                    p.entry_date.strftime("%Y-%m-%d %H:%M:%S"),
                    sym, p.action, f"{p.entry_price:.2f}",
                    f"{current:.2f}",
                    f"{unrealized_pct(p, current):+.2f}%",
                    f"{r:+.2f}" if r is not None else "-",
                    f"{days_held(p, now):.1f}",
                    f"{p.stop_loss:.2f}" if p.stop_loss else "-",
                    f"{p.take_profit:.2f}" if p.take_profit else "-",
                    f"${risk:,.0f}" if risk is not None else "-",
                    p.pattern,
                ),
            )

    def _refresh_closed(self) -> None:
        self._closed_tree.delete(*self._closed_tree.get_children())
        for t in sorted(self._account.closed, key=lambda t: t.exit_date, reverse=True)[:200]:
            r = r_multiple(t, t.exit_price)
            held_days = days_held(t)
            held_str = f"{held_days:.1f}d" if held_days >= 1 else f"{held_days * 24:.1f}h"
            self._closed_tree.insert(
                "", tk.END,
                values=(
                    t.entry_date.strftime("%Y-%m-%d %H:%M:%S"),
                    t.exit_date.strftime("%Y-%m-%d %H:%M:%S"),
                    held_str,
                    t.symbol, t.action,
                    f"{t.entry_price:.2f}", f"{t.exit_price:.2f}",
                    f"{t.pnl_pct:+.2f}%",
                    f"{r:+.2f}" if r is not None else "-",
                    t.exit_reason, t.pattern,
                ),
            )

    def _refresh_performance(self) -> None:
        result = self._account.to_result()
        self._summary_text.config(state=tk.NORMAL)
        self._summary_text.delete("1.0", tk.END)
        if not result.trades:
            self._summary_text.insert(tk.END, "No closed trades yet.\n")
        else:
            pf = result.profit_factor
            pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
            self._summary_text.insert(tk.END, (
                f"Trades:        {len(result.trades)}\n"
                f"Win rate:      {result.win_rate:.1%}\n"
                f"Net P&L:       {result.total_pnl_pct:+.2f}%\n"
                f"Avg winner:    {result.avg_win_pct:+.2f}%\n"
                f"Avg loser:     {result.avg_loss_pct:+.2f}%\n"
                f"Largest win:   {result.largest_win_pct:+.2f}%\n"
                f"Largest loss:  {result.largest_loss_pct:+.2f}%\n"
                f"Profit factor: {pf_str}\n"
                f"Expectancy:    {result.expectancy_pct:+.2f}%/trade\n"
                f"Max drawdown:  {result.max_drawdown_pct:+.2f}%\n"
                f"Sharpe:        {result.sharpe_ratio:.2f}\n"
            ))
        self._summary_text.config(state=tk.DISABLED)

        self._pattern_tree.delete(*self._pattern_tree.get_children())
        for pattern, s in result.pattern_breakdown().items():
            pf_str = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "inf"
            self._pattern_tree.insert(
                "", tk.END,
                values=(pattern, s["trades"], f"{s['win_rate']:.0%}", f"{s['avg_pnl_pct']:+.2f}%", pf_str),
            )

        self._refresh_equity_chart()

    def _refresh_equity_chart(self) -> None:
        curve = self._account.equity_curve
        if len(curve) < 2:
            self._equity_chart_label.config(text="Not enough closed trades yet for an equity curve.", image="")
            return
        xs = list(range(len(curve)))
        ys = [pt[1] for pt in curve]
        fig, ax = plt.subplots(figsize=(5, 3.4), dpi=100)
        ax.plot(xs, ys, color="#2962ff", linewidth=1.5)
        ax.axhline(self._account.initial_capital, color="#888", linestyle="--", linewidth=0.8)
        ax.set_title("Account equity", fontsize=10)
        ax.set_xlabel("Closed trade #", fontsize=8)
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
        self._top.destroy()
