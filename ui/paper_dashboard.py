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
from core.backtester import BacktestTrade
from core.paper_trader import (
    PaperAccount, days_held, position_status, r_multiple,
    risk_dollars, unrealized_pct,
)
from core.scanner import MarketScanner
from data.tv_client import TVClient
from utils.logger import log

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

        self._account = PaperAccount.load()
        self._scanner: Optional[MarketScanner] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._closed = False
        self._photo = None  # keep a ref so PhotoImage isn't GC'd

        self._pos_sort = ("unrl_pct", True)     # (column, descending)
        self._closed_sort = ("closed", True)

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

        exposure_bar = ttk.Frame(self._top, padding=(8, 0))
        exposure_bar.pack(side=tk.TOP, fill=tk.X)
        self._exposure_var = tk.StringVar()
        ttk.Label(exposure_bar, textvariable=self._exposure_var, foreground=COLOR_MUTED).pack(side=tk.LEFT)

        scan_bar = ttk.Frame(self._top, padding=(8, 0, 8, 6))
        scan_bar.pack(side=tk.TOP, fill=tk.X)
        self._scan_stats_var = tk.StringVar(value="Last scan: —   Patterns found: —   Trades opened: —   Rejected: —   Scan time: —")
        ttk.Label(scan_bar, textvariable=self._scan_stats_var, foreground=COLOR_MUTED).pack(side=tk.LEFT)

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

        pos_frame = ttk.LabelFrame(body, text="Open positions (click a header to sort, double-click a row for details)")
        body.add(pos_frame, weight=1)
        pos_cols = [
            ("opened", 125, "Opened"), ("symbol", 65, "Symbol"), ("status", 85, "Status"),
            ("action", 55, "Action"), ("entry", 75, "Entry"), ("current", 75, "Current"),
            ("unrl_pct", 70, "Unrl %"), ("r", 50, "R"), ("days", 45, "Days"),
            ("stop", 110, "Stop"), ("target", 110, "Target"),
            ("value", 85, "Value"), ("mtm", 85, "MTM $"), ("port_pct", 60, "Port %"), ("risk", 70, "Risk $"),
            ("pattern", 190, "Pattern"),
        ]
        self._pos_tree = _SortableTree(pos_frame, pos_cols, self._on_sort_positions, height=7)
        self._pos_tree.pack(fill=tk.BOTH, expand=True)
        self._pos_tree.bind("<Double-1>", self._on_position_double_click)
        self._configure_color_tags(self._pos_tree)
        self._pos_rows: dict[str, tuple[str, BacktestTrade]] = {}

        closed_frame = ttk.LabelFrame(body, text="Closed trades")
        body.add(closed_frame, weight=2)
        closed_cols = [
            ("opened", 125, "Opened"), ("closed", 125, "Closed"), ("held", 65, "Held"),
            ("symbol", 65, "Symbol"), ("action", 55, "Action"), ("entry", 75, "Entry"),
            ("exit", 75, "Exit"), ("pnl", 65, "P&L"), ("r", 50, "R"),
            ("reason", 100, "Reason"), ("pattern", 190, "Pattern"),
        ]
        self._closed_tree = _SortableTree(closed_frame, closed_cols, self._on_sort_closed, height=10)
        self._closed_tree.pack(fill=tk.BOTH, expand=True)
        self._closed_tree.bind("<Double-1>", self._on_closed_double_click)
        self._configure_color_tags(self._closed_tree)
        self._closed_rows: dict[str, BacktestTrade] = {}

    @staticmethod
    def _configure_color_tags(tree: ttk.Treeview) -> None:
        tree.tag_configure("gain", foreground=COLOR_GAIN)
        tree.tag_configure("loss", foreground=COLOR_LOSS)
        tree.tag_configure("flat", foreground=COLOR_MUTED)
        tree.tag_configure("buy", foreground=COLOR_BUY)
        tree.tag_configure("sell", foreground=COLOR_SELL)
        tree.tag_configure("trailing", foreground=COLOR_TRAILING)
        tree.tag_configure("breakeven", foreground=COLOR_BREAKEVEN)

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

        pattern_frame = ttk.LabelFrame(left, text="By pattern (disable losers)")
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

    # ── Sorting ───────────────────────────────────────────────────────────
    def _on_sort_positions(self, col: str) -> None:
        cur_col, cur_desc = self._pos_sort
        self._pos_sort = (col, not cur_desc if col == cur_col else True)
        self._refresh_positions()

    def _on_sort_closed(self, col: str) -> None:
        cur_col, cur_desc = self._closed_sort
        self._closed_sort = (col, not cur_desc if col == cur_col else True)
        self._refresh_closed()

    # ── Row detail popups ─────────────────────────────────────────────────
    def _show_trade_details(self, t: BacktestTrade, current_price: Optional[float]) -> None:
        win = tk.Toplevel(self._top)
        win.title(f"{t.symbol} — {t.pattern}")
        text = tk.Text(win, width=64, height=20, font=("TkFixedFont", 9))
        text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        lines = [
            f"Symbol:     {t.symbol}",
            f"Pattern:    {t.pattern}",
            f"Timeframe:  {t.timeframe}",
            f"Action:     {t.action}",
            f"Confidence: {t.confidence:.0%}",
            f"Opened:     {t.entry_date.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Entry:      {t.entry_price:.2f}",
            f"Stop:       {t.stop_loss:.2f}" if t.stop_loss else "Stop:       -",
            f"Target:     {t.take_profit:.2f}" if t.take_profit else "Target:     -",
        ]
        if current_price is not None:
            r = r_multiple(t, current_price)
            lines += [
                f"Current:    {current_price:.2f}",
                f"Unrealized: {unrealized_pct(t, current_price):+.2f}%",
                f"R:          {r:+.2f}" if r is not None else "R:          -",
            ]
        else:
            r = r_multiple(t, t.exit_price)
            lines += [
                f"Closed:     {t.exit_date.strftime('%Y-%m-%d %H:%M:%S')}",
                f"Exit:       {t.exit_price:.2f}",
                f"P&L:        {t.pnl_pct:+.2f}%",
                f"R:          {r:+.2f}" if r is not None else "R:          -",
                f"Exit reason:{t.exit_reason}",
            ]
        if t.notes:
            lines += ["", "Notes:", t.notes]
        text.insert(tk.END, "\n".join(lines))
        text.config(state=tk.DISABLED)

    def _on_position_double_click(self, _event) -> None:
        sel = self._pos_tree.selection()
        if not sel:
            return
        entry = self._pos_rows.get(sel[0])
        if entry is None:
            return
        sym, t = entry
        self._show_trade_details(t, self._account.last_price(sym, t.entry_price))

    def _on_closed_double_click(self, _event) -> None:
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
        self._refresh_performance()

    def _refresh_header(self) -> None:
        self._equity_var.set(
            f"Cash: ${self._account.cash:,.2f}   Equity: ${self._account.equity():,.2f}   "
            f"Open: {len(self._account.positions)}   Closed: {len(self._account.closed)}"
        )
        exp = self._account.exposure()
        self._exposure_var.set(
            f"Long exposure: {exp['long_pct']:.1f}%   Short exposure: {exp['short_pct']:.1f}%   "
            f"Net exposure: {exp['net_pct']:+.1f}%"
        )
        stats = self._scanner.stats if self._scanner is not None else None
        if stats is None:
            self._scan_stats_var.set("Last scan: —   Patterns found: —   Trades opened: —   Rejected: —   Scan time: —")
            return
        last = stats["last_scan_at"]
        last_str = "—" if not last else datetime.fromisoformat(last).strftime("%H:%M:%S")
        self._scan_stats_var.set(
            f"Last scan: {last_str}   Patterns found: {stats['patterns_found']}   "
            f"Trades opened: {stats['trades_opened']}   Rejected: {stats['signals_rejected']}   "
            f"Scan time: {stats['scan_duration_s']:.1f}s"
        )

    def _refresh_positions(self) -> None:
        self._pos_tree.delete(*self._pos_tree.get_children())
        self._pos_rows = {}
        now = datetime.now(timezone.utc)
        equity = self._account.equity()

        rows = []
        for sym, p in self._account.positions.items():
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
                "unrl": unrealized_pct(p, current), "days": days_held(p, now),
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
                    stop_str, target_str,
                    f"${row['value']:,.0f}", f"{row['mtm']:+,.0f}", f"{row['port_pct']:.1f}%",
                    f"${row['risk']:,.0f}" if row["risk"] is not None else "-",
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
            "symbol": lambda t: t.symbol,
            "action": lambda t: t.action,
            "entry": lambda t: t.entry_price,
            "exit": lambda t: t.exit_price,
            "pnl": lambda t: t.pnl_pct,
            "r": lambda t: (r_multiple(t, t.exit_price) or float("-inf")),
            "reason": lambda t: t.exit_reason,
            "pattern": lambda t: t.pattern,
        }.get(sort_col, lambda t: t.exit_date)
        trades = sorted(self._account.closed, key=sort_key, reverse=desc)[:200]

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
                    held_str, t.symbol, t.action,
                    f"{t.entry_price:.2f}", f"{t.exit_price:.2f}",
                    f"{t.pnl_pct:+.2f}%",
                    f"{r:+.2f}" if r is not None else "-",
                    t.exit_reason, t.pattern,
                ),
                tags=(_pnl_tag(t.pnl_pct), action_tag),
            )
            self._closed_rows[item_id] = t

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
