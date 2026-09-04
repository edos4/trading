from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from patterns._rules import extrema, local_rsi_extrema, parabolic_fit


@dataclass(frozen=True)
class RoundingSetup:
    center: int
    start: int
    entry: int
    neckline: float
    center_close: float
    center_rsi: float
    depth: float
    coefficient: float
    fit: float
    divergence: str
    target: float
    reward_pct: float


def find_rounding_setup(
    ind: IndicatorEngine,
    rsi,
    current: int,
    direction: Literal["bottom", "top"],
    lookback: int = 150,
    entry_scan: int = 120,
    shape_radius: int = 60,
) -> RoundingSetup | None:
    kind = "low" if direction == "bottom" else "high"
    centers = extrema(ind.close, kind, 2)
    for center in reversed(centers):
        if center >= current or current - center > entry_scan:
            continue
        center_close = float(ind.close.iloc[center])
        center_rsi = float(rsi.iloc[center])
        if not np.isfinite(center_rsi):
            continue
        if direction == "bottom" and center_rsi >= 45.0:
            continue
        if direction == "top" and center_rsi <= 55.0:
            continue
        start_lo = max(0, center - lookback)
        prior = ind.close.iloc[start_lo:center]
        if prior.empty:
            continue
        if direction == "bottom":
            start = start_lo + int(np.argmax(prior.to_numpy(dtype=float)))
        else:
            start = start_lo + int(np.argmin(prior.to_numpy(dtype=float)))
        neckline = float(ind.close.iloc[start])
        span = ind.close.iloc[start : center + 1]
        if direction == "bottom" and center_close != float(span.min()):
            continue
        if direction == "top" and center_close != float(span.max()):
            continue
        if neckline <= 0 or center_close <= 0:
            continue
        depth = (
            (neckline - center_close) / neckline
            if direction == "bottom"
            else (center_close - neckline) / neckline
        )
        if depth < 0.15 or depth > 0.50:
            continue
        shape = parabolic_fit(ind.close, center, shape_radius, 0.05)
        if shape is None:
            continue
        coefficient, fit = shape
        if direction == "bottom" and coefficient <= 0:
            continue
        if direction == "top" and coefficient >= 0:
            continue
        if fit < 0.70:
            continue
        entry = entry_trigger(ind, rsi, center, min(current, center + entry_scan), direction)
        if entry is None or entry != current:
            continue
        divergence = divergence_kind(ind, rsi, start, center, entry, direction)
        if divergence is None:
            continue
        entry_close = float(ind.close.iloc[entry])
        target = (
            entry_close + 0.80 * (neckline - entry_close)
            if direction == "bottom"
            else entry_close - 0.80 * (entry_close - neckline)
        )
        reward_pct = abs(target - entry_close) / entry_close
        if reward_pct < 0.23:
            continue
        return RoundingSetup(
            center,
            start,
            entry,
            neckline,
            center_close,
            center_rsi,
            depth,
            coefficient,
            fit,
            divergence,
            target,
            reward_pct,
        )
    return None


def entry_trigger(ind: IndicatorEngine, rsi, center: int, stop: int, direction: str) -> int | None:
    first: int | None = None
    for idx in range(center + 1, stop + 1):
        if not np.isfinite([float(rsi.iloc[idx - 1]), float(rsi.iloc[idx])]).all():
            first = None
            continue
        if direction == "bottom":
            continues = (
                float(ind.high.iloc[idx]) > float(ind.high.iloc[idx - 1])
                and float(ind.low.iloc[idx]) > float(ind.low.iloc[idx - 1])
                and float(rsi.iloc[idx]) > float(rsi.iloc[idx - 1])
            )
        else:
            continues = (
                float(ind.high.iloc[idx]) < float(ind.high.iloc[idx - 1])
                and float(ind.low.iloc[idx]) < float(ind.low.iloc[idx - 1])
                and float(rsi.iloc[idx]) < float(rsi.iloc[idx - 1])
            )
        if not continues:
            first = None
            continue
        if first == idx - 1:
            return idx
        first = idx
    return None


def divergence_kind(ind: IndicatorEngine, rsi, start: int, center: int, entry: int, direction: str) -> str | None:
    kind = "low" if direction == "bottom" else "high"
    points = local_rsi_extrema(rsi, start, center + 1, kind)
    for left, right in zip(points, points[1:]):
        if direction == "bottom":
            valid = float(ind.close.iloc[right]) < float(ind.close.iloc[left]) and float(rsi.iloc[right]) > float(rsi.iloc[left])
        else:
            valid = float(ind.close.iloc[right]) > float(ind.close.iloc[left]) and float(rsi.iloc[right]) < float(rsi.iloc[left])
        if valid:
            return "classic"
    if direction == "bottom":
        fallback = float(ind.close.iloc[center]) < float(ind.close.iloc[start]) and float(rsi.iloc[center]) > float(rsi.iloc[start])
    else:
        fallback = float(ind.close.iloc[center]) > float(ind.close.iloc[start]) and float(rsi.iloc[center]) < float(rsi.iloc[start])
    if fallback:
        return "overall"
    comparisons = entry - center
    if comparisons <= 0:
        return None
    if direction == "bottom":
        matching = sum(float(rsi.iloc[idx]) > float(rsi.iloc[idx - 1]) for idx in range(center + 1, entry + 1))
    else:
        matching = sum(float(rsi.iloc[idx]) < float(rsi.iloc[idx - 1]) for idx in range(center + 1, entry + 1))
    return "trend" if matching / comparisons >= 0.70 else None
