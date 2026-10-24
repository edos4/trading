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
    """Swing extremes confirmed on both sides of the entire flat-price run."""
    points: list[tuple[int, float]] = []
    i = 0
    while i < len(values):
        value = values[i]
        end = i
        while end + 1 < len(values) and values[end + 1] == value:
            end += 1
        if i >= left and end + right < len(values) and np.isfinite(value):
            before = values[i - left:i]
            after = values[end + 1:end + right + 1]
            if np.isfinite(before).all() and np.isfinite(after).all():
                edge = max(np.max(before), np.max(after)) if high else min(np.min(before), np.min(after))
                if (high and value >= edge) or (not high and value <= edge):
                    # Interior bars of a long plateau are not separate swings,
                    # and a plateau needs the reversal after its final bar.
                    points.append((i, float(value)))
        i = end + 1
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


def _plateau_endpoints(values: np.ndarray, points: list[tuple[int, float]]) -> dict[int, int]:
    """The final bar of each confirmed equal-price swing run."""
    ends = {}
    for start, price in points:
        end = start
        while end + 1 < len(values) and values[end + 1] == price:
            end += 1
        ends[start] = end
    return ends


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
    last_touch = max(touches)
    wick_penetrated = 0
    body_penetrated = 0
    anchor_wick_penetrated = 0
    anchor_body_penetrated = 0
    for i in range(i0, n):
        value = _line_value(i, i0, p0, slope)
        if (bars[i] > value + tol) if high else (bars[i] < value - tol):
            wick_penetrated += 1
            if i <= last_touch:
                anchor_wick_penetrated += 1
        if (bodies[i] > value + tol) if high else (bodies[i] < value - tol):
            body_penetrated += 1
            if i <= last_touch:
                anchor_body_penetrated += 1
    wick_ratio = wick_penetrated / span_bars
    body_ratio = body_penetrated / span_bars
    anchor_bars = last_touch - i0 + 1
    anchor_wick_ratio = anchor_wick_penetrated / anchor_bars
    anchor_body_ratio = anchor_body_penetrated / anchor_bars
    # A rail repeatedly cut by candle bodies is not a usable boundary. Wicks
    # may pierce it occasionally, but bodies should not.
    # Also check the actual anchored segment: later clean bars must not dilute
    # violations in the shorter segment that will be drawn when clipped.
    if (
        max(wick_ratio, anchor_wick_ratio) > MAX_WICK_PENETRATION_RATIO
        or max(body_ratio, anchor_body_ratio) > MAX_BODY_PENETRATION_RATIO
    ):
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
        "anchor_body_penetration": anchor_body_ratio,
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
    require_current: bool = False,
    plateau_ends: dict[int, int] | None = None,
) -> dict | None:
    best: dict | None = None
    last_points = [(plateau_ends.get(i, i), price) for i, price in points] if plateau_ends else points
    for a in range(len(points)):
        for b in range(a + 1, len(points)):
            p0, p1 = points[a][1], points[b][1]
            # Use the edge that leaves the full plateau on the correct side:
            # rising support and falling resistance need the final flat bar.
            # Resolve every touch the same way so a plateau still counts once.
            # Horizontal touches use final bars for recency, while the first
            # anchor stays at its earliest edge to preserve the full span.
            use_last = (p1 <= p0) if high else (p1 >= p0)
            candidate_points = last_points if use_last else points
            if p1 == p0 and plateau_ends:
                origin = max(points[a][0], start_floor)
                if origin > plateau_ends.get(points[a][0], points[a][0]):
                    continue
                candidate_points = list(last_points)
                # A plateau crossing the refit floor remains a valid origin;
                # retain all of its clean horizontal span inside that window.
                candidate_points[a] = (origin, p0)
            i0, p0 = candidate_points[a]
            i1, p1 = candidate_points[b]
            if i0 < start_floor or i1 - i0 < min_span:
                continue
            candidate = _evaluate(
                candidate_points, i0, p0, i1, p1, bars, bodies, high, tol, n,
                last_close, end_tolerance, atr,
            )
            if candidate is None:
                continue
            candidate = {**candidate, "i0": i0, "p0": p0, "i1": i1, "p1": p1}
            if (
                require_current
                and n - 1 - max(candidate["touches"]) > MAX_STALE_BARS
                and not _can_extend(
                    candidate, candidate["touches"], bars, bodies, high, tol, n, last_close, atr,
                )
            ):
                # A stale high-scoring rail must not hide a lower-scoring
                # current rail during the refit search.
                continue
            if best is None or candidate["score"] > best["score"]:
                best = candidate
    return best


