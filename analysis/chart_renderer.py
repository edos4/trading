"""
analysis/chart_renderer.py — Renders OHLCV + indicator charts as PNG images.

Produces TradingView-style dark candlestick charts for scan review and vision checks.
Uses mplfinance for candlestick rendering.
"""

from __future__ import annotations
import io
import json
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from utils.logger import log

CHARTS_DIR = Path("charts")

# Default visible range — mirrors TradingView "1Y" on daily charts
VISIBLE_BARS: dict[str, int] = {
    "1d": 252,
    "1W": 65,
    "1M": 12,
}

# TradingView dark theme palette (classic TV candle colors)
TV_BG = "#131722"
TV_GRID = "#2a2e39"
TV_TEXT = "#d1d4dc"
TV_TEXT_DIM = "#787b86"
TV_UP = "#26a69a"
TV_DOWN = "#ef5350"
TV_RSI = "#7e57c2"
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0


class ChartRenderer:
    def __init__(self, save_to_disk: bool = False, session_tz: str = "America/New_York"):
        self._save = save_to_disk
        self._session_tz = session_tz or "America/New_York"
        if save_to_disk:
            CHARTS_DIR.mkdir(exist_ok=True)

    def render(
        self,
        symbol: str,
        timeframe: str,
        ohlcv_df: pd.DataFrame,
        extra_plots: list | None = None,
        title: str | None = None,
        annotations: list[dict] | None = None,
    ) -> bytes:
        """Render a TradingView-style candlestick chart and return PNG bytes."""
        return self._render_chart(
            symbol, timeframe, ohlcv_df,
            add_plots=extra_plots, title=title, annotations=annotations,
        )

    # ── Internal ───────────────────────────────────────────────────────────────
    def _render_chart(
        self,
        symbol: str,
        timeframe: str,
        ohlcv_df: pd.DataFrame,
        add_plots: list | None = None,
        title: str | None = None,
        indicators: dict[str, pd.Series] | None = None,
        annotations: list[dict] | None = None,
    ) -> bytes:
        if isinstance(ohlcv_df.index, pd.DatetimeIndex) and "Open" in ohlcv_df.columns:
            df = ohlcv_df
        else:
            df = self._prepare_df(ohlcv_df, timeframe)
            df = self._trim_to_visible(df, timeframe)

        buf = io.BytesIO()
        style = self._tradingview_style()

        plot_kwargs = dict(
            type="candle",
            style=style,
            volume=True,
            figsize=(14, 9.2),
            panel_ratios=(4, 1, 1.35),
            tight_layout=True,
            returnfig=True,
            warn_too_much_data=2500,
            scale_padding={"left": 0.05, "right": 1.25, "top": 0.85, "bottom": 0.35},
            update_width_config=dict(
                candle_width=0.65,
                candle_linewidth=0.8,
                volume_width=0.65,
                volume_linewidth=0.0,
            ),
            volume_alpha=0.55,
            ylabel="",
        )
        rsi = _rsi_sma(df["Close"], RSI_PERIOD)
        plots = list(add_plots or [])
        self._append_series_plot(
            plots, rsi, panel=2, color=TV_RSI, width=1.0, ylabel="RSI",
        )
        self._append_series_plot(
            plots, pd.Series(RSI_OVERBOUGHT, index=df.index),
            panel=2, color=TV_TEXT_DIM, width=0.7, linestyle="dashed",
        )
        self._append_series_plot(
            plots, pd.Series(RSI_OVERSOLD, index=df.index),
            panel=2, color=TV_TEXT_DIM, width=0.7, linestyle="dashed",
        )
        plot_kwargs["addplot"] = plots
        indicators = dict(indicators or {})
        indicators["rsi_14"] = rsi
        fig, axes = mpf.plot(df, **plot_kwargs)
        self._polish_axes(axes, timeframe)
        self._format_xaxis_months(axes, df, timeframe)
        self._draw_tradingview_header(fig, axes, symbol, timeframe, df)
        self._draw_last_price_line(axes[0], df)
        if annotations:
            self._draw_annotations(axes[0], df, annotations)

        if axes[0].get_legend() is not None:
            axes[0].get_legend().remove()

        fig.subplots_adjust(top=0.92)

        png_bytes = self._save_figure(
            fig, buf, symbol, timeframe, df, indicators=indicators
        )
        return png_bytes

    def _save_figure(
        self,
        fig,
        buf: io.BytesIO,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        indicators: dict[str, pd.Series] | None = None,
    ) -> bytes:
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=TV_BG)
        plt.close(fig)

        png_bytes = buf.getvalue()
        if self._save:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            base = CHARTS_DIR / f"{symbol}_{timeframe}_{ts}"
            json_path = base.with_suffix(".json")
            json_path.write_text(
                json.dumps(
                    self._chart_payload(symbol, timeframe, df, indicators, ts),
                    indent=2,
                ),
                encoding="utf-8",
            )
            log.info(f"ChartRenderer | Saved chart data → {json_path}")
            path = base.with_suffix(".png")
            path.write_bytes(png_bytes)
            log.info(f"ChartRenderer | Saved chart → {path}")
        return png_bytes

    @staticmethod
    def _chart_payload(
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        indicators: dict[str, pd.Series] | None,
        ts: str,
    ) -> dict:
        bars = []
        for i, (idx, row) in enumerate(df.iterrows()):
            bar: dict = {
                "date": idx.strftime("%Y-%m-%d")
                if isinstance(idx, pd.Timestamp)
                else str(idx),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            }
            if indicators:
                for name, series in indicators.items():
                    val = series.iloc[i]
                    bar[name] = None if pd.isna(val) else float(val)
            bars.append(bar)

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "generated_at": ts,
            "bar_count": len(bars),
            "bars": bars,
        }

    def _prepare_df(self, df: pd.DataFrame, timeframe: str = "1d") -> pd.DataFrame:
        """
        mplfinance expects Title-case columns and a DatetimeIndex.
        Synthesizes business-day dates when the store has no timestamps.
        """
        rename = {
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
        out = df.rename(columns=rename)
        if isinstance(out.index, pd.DatetimeIndex):
            # Sort by the raw timestamps first so that, after session-date
            # normalization below, "keep last per date" retains the latest
            # intra-day bar. Some tapes carry two bars for one session (a
            # bogus pre-market bar at 04:00 UTC plus the real 13:30 UTC bar).
            # Duplicate dates yield non-monotonic candle times that break
            # TradingView/LightweightCharts, so they must be collapsed here.
            out = out.sort_index()
            out = self._normalize_session_index(out)
            out = out[~out.index.duplicated(keep="last")]
        if not isinstance(out.index, pd.DatetimeIndex):
            end = pd.Timestamp.now().normalize()
            if timeframe == "1W":
                out.index = pd.date_range(end=end, periods=len(out), freq="W-FRI")
            elif timeframe == "1M":
                out.index = pd.date_range(end=end, periods=len(out), freq="ME")
            else:
                out.index = pd.bdate_range(end=end, periods=len(out))
        return out

    @staticmethod
    def _trim_to_visible(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        bars = VISIBLE_BARS.get(timeframe, VISIBLE_BARS["1d"])
        if len(df) > bars:
            return df.iloc[-bars:]
        return df

    @staticmethod
    def _tv_timeframe_label(timeframe: str) -> str:
        labels = {"1d": "1D", "1W": "1W", "1M": "1M", "1h": "1H", "4h": "4H"}
        return labels.get(timeframe, timeframe.upper())

    @staticmethod
    def _tradingview_style():
        market_colors = mpf.make_marketcolors(
            up=TV_UP,
            down=TV_DOWN,
            edge="inherit",
            wick={"up": TV_UP, "down": TV_DOWN},
            volume={"up": TV_UP, "down": TV_DOWN},
            ohlc="inherit",
            alpha=1.0,
        )
        return mpf.make_mpf_style(
            base_mpf_style="nightclouds",
            marketcolors=market_colors,
            facecolor=TV_BG,
            figcolor=TV_BG,
            gridcolor=TV_GRID,
            gridstyle="--",
            gridaxis="both",
            y_on_right=True,
            rc={
                "axes.edgecolor": TV_GRID,
                "axes.labelcolor": TV_TEXT_DIM,
                "axes.titlecolor": TV_TEXT,
                "axes.grid": True,
                "figure.facecolor": TV_BG,
                "font.family": "sans-serif",
                "font.size": 9,
                "grid.alpha": 0.45,
                "grid.color": TV_GRID,
                "grid.linestyle": "--",
                "grid.linewidth": 0.5,
                "savefig.facecolor": TV_BG,
                "text.color": TV_TEXT,
                "xtick.color": TV_TEXT_DIM,
                "ytick.color": TV_TEXT_DIM,
            },
        )

    @staticmethod
    def _append_series_plot(add_plots: list, series: pd.Series, **kwargs) -> None:
        if series.notna().any():
            add_plots.append(mpf.make_addplot(series, **kwargs))

    @staticmethod
    def _polish_axes(axes, timeframe: str) -> None:
        for axis in axes:
            axis.set_facecolor(TV_BG)
            axis.grid(True, alpha=0.45, color=TV_GRID, linestyle="--", linewidth=0.5)
            axis.tick_params(colors=TV_TEXT_DIM, labelsize=8)
            for spine in axis.spines.values():
                spine.set_color(TV_GRID)

        for axis in axes:
            ylabel = axis.get_ylabel()
            if ylabel in ("", "Price", "Volume"):
                axis.set_ylabel("")
            if not ylabel or ylabel.startswith("Volume"):
                axis.yaxis.set_visible(True)
            if ylabel.startswith("Volume"):
                axis.yaxis.set_major_formatter(
                    FuncFormatter(lambda value, _: ChartRenderer._format_volume(value))
                )
                axis.yaxis.get_offset_text().set_visible(False)

        for axis in axes[:-1]:
            plt.setp(axis.get_xticklabels(), visible=False)

    def _normalize_session_index(self, df: pd.DataFrame) -> pd.DataFrame:
        """Session dates in this market's timezone (NY or Manila)."""
        out = df.copy()
        idx = out.index
        tz = self._session_tz
        if idx.tz is not None:
            idx = idx.tz_convert(tz)
        else:
            idx = idx.tz_localize(tz)
        out.index = idx.tz_localize(None).normalize()
        return out

    def _format_xaxis_months(self, axes, df: pd.DataFrame, timeframe: str) -> None:
        """
        Month labels on mplfinance's integer bar index (not matplotlib dates).
        mdates formatters mis-label the axis as Jan–Sep regardless of data range.
        """
        if timeframe not in ("1d", "1W") or not isinstance(df.index, pd.DatetimeIndex):
            return

        dates = self._normalize_session_index(df).index
        date_axis = axes[-1]

        tick_positions: list[int] = []
        tick_labels: list[str] = []
        prev_key: tuple[int, int] | None = None
        for i, dt in enumerate(dates):
            key = (dt.year, dt.month)
            if key == prev_key:
                continue
            tick_positions.append(i)
            tick_labels.append(
                f"{dt.strftime('%b')}\n{dt.year}"
                if dt.month == 1
                else dt.strftime("%b")
            )
            prev_key = key

        date_axis.set_xticks(tick_positions)
        date_axis.set_xticklabels(
            tick_labels, ha="center", fontsize=8, color=TV_TEXT_DIM
        )

    @staticmethod
    def _draw_tradingview_header(
        fig,
        axes,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
    ) -> None:
        """Symbol + OHLC header in the top-left, matching TradingView layout."""
        price_axis = axes[0]
        latest = df.iloc[-1]
        previous_close = df["Close"].iloc[-2] if len(df) > 1 else latest["Close"]
        change = latest["Close"] - previous_close
        change_pct = (change / previous_close * 100) if previous_close else 0
        change_color = TV_UP if change >= 0 else TV_DOWN

        title = f"{symbol} · {ChartRenderer._tv_timeframe_label(timeframe)} · NASDAQ"
        price_axis.text(
            0.0,
            1.08,
            title,
            transform=price_axis.transAxes,
            va="bottom",
            ha="left",
            fontsize=11,
            fontweight="bold",
            color=TV_TEXT,
            clip_on=False,
        )

        header = (
            f"O {latest['Open']:.2f}  "
            f"H {latest['High']:.2f}  "
            f"L {latest['Low']:.2f}  "
            f"C {latest['Close']:.2f}  "
            f"{change:+.2f} ({change_pct:+.2f}%)"
        )
        price_axis.text(
            0.0,
            1.03,
            header,
            transform=price_axis.transAxes,
            va="bottom",
            ha="left",
            fontsize=9,
            color=change_color,
            clip_on=False,
        )

    @staticmethod
    def _draw_last_price_line(price_axis, df: pd.DataFrame) -> None:
        """Dashed last-price line with label on the right axis."""
        last_close = df["Close"].iloc[-1]
        previous_close = df["Close"].iloc[-2] if len(df) > 1 else last_close
        line_color = TV_UP if last_close >= previous_close else TV_DOWN

        price_axis.axhline(
            last_close, color=line_color, linewidth=0.8, linestyle="--", alpha=0.85
        )
        price_axis.annotate(
            f" {last_close:.2f} ",
            xy=(1.0, last_close),
            xycoords=("axes fraction", "data"),
            xytext=(2, 0),
            textcoords="offset points",
            ha="left",
            va="center",
            fontsize=8,
            color="#ffffff",
            bbox=dict(
                facecolor=line_color,
                edgecolor=line_color,
                pad=1.5,
                boxstyle="square,pad=0.2",
            ),
            clip_on=False,
        )

    @staticmethod
    def _date_to_x(df: pd.DataFrame, date_str: str) -> int | None:
        """Integer x-position of a bar by ISO date, or None if not in view."""
        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex):
            return None
        try:
            ts = pd.Timestamp(date_str)
        except (ValueError, TypeError):
            return None
        positions = np.where(idx.normalize() == ts.normalize())[0]
        return int(positions[0]) if len(positions) else None

    def _draw_annotations(
        self, price_axis, df: pd.DataFrame, annotations: list[dict]
    ) -> None:
        """Overlay pattern markers, horizontal lines, and trend segments."""
        for ann in annotations:
            kind = ann.get("type")
            if kind == "marker":
                self._draw_marker(price_axis, df, ann)
            elif kind == "hline":
                self._draw_hline(price_axis, ann)
            elif kind == "segment":
                self._draw_segment(price_axis, df, ann)

    def _draw_marker(
        self, price_axis, df: pd.DataFrame, ann: dict
    ) -> None:
        x = self._date_to_x(df, ann.get("date", ""))
        if x is None:
            return
        price = float(ann["price"])
        color = ann.get("color", TV_TEXT)
        marker = ann.get("marker", "o")
        label = ann.get("label", "")
        price_axis.scatter(
            [x], [price], marker=marker, s=70,
            color=color, edgecolors="#ffffff", linewidths=0.6, zorder=5,
        )
        if not label:
            return
        pos = ann.get("label_pos", "above")
        offset = 8 if pos == "above" else -8
        va = "bottom" if pos == "above" else "top"
        price_axis.annotate(
            label,
            xy=(x, price),
            xytext=(0, offset),
            textcoords="offset points",
            ha="center", va=va,
            fontsize=8, fontweight="bold", color=color,
            clip_on=True,
        )

    def _draw_hline(self, price_axis, ann: dict) -> None:
        price = float(ann["price"])
        color = ann.get("color", TV_TEXT_DIM)
        style = ann.get("style", "--")
        label = ann.get("label", "")
        price_axis.axhline(
            price, color=color, linestyle=style, linewidth=1.0, alpha=0.85, zorder=3,
        )
        if not label:
            return
        price_axis.annotate(
            f" {label} ",
            xy=(1.0, price),
            xycoords=("axes fraction", "data"),
            xytext=(-2, 0),
            textcoords="offset points",
            ha="right", va="center",
            fontsize=7.5, color="#ffffff",
            bbox=dict(facecolor=color, edgecolor=color, pad=1.5, boxstyle="square,pad=0.2"),
            clip_on=False,
        )

    def _draw_segment(
        self, price_axis, df: pd.DataFrame, ann: dict
    ) -> None:
        x0 = self._date_to_x(df, ann.get("start_date", ""))
        x1 = self._date_to_x(df, ann.get("end_date", ""))
        if x0 is None or x1 is None:
            return
        y0 = float(ann["start_price"])
        y1 = float(ann["end_price"])
        color = ann.get("color", TV_TEXT)
        style = ann.get("style", "-")
        width = float(ann.get("width", 1.2))
        price_axis.plot(
            [x0, x1], [y0, y1],
            color=color, linestyle=style, linewidth=width, alpha=0.9, zorder=4,
        )

    @staticmethod
    def _format_volume(value: float) -> str:
        if pd.isna(value):
            return ""
        abs_value = abs(value)
        if abs_value >= 1_000_000_000:
            return f"{value / 1_000_000_000:.1f}B"
        if abs_value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if abs_value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return f"{value:.0f}"


def _viewer_bar_time(idx) -> str:
    ts = pd.Timestamp(idx)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%d")


def _rsi_sma(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """SMA-smoothed RSI — same formula as IndicatorEngine.rsi()."""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _viewer_finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def _nearest_viewer_time(index: pd.Index, when) -> str | None:
    if when is None or len(index) == 0:
        return None
    ts = pd.Timestamp(when)
    idx_tz = getattr(index, "tz", None)
    if idx_tz is not None and ts.tzinfo is None:
        ts = ts.tz_localize(idx_tz)
    elif idx_tz is None and ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    try:
        loc = index.get_indexer([ts], method="nearest")[0]
    except Exception:
        return None
    if loc < 0:
        return None
    return _viewer_bar_time(index[loc])


def build_trade_viewer_payload(
    ohlcv_df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str = "1d",
    pattern: str | None = None,
    action: str | None = None,
    session_tz: str = "America/New_York",
    entry: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    exit_price: float | None = None,
    exit_reason: str | None = None,
    current: float | None = None,
    entry_time=None,
    exit_time=None,
) -> dict:
    """OHLCV + levels for an interactive TradingView-style viewer (no PNG)."""
    renderer = ChartRenderer(save_to_disk=False, session_tz=session_tz)
    df = renderer._prepare_df(ohlcv_df, timeframe)
    df = renderer._trim_to_visible(df, timeframe)

    candles = []
    volume = []
    rsi14 = []
    rsi = _rsi_sma(df["Close"], RSI_PERIOD)
    for idx, row in df.iterrows():
        t = _viewer_bar_time(idx)
        o = _viewer_finite(row["Open"])
        h = _viewer_finite(row["High"])
        low = _viewer_finite(row["Low"])
        c = _viewer_finite(row["Close"])
        v = _viewer_finite(row["Volume"]) or 0.0
        if None in (o, h, low, c):
            continue
        candles.append({"time": t, "open": o, "high": h, "low": low, "close": c})
        up = c >= o
        volume.append({
            "time": t,
            "value": v,
            "color": "rgba(38,166,154,0.55)" if up else "rgba(239,83,80,0.55)",
        })
        rsi_val = _viewer_finite(rsi.loc[idx])
        if rsi_val is not None:
            rsi14.append({"time": t, "value": rsi_val})

    if len(candles) < 2:
        raise ValueError(f"not enough valid bars for {symbol} {timeframe}")
    last = candles[-1]
    prev_close = candles[-2]["close"]
    change = last["close"] - prev_close
    change_pct = (change / prev_close * 100.0) if prev_close else 0.0

    from patterns.base_pattern import ANN_ENTRY, ANN_STOP, ANN_TARGET

    levels = []
    if entry is not None:
        levels.append({"price": float(entry), "title": "entry", "color": ANN_ENTRY})
    if stop is not None:
        levels.append({"price": float(stop), "title": "stop", "color": ANN_STOP})
    if target is not None:
        levels.append({"price": float(target), "title": "target", "color": ANN_TARGET})
    if exit_price is not None:
        levels.append({
            "price": float(exit_price),
            "title": exit_reason or "exit",
            "color": TV_TEXT,
        })
    elif current is not None:
        levels.append({"price": float(current), "title": "last", "color": TV_TEXT})

    markers = []
    entry_bar = _nearest_viewer_time(df.index, entry_time)
    if entry_bar and action:
        is_buy = str(action).upper() == "BUY"
        markers.append({
            "time": entry_bar,
            "position": "belowBar" if is_buy else "aboveBar",
            "color": ANN_ENTRY,
            "shape": "arrowUp" if is_buy else "arrowDown",
            "text": str(action).upper(),
        })
    exit_bar = _nearest_viewer_time(df.index, exit_time)
    if exit_bar and exit_price is not None:
        markers.append({
            "time": exit_bar,
            "position": "aboveBar",
            "color": TV_TEXT,
            "shape": "circle",
            "text": exit_reason or "exit",
        })

    title = f"{symbol} {renderer._tv_timeframe_label(timeframe)}"
    if pattern:
        title = f"{title} · {pattern}"
    if action:
        title = f"{title} · {action}"
    return {
        "title": title,
        "symbol": symbol,
        "timeframe": timeframe,
        "timeframe_label": renderer._tv_timeframe_label(timeframe),
        "pattern": pattern,
        "action": action,
        "ohlc": {
            "open": last["open"],
            "high": last["high"],
            "low": last["low"],
            "close": last["close"],
            "change": change,
            "change_pct": change_pct,
        },
        "candles": candles,
        "volume": volume,
        "rsi14": rsi14,
        "levels": levels,
        "markers": markers,
    }


def trade_level_annotations(
    *,
    entry: float,
    stop: float | None = None,
    target: float | None = None,
    exit_price: float | None = None,
    exit_reason: str | None = None,
    current: float | None = None,
) -> list[dict]:
    """Hlines for an open/closed paper trade — used by on-click chart render."""
    from patterns.base_pattern import ANN_ENTRY, ANN_STOP, ANN_TARGET

    anns: list[dict] = [
        {"type": "hline", "price": float(entry), "label": "entry",
         "color": ANN_ENTRY, "style": "--"},
    ]
    if stop is not None:
        anns.append({
            "type": "hline", "price": float(stop), "label": "stop",
            "color": ANN_STOP, "style": "--",
        })
    if target is not None:
        anns.append({
            "type": "hline", "price": float(target), "label": "target",
            "color": ANN_TARGET, "style": "--",
        })
    if exit_price is not None:
        anns.append({
            "type": "hline", "price": float(exit_price),
            "label": exit_reason or "exit",
            "color": TV_TEXT, "style": ":",
        })
    elif current is not None:
        anns.append({
            "type": "hline", "price": float(current), "label": "last",
            "color": TV_TEXT, "style": ":",
        })
    return anns
