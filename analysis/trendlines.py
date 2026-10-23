"""Pivot-anchored automatic trend lines for chart overlays.

A rail is drawn through two confirmed swing points and is only extended when
the projection stays close to price without cutting through candle bodies. If
its anchors are stale, the rail is refit on the recent regime; if that also
fails, the rail is dropped rather than projected through unrelated price
action. Support and resistance are selected independently.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRENDLINE_COLOR = "#42a5f5"
PIVOT_LOOKBACK = 3
ATR_TOLERANCE = 0.4
MIN_SPAN_FRACTION = 0.08
MAX_WICK_PENETRATION_RATIO = 0.20
MAX_BODY_PENETRATION_RATIO = 0.10
MAX_END_DISTANCE_FRACTION = 0.12
MAX_END_DISTANCE_ATR = 3.0
MAX_STALE_BARS = 20
RECENT_BARS = 120
EXTENSION_MAX_ATR = 2.0
EXTENSION_WRONG_SIDE_MAX = 0.20
MIN_CHANNEL_OVERLAP = 10
CLOSE_PAIR_ATR = 0.5
BROKEN_SIDE_ATR = 0.25


def _column(df: pd.DataFrame, *names: str) -> np.ndarray:
    for name in names:
        if name in df.columns:
            return pd.to_numeric(df[name], errors="coerce").to_numpy(dtype=float)
    raise ValueError(f"trend lines need one of the columns: {', '.join(names)}")


def _pivots(values: np.ndarray, left: int, right: int, high: bool) -> list[tuple[int, float]]:
    """Swing extremes: the bar is the highest/lowest in its surrounding window."""
    points: list[tuple[int, float]] = []
    for i in range(left, len(values) - right):
        value = values[i]
        window = values[i - left:i + right + 1]
        if not np.isfinite(value) or not np.isfinite(window).any():
            continue
        edge = np.nanmax(window) if high else np.nanmin(window)
        if (high and value >= edge) or (not high and value <= edge):
            # A flat top/bottom spans adjacent bars with the same extreme;
            # counting each bar would falsely confirm the line more than once.
            if points and i - points[-1][0] <= 1 and value == points[-1][1]:
                continue
            points.append((i, float(value)))
    return points


def _atr(df: pd.DataFrame) -> float:
    high = _column(df, "High", "high")
    low = _column(df, "Low", "low")
    close = _column(df, "Close", "close")
    if len(close) < 2:
        return 0.0
    prev = close[:-1]
    true_range = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - prev), np.abs(low[1:] - prev)),
    )
    true_range = true_range[np.isfinite(true_range)]
    return float(np.median(true_range)) if len(true_range) else 0.0


def _line_value(index: int, i0: int, p0: float, slope: float) -> float:
    return p0 + slope * (index - i0)


def _evaluate(
    points: list[tuple[int, float]],
    i0: int,
    p0: float,
    i1: int,
    p1: float,
    bars: np.ndarray,
    bodies: np.ndarray,
    high: bool,
    tol: float,
    n: int,
    last_close: float,
    end_tolerance: float,
    atr: float,
) -> dict | None:
    """Score a candidate line: confirming touches versus wick/body violations."""
    slope = (p1 - p0) / (i1 - i0)
    low_bound = np.nanmin(bars) * 0.85
    high_bound = np.nanmax(bars) * 1.15
    for i in (i0, i1, n - 1):
        value = _line_value(i, i0, p0, slope)
        if not np.isfinite(value) or value < low_bound or value > high_bound:
            return None
    # Score the full projected rail: a rail whose projection drifts away from
    # today's price belongs to an older regime.
    end_value = _line_value(n - 1, i0, p0, slope)
    if abs(end_value - last_close) > end_tolerance:
        return None

    touches = [i0, i1]
    violations = 0
    violation_weight = 0.0
    unit = atr if atr > 0 else max(tol, 1e-9)
    for j, price in points:
        # The rail only exists from its first anchor onward; earlier swings
        # cannot confirm or violate it.
        if j < i0 or j in (i0, i1):
            continue
        delta = price - _line_value(j, i0, p0, slope)
        if abs(delta) <= tol:
            touches.append(j)
        elif (delta > tol) if high else (delta < -tol):
            violations += 1
            # A pivot 3 ATRs past the rail is a regime break, not a wick.
            violation_weight += (abs(delta) - tol) / unit

    span_bars = max(1, n - i0)
    wick_penetrated = 0
    body_penetrated = 0
    for i in range(i0, n):
        value = _line_value(i, i0, p0, slope)
        if (bars[i] > value + tol) if high else (bars[i] < value - tol):
            wick_penetrated += 1
        if (bodies[i] > value + tol) if high else (bodies[i] < value - tol):
            body_penetrated += 1
    wick_ratio = wick_penetrated / span_bars
    body_ratio = body_penetrated / span_bars
    # A rail repeatedly cut by candle bodies is not a usable boundary. Wicks
    # may pierce it occasionally, but bodies should not.
    if wick_ratio > MAX_WICK_PENETRATION_RATIO or body_ratio > MAX_BODY_PENETRATION_RATIO:
        return None

    span = i1 - i0
    score = len(touches) * 3.0 - violations - violation_weight * 2.0
    score -= wick_ratio * 15.0 + body_ratio * 30.0
    score += 1.5 * (i1 / n) + 1.0 * (span / n)
    return {
        "score": score,
        "slope": slope,
        "touches": touches,
        "violations": violations,
        "violation_weight": violation_weight,
        "wick_penetration": wick_ratio,
        "body_penetration": body_ratio,
        "span": span,
    }


def _best_line(
    points: list[tuple[int, float]],
    bars: np.ndarray,
    bodies: np.ndarray,
    high: bool,
    tol: float,
    n: int,
    min_span: int,
    last_close: float,
    end_tolerance: float,
    atr: float,
    start_floor: int = 0,
) -> dict | None:
    best: dict | None = None
    for a in range(len(points)):
        i0, p0 = points[a]
        if i0 < start_floor:
            continue
        for b in range(a + 1, len(points)):
            i1, p1 = points[b]
            if i1 - i0 < min_span:
                continue
            candidate = _evaluate(
                points, i0, p0, i1, p1, bars, bodies, high, tol, n,
                last_close, end_tolerance, atr,
            )
            if candidate is None:
                continue
            if best is None or candidate["score"] > best["score"]:
                best = {**candidate, "i0": i0, "p0": p0, "i1": i1, "p1": p1}
    return best


def _can_extend(
    best: dict,
    touches: list[int],
    bodies: np.ndarray,
    high: bool,
    tol: float,
    n: int,
    last_close: float,
    atr: float,
) -> bool:
    """Only draw past the last touch across clean, nearby price action."""
    end = max(touches)
    if end >= n - 1:
        return True
    slope = best["slope"]
    i0, p0 = best["i0"], best["p0"]
    projected = _line_value(n - 1, i0, p0, slope)
    if abs(projected - last_close) > EXTENSION_MAX_ATR * atr:
        return False
    wrong = 0
    total = n - 1 - end
    for i in range(end + 1, n):
        value = _line_value(i, i0, p0, slope)
        if (bodies[i] > value + tol) if high else (bodies[i] < value - tol):
            wrong += 1
    return wrong / max(1, total) <= EXTENSION_WRONG_SIDE_MAX


def _select_line(
    points: list[tuple[int, float]],
    bars: np.ndarray,
    bodies: np.ndarray,
    high: bool,
    tol: float,
    n: int,
    min_span: int,
    last_close: float,
    end_tolerance: float,
    atr: float,
) -> tuple[dict, list[int], int] | None:
    """Pick a rail, extending or refitting it when its anchors are stale."""
    best = _best_line(
        points, bars, bodies, high, tol, n, min_span,
        last_close, end_tolerance, atr,
    )
    if best is None:
        return None
    touches = sorted(best["touches"])
    end = touches[-1]
    if n - 1 - end <= MAX_STALE_BARS:
        return best, touches, end
    if _can_extend(best, touches, bodies, high, tol, n, last_close, atr):
        return best, touches, n - 1

    recent = _best_line(
        points, bars, bodies, high, tol, n, min_span,
        last_close, end_tolerance, atr,
        start_floor=max(0, n - RECENT_BARS),
    )
    if recent is not None:
        recent_touches = sorted(recent["touches"])
        recent_end = recent_touches[-1]
        if n - 1 - recent_end <= MAX_STALE_BARS:
            return recent, recent_touches, recent_end
        if _can_extend(recent, recent_touches, bodies, high, tol, n, last_close, atr):
            return recent, recent_touches, n - 1
    # No current rail: drop it rather than project an old regime.
    return None


def _r2_against_line(
    points: list[tuple[int, float]],
    touches: list[int],
    i0: int,
    p0: float,
    slope: float,
) -> float:
    wanted = set(touches)
    xs: list[float] = []
    ys: list[float] = []
    for index, price in points:
        if index not in wanted:
            continue
        xs.append(float(index))
        ys.append(price)
    if len(ys) < 2:
        return 1.0
    predicted = np.array([_line_value(int(x), i0, p0, slope) for x in xs])
    actual = np.array(ys)
    ss_res = float(np.sum((actual - predicted) ** 2))
    ss_tot = float(np.sum((actual - actual.mean()) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0


def _line_at(line: dict, index: int) -> float:
    start, end = line["start_index"], line["end_index"]
    if end == start:
        return line["start_price"]
    slope = (line["end_price"] - line["start_price"]) / (end - start)
    return line["start_price"] + slope * (index - start)


def detect_trendlines(
    df: pd.DataFrame,
    *,
    lookback: int = PIVOT_LOOKBACK,
    left: int | None = None,
    right: int | None = None,
    tolerance: float | None = None,
) -> list[dict]:
    """Fit support and resistance lines through confirmed swing pivots.

    Each rail is clipped to its last confirming pivot, extended only across
    clean nearby price action, and refit on the recent regime when stale.
    Lines are independent, so a channel is never forced.
    """
    if len(df) < lookback * 2 + 3:
        return []
    left = lookback if left is None else left
    right = lookback if right is None else right
    n = len(df)
    highs = _column(df, "High", "high")
    lows = _column(df, "Low", "low")
    close = _column(df, "Close", "close")
    opens = _column(df, "Open", "open")
    atr = _atr(df)
    last_close = float(close[-1]) if np.isfinite(close[-1]) else float(np.nanmedian(close))
    if tolerance is None:
        scale = float(np.nanmedian(close)) * 0.002 if np.isfinite(close).any() else 0.0
        tolerance = max(atr * ATR_TOLERANCE, scale)
    end_tolerance = max(last_close * MAX_END_DISTANCE_FRACTION, atr * MAX_END_DISTANCE_ATR)
    min_span = max(6, int(n * MIN_SPAN_FRACTION))

    lines: list[dict] = []
    for kind, bars, high in (("support", lows, False), ("resistance", highs, True)):
        points = _pivots(bars, left, right, high)
        bodies = np.maximum(opens, close) if high else np.minimum(opens, close)
        selected = _select_line(
            points, bars, bodies, high, tolerance, n, min_span,
            last_close, end_tolerance, atr,
        )
        if selected is None:
            continue
        best, touches, end = selected
        slope = best["slope"]
        start = touches[0]
        start_price = _line_value(start, best["i0"], best["p0"], slope)
        end_price = _line_value(end, best["i0"], best["p0"], slope)
        broken = (
            kind == "support" and end_price > last_close + BROKEN_SIDE_ATR * atr
        ) or (
            kind == "resistance" and end_price < last_close - BROKEN_SIDE_ATR * atr
        )
        lines.append({
            "kind": kind,
            "start_index": start,
            "end_index": end,
            "start_price": start_price,
            "end_price": end_price,
            "slope": slope,
            "r2": _r2_against_line(points, touches, best["i0"], best["p0"], slope),
            "pivots": len(touches),
            "violations": best["violations"],
            "body_penetration": best["body_penetration"],
            "stale_bars": int(n - 1 - end),
            "broken": bool(broken),
            "confirmed": len(touches) >= 3,
            "color": TRENDLINE_COLOR,
            "channel": "single",
            "close_pair": False,
            "crosses": False,
        })

    if len(lines) == 2:
        support = next(line for line in lines if line["kind"] == "support")
        resistance = next(line for line in lines if line["kind"] == "resistance")
        start = max(support["start_index"], resistance["start_index"])
        end = min(support["end_index"], resistance["end_index"])
        overlap = end - start + 1
        if overlap <= 0:
            # The two rails describe different regimes; keep the current one.
            lines = [max(lines, key=lambda line: line["end_index"])]
        elif overlap < MIN_CHANNEL_OVERLAP:
            # Both rails are current enough to keep, but they barely overlap;
            # they are independent lines, not a channel shape.
            for line in lines:
                line["channel"] = "short overlap"
                line["close_pair"] = False
                line["crosses"] = False
                line["overlap_bars"] = overlap
        else:
            gaps = [
                _line_at(resistance, i) - _line_at(support, i)
                for i in range(start, end + 1)
            ]
            first, last = gaps[0], gaps[-1]
            if last < first * 0.85:
                shape = "converging"
            elif last > first * 1.15:
                shape = "diverging"
            else:
                shape = "parallel"
            close_pair = abs(last) < CLOSE_PAIR_ATR * atr
            crosses = any(gap <= 0 for gap in gaps)
            for line in lines:
                line["channel"] = shape
                line["close_pair"] = bool(close_pair)
                line["crosses"] = bool(crosses)
                line["overlap_bars"] = overlap

    return lines
