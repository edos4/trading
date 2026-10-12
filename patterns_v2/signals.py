"""Turn detector breakouts into this bot's TradeSignal.

``detect()`` reports the breakout bar. This bot fills on that bar's close, the
same signal-bar entry the scanner uses for the built-in patterns. Stop is the
price that cancels the pattern: the failure pivots for reversals, or the
unbroken rail on the breakout bar for flags, pennants, triangles, wedges,
channels, and rectangles. Target uses the full measured move (pole, bowl, or
pattern width) from the fill. A signal whose reward does not cover the stop
(under 1R) is dropped; a full-depth reversal stop can therefore reject a break.
"""

from __future__ import annotations

import math

import pandas as pd

from patterns.base_pattern import (
    ANN_ENTRY,
    ANN_LINE,
    ANN_PATTERN,
    ANN_STOP,
    ANN_TARGET,
    TradeSignal,
    ann_hline,
    ann_marker,
    ann_segment,
)
from patterns_v2.loader import detect_fn

# detect() returns nothing on a shorter series.
MIN_BARS = 60

# Placeholder so a Patternsv2 paper scan still walks daily bars.
class Patternsv2Scan:
    name = "patternsv2"
    timeframes = ["1d"]
    MIN_BARS = MIN_BARS

    def on_start(self) -> None:
        return None

    def on_stop(self) -> None:
        return None


def _bar_date(df: pd.DataFrame, idx: int) -> str:
    ts = df.index[int(idx)]
    try:
        return ts.strftime("%Y-%m-%d")
    except AttributeError:
        return str(ts)[:10]


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


# Failure price for reversal patterns: the pivot the detector already uses as
# the cancel floor. Geometry patterns use the unbroken rail at the breakout
# bar instead (see _opposite_rail).
_FAILURE_PIVOTS = {
    "double_bottom": ("low 1", "low 2"),
    "double_top": ("high 1", "high 2"),
    "cup_handle": ("handle",),
    "inv_cup_handle": ("handle",),
    "inv_head_shoulders": ("head",),
    "head_shoulders": ("head",),
}


def _point_prices(raw: dict, labels: tuple[str, ...]) -> list[float]:
    wanted = set(labels)
    prices = []
    for point in raw.get("points") or ():
        if len(point) < 3 or str(point[2]) not in wanted:
            continue
        price = _finite(point[1])
        if price is not None and price > 0:
            prices.append(price)
    return prices


def _opposite_rail(raw: dict, level: float, direction: int) -> float | None:
    """Unbroken boundary where the signal bar sits.

    A flag's pole, a cup's rim, and a channel's early pivots are not the stop.
    The stop is the other rail's price on the breakout bar.
    """
    signal = int(raw.get("signal") or -1)
    ends: list[float] = []
    for line in raw.get("lines") or ():
        if len(line) < 4:
            continue
        y1 = _finite(line[3])
        if y1 is None or y1 <= 0:
            continue
        try:
            x1 = int(round(float(line[2])))
        except (TypeError, ValueError):
            continue
        if x1 == signal:
            ends.append(y1)
    if direction > 0:
        below = [y for y in ends if y < level]
        return min(below) if below else None
    above = [y for y in ends if y > level]
    return max(above) if above else None


def _failure_price(raw: dict, level: float, direction: int) -> float | None:
    name = str(raw.get("pattern") or "")
    labels = _FAILURE_PIVOTS.get(name)
    if labels:
        prices = _point_prices(raw, labels)
        if not prices:
            return None
        return min(prices) if direction > 0 else max(prices)
    return _opposite_rail(raw, level, direction)


def _measured_height(raw: dict, level: float, extreme: float) -> float:
    """Keep a tight failure stop separate from the full pattern's objective."""
    name = str(raw.get("pattern") or "")
    if name in {"bull_flag", "bear_flag", "bull_pennant", "bear_pennant"}:
        prices = _point_prices(raw, ("pole", "pole top", "pole bottom"))
        return max(prices) - min(prices) if len(prices) >= 2 else 0.0
    if name in {"cup_handle", "inv_cup_handle"}:
        prices = _point_prices(raw, ("cup",))
        return abs(level - prices[0]) if prices else 0.0
    if name in _FAILURE_PIVOTS:
        return abs(level - extreme)
    # Converging rails are narrow by the break; their objective is the full
    # formation's width, not that final narrow distance to the failure rail.
    signal = int(raw["signal"])
    rails = [line for line in raw.get("lines") or ()
             if len(line) >= 4 and int(round(float(line[2]))) == signal]
    widths = []
    for bar in (int(raw["start"]), int(raw["end"]), signal):
        prices = []
        for x0, y0, x1, y1 in rails:
            span = float(x1) - float(x0)
            price = float(y1) if span == 0 else float(y0) + (float(y1) - float(y0)) * (bar - float(x0)) / span
            if math.isfinite(price):
                prices.append(price)
        if len(prices) >= 2:
            widths.append(max(prices) - min(prices))
    return max(widths) if widths else abs(level - extreme)


