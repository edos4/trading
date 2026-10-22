"""Catalog of indicators available to the visual rule builder.

Every entry computes locally from OHLCV (no TradingView session). The catalog
drives the editor's picker, parameter validation and warmup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from analysis.indicator_engine import IndicatorEngine


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    kind: str = "int"  # "int" | "float"
    default: float = 14
    minimum: float = 1
    maximum: float = 500


@dataclass(frozen=True)
class Indicator:
    id: str
    label: str
    group: str
    params: tuple[Param, ...]
    outputs: tuple[str, ...]
    compute: Callable[[pd.DataFrame, dict], dict[str, pd.Series]]
    warmup: Callable[[dict], int]


def _engine(df: pd.DataFrame) -> IndicatorEngine:
    return IndicatorEngine(df)


def _rma(values: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (same recursion as indicators/_math.rma)."""
    return values.ewm(alpha=1.0 / float(period), adjust=False).mean()


def _price(column: str):
    return lambda df, _p: {"value": df[column].astype(float)}


def _sma(df, p):
    return {"value": _engine(df).sma(int(p["period"]))}


def _ema(df, p):
    return {"value": df["close"].astype(float).ewm(
        span=int(p["period"]), adjust=False).mean()}


def _wma(df, p):
    return {"value": _engine(df).wma(int(p["period"]))}


def _rsi(df, p):
    return {"value": _engine(df).rsi_wilder(int(p["period"]))}


def _macd(df, p):
    line, signal, hist = _engine(df).macd(
        int(p["fast"]), int(p["slow"]), int(p["signal"]))
    return {"line": line, "signal": signal, "hist": hist}


def _stoch(df, p):
    k, d = _engine(df).stoch(int(p["k_period"]), int(p["d_period"]))
    return {"k": k, "d": d}


def _atr(df, p):
    return {"value": _engine(df).atr(int(p["period"]))}


def _bb(df, p):
    upper, mid, lower = _engine(df).bollinger_bands(
        int(p["period"]), float(p["std_dev"]))
    return {"upper": upper, "mid": mid, "lower": lower}


def _obv(df, _p):
    return {"value": _engine(df).obv()}


def _vwap(df, p):
    typical = (df["high"].astype(float) + df["low"].astype(float)
               + df["close"].astype(float)) / 3.0
    volume = df["volume"].astype(float)
    numerator = (typical * volume).rolling(int(p["period"])).sum()
    denominator = volume.rolling(int(p["period"])).sum()
    return {"value": numerator / denominator.replace(0, np.nan)}


def _rvol(df, p):
    volume = df["volume"].astype(float)
    average = volume.rolling(int(p["period"])).mean()
    return {"value": volume / average.replace(0, np.nan)}


def _pct_change(df, p):
    return {"value": df["close"].astype(float).pct_change(int(p["bars"])) * 100.0}


def _highest(df, p):
    return {"value": df["high"].astype(float).rolling(int(p["period"])).max()}


def _lowest(df, p):
    return {"value": df["low"].astype(float).rolling(int(p["period"])).min()}


