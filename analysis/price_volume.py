"""
analysis/price_volume.py — Price-volume confirmation gate for pattern signals.

Does NOT generate entries. After a chart pattern emits BUY/SELL, this gate
checks relative volume (RVOL) and OBV slope and only lets the signal through
when volume confirms the direction.

Fail-open when history is too short (<20 bars) so thin/missing volume data
cannot freeze the scanner or backtester.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from config import settings
from data.ohlcv_store import OHLCVStore
from patterns.base_pattern import TradeSignal

_RVOL_LOOKBACK = 20


@dataclass(frozen=True)
class VolumeVerdict:
    passed: bool
    reason: str = ""
    rvol: float | None = None
    obv_slope: float | None = None


def relative_volume(
    ind: IndicatorEngine,
    lookback: int = _RVOL_LOOKBACK,
    *,
    bar_idx: int = -1,
) -> float | None:
    """Signal-bar volume / SMA(volume, lookback). None if insufficient data."""
    vol = ind.volume
    if len(vol) < lookback:
        return None
    # Exclude the signal bar from the baseline average (standard RVOL).
    end = len(vol) if bar_idx == -1 else bar_idx + 1
    if end < lookback:
        return None
    signal_vol = float(vol.iloc[end - 1])
    baseline = vol.iloc[end - lookback : end - 1]
    avg = float(baseline.mean())
    if not np.isfinite(avg) or avg <= 0:
        return None
    if not np.isfinite(signal_vol):
        return None
    return signal_vol / avg


def obv_slope(
    ind: IndicatorEngine,
    bars: int = 5,
    *,
    bar_idx: int = -1,
) -> float | None:
    """Average per-bar change in OBV over the last `bars` bars.

    Positive → accumulation; negative → distribution.
    """
    if bars < 1:
        return None
    obv = ind.obv()
    end = len(obv) if bar_idx == -1 else bar_idx + 1
    if end <= bars:
        return None
    start = end - bars - 1
    # Need bars+1 points to get `bars` steps of slope.
    window = obv.iloc[start:end]
    if len(window) < bars + 1:
        return None
    first = float(window.iloc[0])
    last = float(window.iloc[-1])
    if not np.isfinite(first) or not np.isfinite(last):
        return None
    return (last - first) / bars


def compute_volume_metrics(
    store: OHLCVStore,
    symbol: str,
    timeframe: str,
    *,
    obv_bars: int | None = None,
) -> tuple[float | None, float | None]:
    """Return (rvol, obv_slope) for the latest bar, or (None, None)."""
    if obv_bars is None:
        obv_bars = settings.volume_gate_obv_bars
    df = store.get_df(symbol, timeframe, min_bars=1)
    if df is None or len(df) < _RVOL_LOOKBACK:
        return None, None
    ind = IndicatorEngine(df)
    return relative_volume(ind), obv_slope(ind, bars=obv_bars)


def volume_confirm_gate(
    signal: TradeSignal,
    store: OHLCVStore,
    *,
    rvol_min: float | None = None,
    obv_bars: int | None = None,
) -> VolumeVerdict:
    """Return whether `signal` clears the RVOL + OBV direction filter.

    Also mutates `signal.rvol` / `signal.obv_slope` when metrics are available
    so accepted trades can be tagged for A/B post-analysis.
    """
    if rvol_min is None:
        rvol_min = settings.volume_gate_rvol_min
    if obv_bars is None:
        obv_bars = settings.volume_gate_obv_bars

    if signal.action == "CLOSE":
        return VolumeVerdict(passed=True, reason="skipped")

    df = store.get_df(signal.symbol, signal.timeframe, min_bars=1)
    if df is None or len(df) < _RVOL_LOOKBACK:
        return VolumeVerdict(
            passed=True,
            reason="insufficient bars (fail-open)",
        )

    ind = IndicatorEngine(df)
    rvol = relative_volume(ind)
    slope = obv_slope(ind, bars=obv_bars)
    signal.rvol = rvol
    signal.obv_slope = slope

    if rvol is None:
        return VolumeVerdict(
            passed=True,
            reason="rvol unavailable (fail-open)",
            rvol=rvol,
            obv_slope=slope,
        )

    if rvol < rvol_min:
        return VolumeVerdict(
            passed=False,
            reason=f"rvol={rvol:.2f} < {rvol_min}",
            rvol=rvol,
            obv_slope=slope,
        )

    if slope is None:
        return VolumeVerdict(
            passed=True,
            reason=f"rvol={rvol:.2f} ok; obv unavailable (fail-open)",
            rvol=rvol,
            obv_slope=slope,
        )

    if signal.action == "BUY" and slope < 0:
        return VolumeVerdict(
            passed=False,
            reason=f"BUY but OBV slope={slope:.0f} < 0 (rvol={rvol:.2f})",
            rvol=rvol,
            obv_slope=slope,
        )
    if signal.action == "SELL" and slope > 0:
        return VolumeVerdict(
            passed=False,
            reason=f"SELL but OBV slope={slope:.0f} > 0 (rvol={rvol:.2f})",
            rvol=rvol,
            obv_slope=slope,
        )

    return VolumeVerdict(
        passed=True,
        reason=f"rvol={rvol:.2f} ≥ {rvol_min}; obv_slope={slope:.0f}",
        rvol=rvol,
        obv_slope=slope,
    )


def ab_metrics_from_result(result) -> dict:
    """Compact side-by-side stats for volume-gate A/B compare."""
    from core.backtester import trade_r_multiple

    r_values = [
        r for t in result.trades
        if (r := trade_r_multiple(t, t.exit_price)) is not None
    ]
    pf = result.profit_factor
    return {
        "trades": len(result.trades),
        "win_rate": round(result.win_rate, 4),
        "avg_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
        "expectancy_pct": round(result.expectancy_pct, 4),
        "profit_factor": round(pf, 4) if pf != float("inf") else None,
        "max_drawdown_pct": round(result.max_drawdown_pct, 4),
        "account_weighted_pnl_pct": round(result.account_weighted_pnl_pct, 4),
        "total_signals": result.total_signals,
    }