def _levels(raw: dict, entry: float) -> tuple[float | None, float | None]:
    direction = 1 if int(raw.get("direction") or 0) > 0 else -1
    level = _finite(raw.get("level"))
    if level is None or level <= 0:
        return None, None
    extreme = _failure_price(raw, level, direction)
    if extreme is None:
        return None, None
    if direction * (level - extreme) <= 0:
        return None, None
    height = _measured_height(raw, level, extreme)
    if not math.isfinite(height) or height <= 0:
        return None, None
    if direction > 0:
        stop = extreme if extreme < entry else (level if level < entry else None)
        target = entry + height
    else:
        stop = extreme if extreme > entry else (level if level > entry else None)
        target = entry - height
    if target <= 0 or stop is None or stop <= 0:
        return None, None
    risk = (entry - stop) if direction > 0 else (stop - entry)
    reward = (target - entry) if direction > 0 else (entry - target)
    if risk <= 0 or reward < risk:
        return None, None
    return stop, target


def _annotations(df: pd.DataFrame, raw: dict, entry_idx: int, entry: float, stop, target) -> list[dict]:
    n = len(df)
    out: list[dict] = []
    for line in raw.get("lines") or ():
        if len(line) < 4:
            continue
        x0 = int(round(float(line[0])))
        y0 = _finite(line[1])
        x1 = int(round(float(line[2])))
        y1 = _finite(line[3])
        if y0 is None or y1 is None or not (0 <= x0 < n and 0 <= x1 < n):
            continue
        out.append(ann_segment(_bar_date(df, x0), _bar_date(df, x1), y0, y1, ANN_PATTERN))
    for point in raw.get("points") or ():
        if len(point) < 3:
            continue
        x, y, label = int(round(float(point[0]))), _finite(point[1]), str(point[2])
        if y is None or not (0 <= x < n):
            continue
        out.append(ann_marker(_bar_date(df, x), y, label, ANN_PATTERN, "o", "above"))
    level = _finite(raw.get("level"))
    if level is not None and level > 0:
        out.append(ann_hline(level, "level", ANN_LINE))
    if stop is not None:
        out.append(ann_hline(stop, "stop", ANN_STOP))
    if target is not None:
        out.append(ann_hline(target, "target", ANN_TARGET))
    # A version id on one anchor and not the others keeps the chart renderer
    # from redrawing this geometry with the built-in pattern outlines.
    entry_mark = ann_marker(_bar_date(df, entry_idx), entry, "entry", ANN_ENTRY, "o", "below")
    entry_mark["pattern_version_id"] = "patternsv2"
    out.append(entry_mark)
    return out


def _to_signal(df: pd.DataFrame, raw: dict, *, symbol: str, timeframe: str, latest: int) -> TradeSignal | None:
    idx = int(raw.get("signal"))
    if idx < 0 or idx >= len(df):
        return None
    entry = _finite(df["close"].iloc[idx])
    if entry is None or entry <= 0:
        return None
    name = str(raw.get("pattern") or "pattern")
    direction = 1 if int(raw.get("direction") or 0) > 0 else -1
    stop, target = _levels(raw, entry)
    if stop is None or target is None:
        return None
    score = _finite(raw.get("score")) or 0.0
    age = latest - idx
    notes = f"Patternsv2 {name} score={score:.2f}"
    if age > 0:
        notes = f"{notes} | chart-scan: triggered {age} bar(s) ago"
    return TradeSignal(
        symbol=symbol,
        action="BUY" if direction > 0 else "SELL",
        pattern=name,
        timeframe=timeframe,
        confidence=float(min(1.0, max(0.0, score / 10.0))),
        price=round(entry, 4),
        qty=0.0,
        stop_loss=None if stop is None else round(stop, 4),
        take_profit=None if target is None else round(target, 4),
        setup_key=(int(raw.get("start") or 0), int(raw.get("end") or 0), idx),
        notes=notes,
        chart_annotations=_annotations(df, raw, idx, entry, stop, target),
        provenance="patternsv2",
    )


def detect_trade_signals(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    lookback: int | None = None,
    latest_bar_only: bool = False,
    detect=None,
) -> list[TradeSignal]:
    """Newest breakout per pattern. ``latest_bar_only`` keeps the current bar."""
    if df is None or len(df) < MIN_BARS:
        return []
    fn = detect or detect_fn()
    found = fn(
        df["high"].to_numpy(dtype=float),
        df["low"].to_numpy(dtype=float),
        df["close"].to_numpy(dtype=float),
    )
    n = len(df)
    latest = n - 1
    floor = 0 if lookback is None else max(0, n - int(lookback))
    best: dict[str, tuple[int, float, dict]] = {}
    for raw in found or ():
        idx = int(raw.get("signal"))
        if latest_bar_only and idx != latest:
            continue
        if idx < floor or idx > latest:
            continue
        name = str(raw.get("pattern") or "")
        score = _finite(raw.get("score")) or 0.0
        prev = best.get(name)
        if prev is None or idx > prev[0] or (idx == prev[0] and score > prev[1]):
            best[name] = (idx, score, raw)
    out: list[TradeSignal] = []
    for _idx, _score, raw in sorted(best.values(), key=lambda item: item[0]):
        signal = _to_signal(df, raw, symbol=symbol, timeframe=timeframe, latest=latest)
        if signal is not None:
            out.append(signal)
    return out