def _adx(df, p):
    period = int(p["period"])
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    previous = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - previous).abs(), (low - previous).abs()], axis=1,
    ).max(axis=1)
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(
        np.where((up > down) & (up > 0), up, 0.0), index=df.index, dtype=float)
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0), down, 0.0), index=df.index, dtype=float)
    atr = _rma(tr, period)
    plus_di = 100.0 * _rma(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100.0 * _rma(minus_dm, period) / atr.replace(0, np.nan)
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    return {"value": _rma(dx, period)}


def _atr_pct(df, p):
    atr = _engine(df).atr(int(p["period"]))
    return {"value": atr / df["close"].astype(float).replace(0, np.nan) * 100.0}


def _dist_sma_atr(df, p):
    sma = _engine(df).sma(int(p["period"]))
    atr = _engine(df).atr(int(p["atr_period"]))
    return {"value": (df["close"].astype(float) - sma) / atr.replace(0, np.nan)}


PERIOD = (Param("period", "Period"),)

INDICATORS: tuple[Indicator, ...] = (
    Indicator("open", "Open", "Price", (), ("value",), _price("open"), lambda p: 1),
    Indicator("high", "High", "Price", (), ("value",), _price("high"), lambda p: 1),
    Indicator("low", "Low", "Price", (), ("value",), _price("low"), lambda p: 1),
    Indicator("close", "Close", "Price", (), ("value",), _price("close"), lambda p: 1),
    Indicator("volume", "Volume", "Price", (), ("value",), _price("volume"), lambda p: 1),
    Indicator("sma", "SMA", "Trend", PERIOD, ("value",), _sma,
              lambda p: int(p["period"])),
    Indicator("ema", "EMA", "Trend", PERIOD, ("value",), _ema,
              lambda p: int(p["period"])),
    Indicator("wma", "WMA", "Trend", PERIOD, ("value",), _wma,
              lambda p: int(p["period"])),
    Indicator("adx", "ADX", "Trend", PERIOD, ("value",), _adx,
              lambda p: 2 * int(p["period"])),
    Indicator("dist_sma_atr", "Distance from SMA (ATR)", "Trend",
              (Param("period", "SMA period", default=50),
               Param("atr_period", "ATR period", default=14)),
              ("value",), _dist_sma_atr,
              lambda p: max(int(p["period"]), int(p["atr_period"])) + 1),
    Indicator("rsi", "RSI (Wilder)", "Momentum", PERIOD, ("value",), _rsi,
              lambda p: int(p["period"]) + 1),
    Indicator("macd", "MACD", "Momentum",
              (Param("fast", "Fast", default=12, maximum=100),
               Param("slow", "Slow", default=26, maximum=200),
               Param("signal", "Signal", default=9, maximum=100)),
              ("line", "signal", "hist"), _macd,
              lambda p: int(p["slow"]) + int(p["signal"]) + 1),
    Indicator("stoch", "Stochastic", "Momentum",
              (Param("k_period", "%K period", default=14),
               Param("d_period", "%D period", default=3, maximum=100)),
              ("k", "d"), _stoch,
              lambda p: int(p["k_period"]) + int(p["d_period"]) + 1),
    Indicator("pct_change", "% change", "Momentum",
              (Param("bars", "Bars", default=5, maximum=250),),
              ("value",), _pct_change,
              lambda p: int(p["bars"]) + 1),
    Indicator("atr", "ATR", "Volatility", PERIOD, ("value",), _atr,
              lambda p: int(p["period"]) + 1),
    Indicator("atr_pct", "ATR % of price", "Volatility", PERIOD, ("value",), _atr_pct,
              lambda p: int(p["period"]) + 1),
    Indicator("bb", "Bollinger Bands", "Volatility",
              (Param("period", "Period", default=20),
               Param("std_dev", "Std dev", "float", 2.0, 0.1, 10.0)),
              ("upper", "mid", "lower"), _bb,
              lambda p: int(p["period"])),
    Indicator("highest", "Highest high", "Volatility", PERIOD, ("value",), _highest,
              lambda p: int(p["period"])),
    Indicator("lowest", "Lowest low", "Volatility", PERIOD, ("value",), _lowest,
              lambda p: int(p["period"])),
    Indicator("obv", "OBV", "Volume", (), ("value",), _obv, lambda p: 2),
    Indicator("vwap", "Rolling VWAP", "Volume", PERIOD, ("value",), _vwap,
              lambda p: int(p["period"])),
    Indicator("rvol", "Relative volume", "Volume", PERIOD, ("value",), _rvol,
              lambda p: int(p["period"]) + 1),
)

_BY_ID = {entry.id: entry for entry in INDICATORS}
GROUPS = ("Price", "Trend", "Momentum", "Volatility", "Volume")


def get_indicator(indicator_id: str) -> Indicator:
    entry = _BY_ID.get((indicator_id or "").strip())
    if entry is None:
        raise ValueError(f"Unknown indicator {indicator_id!r}")
    return entry


def resolve_params(indicator_id: str, raw: dict | None) -> dict:
    entry = get_indicator(indicator_id)
    supplied = dict(raw or {})
    known = {param.name: param for param in entry.params}
    unknown = sorted(set(supplied) - set(known))
    if unknown:
        raise ValueError(
            f"{entry.label}: unknown parameter(s) {', '.join(unknown)}")
    resolved = {}
    for name, param in known.items():
        value = supplied.get(name, param.default)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{entry.label}: {param.label} must be a number") from None
        if not np.isfinite(number):
            raise ValueError(f"{entry.label}: {param.label} must be finite")
        if number < param.minimum or number > param.maximum:
            raise ValueError(
                f"{entry.label}: {param.label} must be between "
                f"{param.minimum:g} and {param.maximum:g}")
        resolved[name] = int(number) if param.kind == "int" else number
    return resolved


def compute(indicator_id: str, df: pd.DataFrame, params: dict | None) -> dict[str, pd.Series]:
    """Compute every output of one indicator on a RangeIndex OHLCV frame."""
    entry = get_indicator(indicator_id)
    resolved = resolve_params(indicator_id, params)
    outputs = entry.compute(df.reset_index(drop=True), resolved)
    return {name: series.astype(float) for name, series in outputs.items()}


def warmup(indicator_id: str, params: dict | None) -> int:
    entry = get_indicator(indicator_id)
    return max(1, int(entry.warmup(resolve_params(indicator_id, params))))


def label(indicator_id: str, params: dict | None, field: str = "value") -> str:
    entry = get_indicator(indicator_id)
    resolved = resolve_params(indicator_id, params)
    name = entry.label
    if field and field != "value":
        name = f"{name} {field}"
    if resolved:
        args = ", ".join(f"{value:g}" if isinstance(value, float) else str(value)
                         for value in resolved.values())
        return f"{name}({args})"
    return name


def validate_rule(rule) -> None:
    """Reject unknown indicators, bad parameters and impossible output fields.

    ``RulePayload`` validates shape only; the catalog is the authority for ids,
    parameter ranges and output names. Saving, validating and constructing a
    detector all call this so a hand-written JSON body cannot bypass the editor.
    """

    def walk(node) -> None:
        if getattr(node, "kind", "") == "group" or hasattr(node, "children"):
            for child in node.children:
                walk(child)
            return
        for operand in (node.left, node.right):
            if operand.source != "indicator":
                continue
            entry = get_indicator(operand.id)
            resolve_params(operand.id, operand.params)
            field = operand.field or "value"
            if field not in entry.outputs:
                # Omitted/"value" on a multi-output indicator means its first
                # output, matching the evaluator's behaviour.
                if not (field in ("", "value") and entry.outputs):
                    raise ValueError(
                        f"{entry.label}: unknown output {operand.field!r}; "
                        f"choose one of {', '.join(entry.outputs)}")

    walk(rule.root)

    attachment = getattr(getattr(rule, "attachments", None), "patternsv2", None)
    if attachment is not None:
        pattern = (attachment.pattern or "all").strip()
        if pattern != "all":
            from patterns_v2.patterns.common import PATTERN_NAMES

            if pattern not in PATTERN_NAMES:
                raise ValueError(
                    f"Unknown Patternsv2 pattern {pattern!r}; choose all or one of "
                    f"{', '.join(sorted(PATTERN_NAMES))}")


def catalog_payload() -> list[dict]:
    return [
        {
            "id": entry.id,
            "label": entry.label,
            "group": entry.group,
            "outputs": list(entry.outputs),
            "params": [
                {
                    "name": param.name,
                    "label": param.label,
                    "kind": param.kind,
                    "default": param.default,
                    "min": param.minimum,
                    "max": param.maximum,
                }
                for param in entry.params
            ],
        }
        for entry in INDICATORS
    ]
