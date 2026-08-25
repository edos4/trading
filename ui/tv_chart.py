"""Interactive TradingView-style candlestick viewer (tk Canvas, no PNG)."""

from __future__ import annotations

import tkinter as tk
from typing import Any, Optional

TV_BG = "#131722"
TV_GRID = "#2a2e39"
TV_TEXT = "#d1d4dc"
TV_DIM = "#787b86"
TV_UP = "#26a69a"
TV_DOWN = "#ef5350"
TV_KRONOS = "#e040fb"
TV_KRONOS_UP = "#ce93d8"
TV_KRONOS_DOWN = "#7b1fa2"
TV_CROSS = "#9598a1"


def open_trade_viewer(parent: tk.Misc, payload: dict[str, Any]) -> tk.Toplevel:
    win = tk.Toplevel(parent)
    win.title(payload.get("title") or payload.get("symbol") or "Chart")
    win.geometry("1120x700")
    win.minsize(720, 460)
    win.configure(bg=TV_BG)
    chart = TradingViewChart(win)
    chart.pack(fill=tk.BOTH, expand=True)
    chart.set_payload(payload)
    win.focus_set()
    return win


class TradingViewChart(tk.Frame):
    def __init__(self, master: tk.Misc, **kwargs):
        super().__init__(master, bg=TV_BG, **kwargs)
        self._payload: dict[str, Any] = {}
        self._candles: list[dict] = []
        self._volume: dict[str, dict] = {}
        self._levels: list[dict] = []
        self._markers: dict[str, dict] = {}
        self._forecast: dict[str, float] = {}
        self._forecast_color = TV_KRONOS
        self._start = 0
        self._visible = 120
        self._hover: Optional[int] = None
        self._drag_x: Optional[int] = None
        self._drag_start = 0
        self._price_lo = 0.0
        self._price_hi = 1.0
        self._vol_hi = 1.0
        self._plot = (0, 0, 1, 1)
        self._vol_plot = (0, 0, 1, 1)

        self._header = tk.Frame(self, bg=TV_BG)
        self._header.pack(fill=tk.X, padx=10, pady=(8, 0))
        self._title_var = tk.StringVar()
        self._ohlc_var = tk.StringVar()
        self._legend_var = tk.StringVar(value="scroll to zoom · drag to pan")
        self._default_legend = self._legend_var.get()
        tk.Label(
            self._header, textvariable=self._title_var, fg=TV_TEXT, bg=TV_BG,
            font=("Trebuchet MS", 13, "bold"), anchor="w",
        ).pack(fill=tk.X)
        self._ohlc_label = tk.Label(
            self._header, textvariable=self._ohlc_var, fg=TV_DIM, bg=TV_BG,
            font=("Trebuchet MS", 10), anchor="w",
        )
        self._ohlc_label.pack(fill=tk.X)
        tk.Label(
            self._header, textvariable=self._legend_var, fg=TV_DIM, bg=TV_BG,
            font=("Trebuchet MS", 9), anchor="w",
        ).pack(fill=tk.X)

        self._canvas = tk.Canvas(self, bg=TV_BG, highlightthickness=0, cursor="crosshair")
        self._canvas.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self._canvas.bind("<Configure>", lambda _e: self._redraw())
        self._canvas.bind("<Motion>", self._on_motion)
        self._canvas.bind("<Leave>", self._on_leave)
        self._canvas.bind("<ButtonPress-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<MouseWheel>", self._on_wheel)
        self._canvas.bind("<Button-4>", lambda e: self._zoom_at(e.x, 0.85))
        self._canvas.bind("<Button-5>", lambda e: self._zoom_at(e.x, 1.18))
        self._canvas.focus_set()
        self._canvas.bind("<Left>", lambda _e: self._pan(-max(1, self._visible // 12)))
        self._canvas.bind("<Right>", lambda _e: self._pan(max(1, self._visible // 12)))

    def set_payload(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self._candles = list(payload.get("candles") or [])
        seen = {row["time"] for row in self._candles}
        for row in payload.get("pred_candles") or []:
            if row.get("time") in seen:
                continue
            self._candles.append({**row, "predicted": True})
            seen.add(row["time"])
        self._volume = {row["time"]: row for row in payload.get("volume") or []}
        self._levels = list(payload.get("levels") or [])
        self._markers = {row["time"]: row for row in payload.get("markers") or []}
        self._forecast = {
            row["time"]: row["value"] for row in payload.get("forecast") or []
            if row.get("time") is not None and row.get("value") is not None
        }
        self._forecast_color = payload.get("forecast_color") or TV_KRONOS
        n = len(self._candles)
        extra = len(payload.get("pred_candles") or [])
        self._visible = min(max(n, 2), max(180, extra + 80))
        self._start = max(0, n - self._visible)
        title = payload.get("title") or payload.get("symbol") or "Chart"
        self._title_var.set(title)
        if self._forecast:
            self._legend_var.set(
                "Kronos forecast    scroll to zoom · drag to pan"
            )
        else:
            self._legend_var.set(self._default_legend)
        self._set_ohlc_label(self._candles[-1] if self._candles else None, from_last=True)
        self._redraw()

    def _set_ohlc_label(self, candle: Optional[dict], *, from_last: bool = False) -> None:
        if not candle:
            self._ohlc_var.set("")
            self._ohlc_label.configure(fg=TV_DIM)
            return
        o, h, low, c = candle["open"], candle["high"], candle["low"], candle["close"]
        prev = None
        if from_last and len(self._candles) > 1:
            prev = self._candles[-2]["close"]
        elif not from_last:
            idx = next((i for i, row in enumerate(self._candles) if row["time"] == candle["time"]), -1)
            if idx > 0:
                prev = self._candles[idx - 1]["close"]
        change = c - (prev if prev is not None else o)
        pct = (change / prev * 100.0) if prev else 0.0
        sign = "+" if change >= 0 else ""
        vol = (self._volume.get(candle["time"]) or {}).get("value")
        vol_s = _fmt_volume(vol) if vol is not None else "—"
        prefix = "Kronos  " if candle.get("predicted") else ""
        self._ohlc_var.set(
            f"{prefix}{candle['time']}    O {_fmt_price(o)}  H {_fmt_price(h)}  "
            f"L {_fmt_price(low)}  C {_fmt_price(c)}    "
            f"{sign}{_fmt_price(change)} ({sign}{pct:.2f}%)    Vol {vol_s}"
        )
        self._ohlc_label.configure(fg=TV_UP if change >= 0 else TV_DOWN)

    def _redraw(self) -> None:
        c = self._canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 40 or h < 40 or len(self._candles) < 2:
            return
        axis_w = 72
        time_h = 28
        pad_l, pad_t = 8, 8
        vol_h = max(52, int(h * 0.18))
        price_bottom = h - time_h - vol_h
        self._plot = (pad_l, pad_t, w - axis_w, price_bottom)
        self._vol_plot = (pad_l, price_bottom + 6, w - axis_w, h - time_h)
        self._clamp_window()
        visible = self._candles[self._start:self._start + self._visible]
        if not visible:
            return
        lows = [row["low"] for row in visible]
        highs = [row["high"] for row in visible]
        for level in self._levels:
            lows.append(level["price"])
            highs.append(level["price"])
        for row in visible:
            val = self._forecast.get(row["time"])
            if val is not None:
                lows.append(val)
                highs.append(val)
        self._price_lo = min(lows)
        self._price_hi = max(highs)
        pad = (self._price_hi - self._price_lo) * 0.04 or 0.01
        self._price_lo -= pad
        self._price_hi += pad
        vols = [
            (self._volume.get(row["time"]) or {}).get("value") or 0.0
            for row in visible
        ]
        self._vol_hi = max(vols) * 1.15 if vols else 1.0
        self._draw_grid(visible)
        self._draw_candles(visible)
        self._draw_forecast(visible)
        self._draw_volume(visible)
        self._draw_levels()
        self._draw_axis(visible)
        self._draw_markers(visible)
        if self._hover is not None:
            self._draw_crosshair(self._hover)

    def _clamp_window(self) -> None:
        n = len(self._candles)
        self._visible = max(20, min(self._visible, max(n, 20)))
        self._start = max(0, min(self._start, max(0, n - 2)))
        if self._start + self._visible > n:
            self._start = max(0, n - self._visible)

    def _x_for(self, i: int) -> float:
        x0, _, x1, _ = self._plot
        span = max(self._visible, 1)
        return x0 + (i + 0.5) * (x1 - x0) / span

    def _bar_w(self) -> float:
        x0, _, x1, _ = self._plot
        return max(1.0, (x1 - x0) / max(self._visible, 1) * 0.7)

    def _y_price(self, price: float) -> float:
        _, y0, _, y1 = self._plot
        span = self._price_hi - self._price_lo or 1.0
        return y1 - (price - self._price_lo) / span * (y1 - y0)

    def _y_vol(self, value: float) -> float:
        _, y0, _, y1 = self._vol_plot
        return y1 - (value / (self._vol_hi or 1.0)) * (y1 - y0)

    def _draw_grid(self, visible: list[dict]) -> None:
        c = self._canvas
        x0, y0, x1, y1 = self._plot
        vx0, vy0, vx1, vy1 = self._vol_plot
        steps = 6
        for i in range(steps + 1):
            frac = i / steps
            y = y0 + (y1 - y0) * frac
            c.create_line(x0, y, x1, y, fill=TV_GRID, width=1)
            price = self._price_hi - frac * (self._price_hi - self._price_lo)
            c.create_text(
                x1 + 8, y, text=_fmt_price(price), fill=TV_DIM,
                anchor="w", font=("Trebuchet MS", 9),
            )
        c.create_line(x1, y0, x1, vy1, fill=TV_GRID, width=1)
        c.create_line(x0, vy1, vx1, vy1, fill=TV_GRID, width=1)
        stride = max(1, len(visible) // 6)
        for i, row in enumerate(visible):
            if i % stride != 0 and i != len(visible) - 1:
                continue
            x = self._x_for(i)
            c.create_line(x, y0, x, vy1, fill=TV_GRID, width=1)
            c.create_text(
                x, vy1 + 10, text=row["time"][5:] if len(row["time"]) >= 10 else row["time"],
                fill=TV_DIM, anchor="n", font=("Trebuchet MS", 8),
            )

    def _draw_candles(self, visible: list[dict]) -> None:
        c = self._canvas
        half = self._bar_w() / 2
        for i, row in enumerate(visible):
            x = self._x_for(i)
            if row.get("predicted"):
                color = TV_KRONOS_UP if row["close"] >= row["open"] else TV_KRONOS_DOWN
            else:
                color = TV_UP if row["close"] >= row["open"] else TV_DOWN
            y_h = self._y_price(row["high"])
            y_l = self._y_price(row["low"])
            y_o = self._y_price(row["open"])
            y_c = self._y_price(row["close"])
            c.create_line(x, y_h, x, y_l, fill=color, width=1)
            top, bot = min(y_o, y_c), max(y_o, y_c)
            if bot - top < 1:
                bot = top + 1
            c.create_rectangle(x - half, top, x + half, bot, outline=color, fill=color)

    def _draw_volume(self, visible: list[dict]) -> None:
        c = self._canvas
        half = self._bar_w() / 2
        _, _, _, y1 = self._vol_plot
        for i, row in enumerate(visible):
            if row.get("predicted"):
                continue
            vol = (self._volume.get(row["time"]) or {}).get("value") or 0.0
            x = self._x_for(i)
            y = self._y_vol(vol)
            color = "#1f5c56" if row["close"] >= row["open"] else "#6e3331"
            c.create_rectangle(x - half, y, x + half, y1, outline="", fill=color)

    def _draw_forecast(self, visible: list[dict]) -> None:
        if not self._forecast:
            return
        self._polyline(visible, self._forecast, self._forecast_color, width=2)

    def _polyline(
        self, visible: list[dict], series: dict[str, float], color: str, width: int = 1,
    ) -> None:
        pts = []
        for i, row in enumerate(visible):
            val = series.get(row["time"])
            if val is None:
                continue
            pts.extend([self._x_for(i), self._y_price(val)])
        if len(pts) >= 4:
            self._canvas.create_line(*pts, fill=color, width=width, smooth=False)

    def _draw_levels(self) -> None:
        x0, _, x1, _ = self._plot
        for level in self._levels:
            y = self._y_price(level["price"])
            self._canvas.create_line(
                x0, y, x1, y, fill=level["color"], width=1, dash=(6, 4),
            )
            self._canvas.create_text(
                x1 - 4, y - 8, text=f"{level['title']} {_fmt_price(level['price'])}",
                fill=level["color"], anchor="e", font=("Trebuchet MS", 8),
            )

    def _draw_axis(self, visible: list[dict]) -> None:
        last = visible[-1]
        y = self._y_price(last["close"])
        color = TV_UP if last["close"] >= last["open"] else TV_DOWN
        x0, _, x1, _ = self._plot
        self._canvas.create_line(x0, y, x1, y, fill=color, width=1, dash=(4, 3))
        self._canvas.create_rectangle(
            x1 + 2, y - 8, x1 + 70, y + 8, fill=color, outline=color,
        )
        self._canvas.create_text(
            x1 + 36, y, text=_fmt_price(last["close"]),
            fill="#ffffff", font=("Trebuchet MS", 8, "bold"),
        )

    def _draw_markers(self, visible: list[dict]) -> None:
        for i, row in enumerate(visible):
            marker = self._markers.get(row["time"])
            if not marker:
                continue
            x = self._x_for(i)
            buy = marker.get("shape") == "arrowUp"
            y = self._y_price(row["low"] if buy else row["high"])
            color = marker.get("color") or TV_TEXT
            if buy:
                self._canvas.create_polygon(
                    x, y + 14, x - 6, y + 2, x + 6, y + 2, fill=color, outline=color,
                )
            else:
                self._canvas.create_polygon(
                    x, y - 14, x - 6, y - 2, x + 6, y - 2, fill=color, outline=color,
                )
            self._canvas.create_text(
                x, y + (22 if buy else -22), text=marker.get("text") or "",
                fill=color, font=("Trebuchet MS", 8, "bold"),
            )

    def _draw_crosshair(self, i: int) -> None:
        if i < 0 or i >= self._visible:
            return
        idx = self._start + i
        if idx >= len(self._candles):
            return
        row = self._candles[idx]
        x = self._x_for(i)
        _, y0, _, y1 = self._plot
        _, vy0, _, vy1 = self._vol_plot
        y = self._y_price(row["close"])
        self._canvas.create_line(x, y0, x, vy1, fill=TV_CROSS, dash=(3, 3), tags="xh")
        self._canvas.create_line(self._plot[0], y, self._plot[2], y, fill=TV_CROSS, dash=(3, 3), tags="xh")

    def _index_at(self, x: int) -> Optional[int]:
        x0, _, x1, _ = self._plot
        if x < x0 or x > x1:
            return None
        frac = (x - x0) / max(x1 - x0, 1)
        i = int(frac * self._visible)
        if 0 <= i < min(self._visible, len(self._candles) - self._start):
            return i
        return None

    def _on_motion(self, event) -> None:
        i = self._index_at(event.x)
        self._hover = i
        if i is not None:
            self._set_ohlc_label(self._candles[self._start + i])
        self._redraw()

    def _on_leave(self, _event) -> None:
        self._hover = None
        self._set_ohlc_label(self._candles[-1] if self._candles else None, from_last=True)
        self._redraw()

    def _on_press(self, event) -> None:
        self._canvas.focus_set()
        self._drag_x = event.x
        self._drag_start = self._start

    def _on_drag(self, event) -> None:
        if self._drag_x is None:
            return
        bar_px = max((self._plot[2] - self._plot[0]) / max(self._visible, 1), 1.0)
        shift = int((self._drag_x - event.x) / bar_px)
        self._start = self._drag_start + shift
        self._clamp_window()
        self._redraw()

    def _on_release(self, _event) -> None:
        self._drag_x = None

    def _on_wheel(self, event) -> None:
        factor = 0.85 if event.delta > 0 else 1.18
        self._zoom_at(event.x, factor)

    def _zoom_at(self, x: int, factor: float) -> None:
        anchor = self._index_at(x)
        if anchor is None:
            anchor = self._visible - 1
        abs_i = self._start + anchor
        self._visible = int(self._visible * factor)
        self._clamp_window()
        self._start = abs_i - int(anchor * self._visible / max(anchor + 1, self._visible))
        self._clamp_window()
        self._redraw()

    def _pan(self, bars: int) -> None:
        self._start += bars
        self._clamp_window()
        self._redraw()


def _fmt_price(value: float) -> str:
    if abs(value) >= 1:
        return f"{value:.2f}"
    return f"{value:.4f}"


def _fmt_volume(value: float) -> str:
    av = abs(value)
    if av >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if av >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if av >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"