def _can_extend(
    best: dict,
    touches: list[int],
    bars: np.ndarray,
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
    wick_wrong = 0
    total = n - 1 - end
    for i in range(end + 1, n):
        value = _line_value(i, i0, p0, slope)
        if (bodies[i] > value + tol) if high else (bodies[i] < value - tol):
            wrong += 1
        if (bars[i] > value + tol) if high else (bars[i] < value - tol):
            wick_wrong += 1
    return (
        wrong / total <= EXTENSION_WRONG_SIDE_MAX
        and wick_wrong / total <= MAX_WICK_PENETRATION_RATIO
    )


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
    plateau_ends: dict[int, int] | None = None,
) -> tuple[dict, list[int], int] | None:
    """Pick a rail, extending or refitting it when its anchors are stale."""
    best = _best_line(
        points, bars, bodies, high, tol, n, min_span,
        last_close, end_tolerance, atr,
        # Filter unusable stale rails before ranking. A valid current rail
        # may still need its first anchor before the recent refit window.
        require_current=True,
        plateau_ends=plateau_ends,
    )
    recent_min_span = min(min_span, max(6, int(min(n, RECENT_BARS) * MIN_SPAN_FRACTION)))
    if best is not None:
        touches = sorted(best["touches"])
        end = touches[-1]
        if n - 1 - end <= MAX_STALE_BARS:
            return best, touches, end
        if _can_extend(best, touches, bars, bodies, high, tol, n, last_close, atr):
            return best, touches, n - 1
    elif recent_min_span == min_span:
        # Searching a subset with the same span cannot rescue a missing fit.
        return None

    # An old trade can keep thousands of bars in view. The recent search needs
    # a span relative to its own window, even when no full-window fit exists.
    recent = _best_line(
        points, bars, bodies, high, tol, n, recent_min_span,
        last_close, end_tolerance, atr,
        start_floor=max(0, n - RECENT_BARS),
        require_current=True,
        plateau_ends=plateau_ends,
    )
    if recent is not None:
        recent_touches = sorted(recent["touches"])
        recent_end = recent_touches[-1]
        if n - 1 - recent_end <= MAX_STALE_BARS:
            return recent, recent_touches, recent_end
        if _can_extend(recent, recent_touches, bars, bodies, high, tol, n, last_close, atr):
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
    Lines are independent, so a channel is never forced. Incomplete or
    inconsistent OHLC frames cannot validate a rail and return no lines.
    """
    left = lookback if left is None else left
    right = lookback if right is None else right
    if left < 1 or right < 1:
        raise ValueError("trend line pivot windows must be positive")
    if tolerance is not None and (not np.isfinite(tolerance) or tolerance < 0):
        raise ValueError("trend line tolerance must be finite and nonnegative")
    if len(df) < left + right + 3:
        return []
    n = len(df)
    highs = _column(df, "High", "high")
    lows = _column(df, "Low", "low")
    close = _column(df, "Close", "close")
    opens = _column(df, "Open", "open")
    if not np.isfinite(np.column_stack((highs, lows, close, opens))).all():
        return []
    if np.any(lows > np.minimum(opens, close)) or np.any(highs < np.maximum(opens, close)):
        return []
    atr = _atr(df)
    last_close = float(close[-1])
    if tolerance is None:
        scale = float(np.median(close)) * 0.002
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
            plateau_ends=_plateau_endpoints(bars, points),
        )
        if selected is None:
            continue
        best, touches, end = selected
        slope = best["slope"]
        start = touches[0]
        start_price = _line_value(start, best["i0"], best["p0"], slope)
        end_price = _line_value(end, best["i0"], best["p0"], slope)
        current_price = _line_value(n - 1, best["i0"], best["p0"], slope)
        broken = (
            kind == "support" and current_price > last_close + BROKEN_SIDE_ATR * atr
        ) or (
            kind == "resistance" and current_price < last_close - BROKEN_SIDE_ATR * atr
        )
        lines.append({
            "kind": kind,
            "start_index": start,
            "end_index": end,
            "start_price": start_price,
            "end_price": end_price,
            "slope": slope,
            "r2": _r2_against_line(
                [(i, float(bars[i])) for i in touches], touches, best["i0"], best["p0"], slope,
            ),
            "pivots": len(touches),
            "violations": best["violations"],
            "body_penetration": (
                best["body_penetration"] if end == n - 1 else best["anchor_body_penetration"]
            ),
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
