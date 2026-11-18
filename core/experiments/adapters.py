"""Reviewed, explicit per-source adapters; missing rules remain blockers.

No keyword-derived trading strategy and no execution of downloaded source
code. Runnable folders trade a labeled research book when the source never
froze an order: one fixed-notional position, next-open fill, and a 20-session
exit. That book is not the video's stop, target, or size. Additional source
adapters belong in this registry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from indicators._math import dema, ema, wilder_atr

VERSION = "experiments-v3"
RESEARCH_HOLD_BARS = 20
RESEARCH_NOTE = (
    "Research trade book: one fixed-notional position, next-open fill, exit after 20 sessions. "
    "Directional observations keep their side. Neutral components use a fresh cross or a candle-color change. "
    "This is not the source's unresolved stop, target, or size. PH does not open shorts."
)
FITNESS = {331, 350, 374, 412, 418, 429, 432, 439}


@dataclass
class Variant:
    name: str
    warmup: int
    entry: np.ndarray
    exit: np.ndarray
    evidence: Callable[[int], dict]
    kind: str = "event-study"
    policy: dict = field(default_factory=dict)


SPECS = {
    40: {
        "scope": "SPY daily 20-session breakout / 10-session low or 2.5 ATR trail",
        "kind": "trades", "markets": ["us"], "symbols": ["SPY"],
        "parameters": {"entry_window": 20, "exit_window": 10, "atr_period": 14,
                       "trail_atr": 2.5, "risk_fraction": 0.005, "exposure_cap": 1.0},
        "assumptions": ["Wilder ATR; trailing high-water mark uses completed closes.",
                        "Test the prior stop at the close, then update the trail for the next decision.",
                        "Close exits fill next open; gaps can exceed planned risk.",
                        "Whole shares, finite cash, no leverage; the first eligible date may arm an entry."],
        "limitations": ["Implements the visible SPY example; external hosted-skill automation is not included."],
    },
    406: {
        "scope": "Four source EMA panels, evaluated as separate long-only accounts",
        "kind": "trades", "markets": ["us", "ph"],
        "parameters": {"pairs": [[10, 50], [8, 21], [20, 50]], "or_panel": [10, 50, 100]},
        "assumptions": ["Daily close EMA, adjust=False seeded at the first available close.",
                        "Warm-up is five times the slowest EMA length, plus a prior bar.",
                        "Next-open fills; one position per symbol/variant, no duplicate entry while held.",
                        "The fourth panel preserves cross-up OR close above EMA100.",
                        "Independent fixed-notional positions; no invented protective stop or target."],
        "limitations": ["Each variant is a separate comparison; their capital and P&L are not pooled.",
                        "Finite portfolio cash/exposure constraints are not modeled for these panels."],
    },
    48: {
        "scope": "Completed three-candle FVG geometry and midpoint references only",
        "kind": "event-study", "markets": ["us", "ph"],
        "parameters": {"outer_bars": 3, "minimum_gap": 0, "outcome_horizons": [5, 20]},
        "assumptions": ["Daily stock candles; strict outer-wick non-overlap, no extra size filter.",
                        "Forward returns start at the next observed open and are descriptive outcomes."],
        "limitations": ["Trade entry, stop/invalidation ambiguities, retest policy and sizing remain blocked.",
                        "A geometric gap is not evidence of resting orders or guaranteed future filling."],
    },
    56: {
        "scope": "Rolling Z-score extreme and trend-context observations only",
        "kind": "event-study", "markets": ["us", "ph"],
        "parameters": {"lookback": 50, "smoothing": 3, "extreme": 2,
                       "trend_level": 1, "std_ddof": 1, "outcome_horizons": [5, 20]},
        "assumptions": ["Daily close; sample rolling standard deviation, EMA3 adjust=False.",
                        "Observe fresh threshold entries; no unverified momentum-column formula is inferred."],
        "limitations": ["Momentum-color confirmation, divergence pivots, dynamic Z-stop and trading exits remain blocked.",
                        "Z-score extremes are not calibrated reversal probabilities."],
    },
    145: {
        "scope": "Rolling Z-score baseline; mean-reversion and trend feature studies",
        "kind": "event-study", "markets": ["us", "ph"],
        "parameters": {"lookback": 50, "smoothing": 3, "extreme": 2,
                       "trend_level": 1, "std_ddof": 1, "outcome_horizons": [5, 20]},
        "assumptions": ["Daily close, sample deviation and EMA3; emit fresh threshold observations only."],
        "limitations": ["Only the defined score component runs; unspecified momentum, divergence and order policies remain blocked."],
    },
    353: {
        "scope": "20/50/100 EMA alignment observations, without invented pullback entries",
        "kind": "event-study", "markets": ["us", "ph"],
        "parameters": {"periods": [20, 50, 100], "outcome_horizons": [5, 20]},
        "assumptions": ["Daily close EMA seeded at first available close; 501-bar warm-up.",
                        "Record a fresh bullish 20>50>100 alignment; persistent alignment is not a fresh cross."],
        "limitations": ["The source's pullback confirmation, stops and exits are unspecified; trading remains blocked."],
    },
    368: {
        "scope": "Source 50/200 averages and prior-20-session Donchian observations",
        "kind": "event-study", "markets": ["us", "ph"],
        "parameters": {"averages": [50, 200], "donchian": 20, "outcome_horizons": [5, 20]},
        "assumptions": ["Simple 50/200 averages as an explicit moving-average convention.",
                        "Donchian comparison excludes the signal bar and uses a completed closing break."],
        "limitations": ["The opinion grades and other indicators are not implemented as trade rules; exits are unspecified."],
    },
}

for _number in (385, 433):
    SPECS[_number] = {
        "scope": "OHLC candle anatomy measurements only; pressure labels are not orders",
        "kind": "event-study", "markets": ["us", "ph"],
        "parameters": {"body": "abs(close-open)", "upper_wick": "high-max(open,close)",
                       "lower_wick": "min(open,close)-low"},
        "assumptions": ["Measure each completed daily candle without inventing a rejection threshold."],
        "limitations": ["No support/resistance classifier, entry, exit or forecast direction is inferred.",
                        "OHLC measurements cannot identify trader intent or future control."],
    }

for _number, _scope, _parameters, _limitations in (
    (4, "EMA34 high/close/low channel measurements only", {"ema_period":34,"sources":["high","close","low"]},
     ["Volume profile, sector data, volatility formulas and trading rules remain blocked."]),
    (25, "9/20/50 EMA chart-layout measurements only", {"ema_periods":[9,20,50]},
     ["Session VWAP requires intraday data/reset definitions; pullback trading is not implemented."]),
    (110, "Completed prior-day high/low/midpoint references only", {"reference":"previous completed daily bar"},
     ["The intraday midpoint-before-boundary sequence, candle detector and volume baseline remain blocked."]),
    (115, "Neutral OHLC candle anatomy component only", {"body":"abs(close-open)","wicks":"OHLC shadows"},
     ["Key-level selection, higher/lower-frame confirmation and engulfing trade execution remain blocked."]),
    (120, "Stochastic 5/3/3 measurements only", {"k_length":5,"k_smoothing":3,"d_smoothing":3},
     ["Trendline anchors, extreme thresholds and bounce/break trading rules remain blocked."]),
    (167, "Stochastic 5/3/3 component of the repost only", {"k_length":5,"k_smoothing":3,"d_smoothing":3},
     ["Shared tutorial with source120: not independent evidence. Trendline trading remains blocked."]),
    (184, "Writeup-defined three-candle FVG geometry only", {"outer_bars":3,"minimum_gap":0,"outcome_horizons":[5,20]},
     ["The geometry is the writeup's additional research definition, not an exact numerical video rule.",
      "Resting-order claims, retests, invalidation and trade execution remain unverified/blocked."]),
    (258, "DEMA200 measurements from the surviving lesson only", {"dema_length":200},
     ["The truncated Supertrend configuration, all entries and exits remain blocked."]),
    (311, "DEMA200 measurements only", {"dema_length":200,"source":"close"},
     ["Supertrend settings and signal-location stop are unresolved; no side-of-DEMA entry filter is invented."]),
    (394, "MACD12/26/9 measurements only", {"fast":12,"slow":26,"signal":9},
     ["VWAP session inputs, volume/candle thresholds and the combined trade policy remain blocked."]),
    (400, "Nine-period SMA context measurements only", {"sma_period":9},
     ["Active-session/volume thresholds, reversal-candle definitions and trading rules remain blocked."]),
    (414, "Bullish EMA9/21 arming-cross observations only", {"fast":9,"slow":21,"outcome_horizons":[5,20]},
     ["The crossover is explicitly not an entry. Retest/hold, expiry and risk/exit policies remain blocked."]),
    (431, "Bearish EMA50/200 context-cross observations only", {"fast":50,"slow":200,"outcome_horizons":[5,20]},
     ["BOS hierarchy, the unnamed curve, pullback entries, pyramiding and exits remain blocked."]),
):
    SPECS[_number] = {
        "scope":_scope, "kind":"event-study", "markets":["us","ph"], "parameters":_parameters,
        "assumptions":["Measure the defined component on completed daily stock bars; this does not reproduce an intraday trading sequence.",
                       "Close source where unspecified; recursive EMA/DEMA seeded at the first available close with five-period-length warm-up.",
                       "Neutral measurements have no inferred trade or forecast direction."],
        "limitations":_limitations,
    }

# Source folders may share exactly the same tested formula and execution policy.
RESEARCH_FAMILIES = (
    ((4,), "ema34-slope", "Fresh positive or negative EMA34 close slope"),
    ((25,), "ema9-20", "EMA9 crosses EMA20 in either direction"),
    ((48, 184), "outer-wick-gap", "Three-bar outer-wick gap, in the gap direction"),
    ((56, 145), "smoothed-z", "Smoothed Z enters ±2 (fade) or exceeds +1 (long trend)"),
    ((110,), "prior-range", "Fresh close above prior high or below prior low"),
    ((115, 385, 433), "candle-color", "Direction changes between successive non-doji candles"),
    ((120, 167), "stochastic-cross", "Stochastic 5/3/3 K crosses D"),
    ((258, 311), "dema200-cross", "Close crosses DEMA200"),
    ((353,), "ema-alignment", "Fresh bullish EMA20 > EMA50 > EMA100 alignment"),
    ((368,), "sma-and-donchian", "SMA50/200 crosses or prior-20 range breaks in separate books"),
    ((394,), "macd-cross", "MACD12/26 crosses its EMA9 signal"),
    ((400,), "sma9-cross", "Close crosses SMA9"),
    ((414,), "ema9-21-up", "Buy on EMA9 crossing above EMA21"),
    ((431,), "ema50-200-down", "Short on EMA50 crossing below EMA200"),
)
for numbers, family, trigger in RESEARCH_FAMILIES:
    for number in numbers:
        spec = SPECS[number]
        spec.update(kind="trades", family_id=family, measurement_id=family,
                    policy_id="research-hold20-v3", source_strategy_implemented=False,
                    tested_label=f"{trigger} · 20-session research trade")
        spec["parameters"].update(hold_bars=RESEARCH_HOLD_BARS, trigger=trigger)
        spec["limitations"].append(RESEARCH_NOTE)
for number, family in ((40, "spy-breakout"), (406, "ema-panels")):
    SPECS[number].update(family_id=family, policy_id=family + "-v3",
                         tested_label=SPECS[number]["scope"], source_strategy_implemented=True)


def definition(number: int) -> dict:
    """Catalog availability is an explicit reviewed registry, not inferred code."""
    if number in SPECS:
        return {"availability": "ready", "adapter_version": VERSION,
                "blocker": None, **SPECS[number]}
    if number in FITNESS:
        blocker = "Non-trading fitness source: no OHLCV experiment or trading rules."
    else:
        blocker = "The source does not yet supply a complete, verified daily experiment specification."
    return {"availability": "blocked", "kind": "blocked", "scope": None,
            "adapter_version": VERSION, "blocker": blocker, "parameters": {},
            "assumptions": [], "limitations": []}


def cross_above(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    result = np.zeros(len(a), dtype=bool)
    result[1:] = (a[1:] > b[1:]) & (a[:-1] <= b[:-1])
    return result


def fresh(condition: np.ndarray) -> np.ndarray:
    return condition & ~np.r_[False, condition[:-1]]


def book(name: str, warmup: int, entries, evidence: Callable[[int], dict]) -> Variant:
    """Fixed-notional research book. The 20-session exit is not a source rule."""
    signed = np.asarray(entries, dtype=int)
    return Variant(name, warmup, signed, np.zeros(len(signed), dtype=bool), evidence,
                   kind="trades", policy={"hold_bars": RESEARCH_HOLD_BARS, "research_execution": True})


def signed_cross(up: np.ndarray, down: np.ndarray) -> np.ndarray:
    entries = np.zeros(len(up), dtype=int)
    up_b, down_b = np.asarray(up, dtype=bool), np.asarray(down, dtype=bool)
    entries[up_b] = 1
    entries[down_b] = -1
    entries[up_b & down_b] = 0
    return entries


def candle_turns(open_: np.ndarray, close: np.ndarray) -> np.ndarray:
    color = np.where(close > open_, 1, np.where(close < open_, -1, 0)).astype(int)
    turns = np.zeros(len(color), dtype=int)
    prior = 0
    for i, side in enumerate(color):
        if side:
            if prior and side != prior:
                turns[i] = side
            prior = side
    return turns


def variants(number: int, df: pd.DataFrame) -> list[Variant]:
    """All rolling/recursive calculations use past data only."""
    close = df.close.to_numpy(dtype=float)
    high, low = df.high.to_numpy(dtype=float), df.low.to_numpy(dtype=float)
    empty = np.zeros(len(df), dtype=bool)
    if number in {115, 385, 433}:
        open_ = df.open.to_numpy(dtype=float)
        return [book("candle-anatomy", 0, candle_turns(open_, close),
                     lambda i: {"body": float(abs(close[i] - open_[i])),
                                "upper_wick": float(high[i] - max(close[i], open_[i])),
                                "lower_wick": float(min(close[i], open_[i]) - low[i]),
                                "range": float(high[i] - low[i]),
                                "research_side": "candle-color-change"})]
    if number in {4,25,110,120,167,258,311,394,400}:
        values, entries, warm = {}, np.zeros(len(df), dtype=int), 0
        if number == 4:
            for name in ("high","close","low"):
                line = ema(df[name].to_numpy(dtype=float),34)
                values[f"ema34_{name}"] = line
                values[f"ema34_{name}_slope"] = np.r_[np.nan,np.diff(line)]
            slope = values["ema34_close_slope"]
            finite = np.isfinite(slope)
            entries = signed_cross(fresh(finite & (slope > 0)), fresh(finite & (slope < 0)))
            warm = 171
        elif number == 25:
            values = {f"ema{n}":ema(close,n) for n in (9,20,50)}
            values["ema9_minus_20"] = values["ema9"]-values["ema20"]
            entries = signed_cross(cross_above(values["ema9"], values["ema20"]),
                                   cross_above(values["ema20"], values["ema9"]))
            warm = 251
        elif number == 110:
            prior_high,prior_low=df.high.shift(1).to_numpy(),df.low.shift(1).to_numpy()
            values={"prior_high":prior_high,"prior_low":prior_low,"prior_midpoint":(prior_high+prior_low)/2}
            entries = signed_cross(fresh(close > prior_high), fresh(close < prior_low))
            warm = 1
        elif number in {120,167}:
            lowest,highest=df.low.rolling(5).min(),df.high.rolling(5).max()
            k=(100*(df.close-lowest)/(highest-lowest).replace(0,np.nan)).rolling(3).mean()
            values={"k":k.to_numpy(),"d":k.rolling(3).mean().to_numpy()}
            entries = signed_cross(cross_above(values["k"], values["d"]),
                                   cross_above(values["d"], values["k"]))
            warm=8
        elif number in {258,311}:
            values={"dema200":dema(close,200)}
            entries = signed_cross(cross_above(close, values["dema200"]),
                                   cross_above(values["dema200"], close))
            warm=1001
        elif number == 394:
            line=ema(close,12)-ema(close,26)
            signal=ema(line,9)
            values={"macd":line,"signal":signal,"histogram":line-signal}
            entries = signed_cross(cross_above(line, signal), cross_above(signal, line))
            warm=139
        elif number == 400:
            values={"sma9":df.close.rolling(9).mean().to_numpy()}
            entries = signed_cross(cross_above(close, values["sma9"]),
                                   cross_above(values["sma9"], close))
            warm=8
        def measured(i, values=values):
            reading = {}
            for key, series in values.items():
                value = float(series[i])
                reading[key] = value if np.isfinite(value) else None
            return reading
        return [book("component-measurements", warm, entries, measured)]
    if number in {414,431}:
        fast,slow=(9,21) if number==414 else (50,200)
        a,b=ema(close,fast),ema(close,slow)
        entries=cross_above(a,b).astype(int) if number==414 else -cross_above(b,a).astype(int)
        return [book("arming-cross-only", 5*slow+1, entries,
                     lambda i: {f"ema{fast}": float(a[i]), f"ema{slow}": float(b[i]),
                                "source_calls_this_an_entry": False, "research_entry": True})]
    if number == 40:
        upper = df.high.rolling(20).max().shift(1).to_numpy()
        lower = df.low.rolling(10).min().shift(1).to_numpy()
        atr = wilder_atr(high, low, close, 14)
        return [Variant("spy-breakout", 21, (close > upper).astype(int), close < lower,
                        lambda i: {"prior_20_high": float(upper[i]), "prior_10_low": float(lower[i]),
                                   "atr14": float(atr[i])}, kind="trades",
                        policy={"finite_account": True, "risk_fraction": 0.005,
                                "trail_atr": 2.5, "atr": atr})]
    if number == 406:
        out = []
        for fast, slow, or_panel in [(10, 50, False), (8, 21, False), (20, 50, False), (10, 50, True)]:
            a, b = ema(close, fast), ema(close, slow)
            c = ema(close, 100)
            entries = cross_above(a, b) | ((close > c) if or_panel else empty)
            def evidence(i, a=a, b=b, c=c, fast=fast, slow=slow, or_panel=or_panel):
                values = {f"ema{fast}": float(a[i]), f"ema{slow}": float(b[i])}
                if or_panel:
                    values.update(ema100=float(c[i]), operator="OR", price_above_100=bool(close[i] > c[i]))
                return values
            out.append(Variant(f"ema-{fast}-{slow}" + ("-or-100" if or_panel else ""),
                               5 * (100 if or_panel else slow) + 1, entries.astype(int),
                               cross_above(b, a), evidence, kind="trades"))
        return out
    if number in {48, 184}:
        entries = np.zeros(len(df), dtype=int)
        entries[2:] = np.where(low[2:] > high[:-2], 1,
                              np.where(high[2:] < low[:-2], -1, 0))
        def evidence(i):
            if entries[i] > 0:
                lower, upper = high[i - 2], low[i]
            elif entries[i] < 0:
                lower, upper = high[i], low[i - 2]
            else:
                return {"formed_on_third_bar": False}
            return {"gap_low": float(lower), "gap_high": float(upper),
                    "midpoint": float((lower + upper) / 2), "formed_on_third_bar": True}
        return [book("fvg-geometry", 2, entries, evidence)]
    if number in {56, 145}:
        mean = df.close.rolling(50).mean()
        dev = df.close.rolling(50).std(ddof=1).replace(0, np.nan)
        z = ((df.close - mean) / dev).ewm(span=3, adjust=False).mean().where(dev.notna()).to_numpy()
        extreme = np.where(fresh(z < -2), 1, np.where(fresh(z > 2), -1, 0))
        trend = np.where(fresh(z > 1), 1, 0)
        def z_evidence(i, z=z):
            value = float(z[i])
            return {"z": value if np.isfinite(value) else None, "calibrated_probability": False}
        return [book(name, 55, side, z_evidence)
                for name, side in (("z-extremes", extreme), ("z-trend-context", trend))]
    if number == 353:
        a, b, c = (ema(close, n) for n in (20, 50, 100))
        entries = fresh((a > b) & (b > c)).astype(int)
        return [book("bullish-alignment", 501, entries,
                     lambda i: {"ema20": float(a[i]), "ema50": float(b[i]), "ema100": float(c[i])})]
    if number == 368:
        a, b = (df.close.rolling(n).mean().to_numpy() for n in (50, 200))
        upper = df.high.rolling(20).max().shift(1).to_numpy()
        lower = df.low.rolling(20).min().shift(1).to_numpy()
        entries = np.where(fresh(close > upper), 1, np.where(fresh(close < lower), -1, 0))
        return [book("average-cross-context", 201,
                     np.where(cross_above(a, b), 1, np.where(cross_above(b, a), -1, 0)),
                     lambda i: {"sma50": float(a[i]), "sma200": float(b[i])}),
                book("prior-20-donchian", 21, entries,
                     lambda i: {"prior_20_high": float(upper[i]), "prior_20_low": float(lower[i])})]
    raise ValueError(f"No runnable adapter for source {number}")
