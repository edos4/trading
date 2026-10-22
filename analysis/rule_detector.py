"""Live detector for a saved visual indicator rule.

Injected into the scanner exactly like ``TvStackIndicator``: it fires on the
false→true edge of the rule and trails out when the rule stops matching.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.indicator_engine import IndicatorEngine
from analysis.rule_catalog import validate_rule
from analysis.rule_engine import evaluate_bar, evaluate_series, required_bars
from core.rule_contracts import ExitPlan, RulePayload, TargetPlan, StopPlan
from indicators._signal import BaseIndicator, emit


def _last(df: pd.DataFrame, column: str) -> float:
    return float(df[column].iloc[-1])


def _atr(df: pd.DataFrame, period: int) -> float:
    values = IndicatorEngine(df).atr(int(period))
    value = float(values.iloc[-1]) if len(values) else float("nan")
    return value


def _stop_price(plan: StopPlan, direction: str, price: float,
                df: pd.DataFrame) -> float | None:
    if plan.kind == "percent":
        distance = price * (float(plan.percent) / 100.0)
    elif plan.kind == "atr":
        atr = _atr(df, plan.atr_period)
        if not np.isfinite(atr) or atr <= 0:
            return None
        distance = float(plan.atr_mult) * atr
    else:
        # "none" is not allowed to leave a trade unstopped: emit() needs a
        # finite stop, and an unbounded loss is not a strategy.
        atr = _atr(df, 14)
        if not np.isfinite(atr) or atr <= 0:
            return None
        distance = 2.0 * atr
    stop = price - distance if direction == "long" else price + distance
    return round(stop, 4) if np.isfinite(stop) and stop > 0 else None


def _target_price(plan: TargetPlan, direction: str, price: float,
                  stop: float) -> float | None:
    if plan.kind == "none":
        return None
    if plan.kind == "percent":
        distance = price * (float(plan.percent) / 100.0)
        target = price + distance if direction == "long" else price - distance
    else:
        risk = abs(price - stop)
        if risk <= 0:
            return None
        target = (price + float(plan.r) * risk if direction == "long"
                  else price - float(plan.r) * risk)
    return round(target, 4) if np.isfinite(target) and target > 0 else None


def exit_levels(plan: ExitPlan, direction: str, price: float,
                df: pd.DataFrame) -> tuple[float | None, float | None]:
    """Stop/target bracket on the correct side of ``price``, or (None, None)."""
    stop = _stop_price(plan.stop, direction, price, df)
    if stop is None:
        return None, None
    target = _target_price(plan.target, direction, price, stop)
    if direction == "long":
        if stop >= price or (target is not None and target <= price):
            return None, None
    else:
        if stop <= price or (target is not None and target >= price):
            return None, None
    return stop, target


class VisualRuleIndicator(BaseIndicator):
    """One saved rule. Not discovered with the local indicators."""

    def __init__(self, rule: dict):
        payload = dict(rule.get("payload") or {})
        payload["name"] = rule.get("name") or payload.get("name") or "visual_rule"
        payload.setdefault("schema_version", 1)
        self._rule = RulePayload.model_validate(payload)
        validate_rule(self._rule)
        # `_trail_indicators` skips detectors with trails=False; implementing
        # trail() without this flag would silently disable rule-false exits.
        self.trails = bool(self._rule.exit.on_false)
        self.error = ""
        self.rule_id = rule.get("id") or ""

    @property
    def name(self) -> str:
        return self._rule.name

    @property
    def timeframes(self) -> list[str]:
        return [self._rule.timeframe]

    @property
    def MIN_BARS(self) -> int:
        return required_bars(self._rule)

    @property
    def action(self) -> str:
        return "BUY" if self._rule.direction == "long" else "SELL"

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None or len(df) < self.MIN_BARS or len(df) < 2:
            return None
        series = evaluate_series(self._rule, df)
        now = bool(series.iloc[-1]) if not pd.isna(series.iloc[-1]) else False
        prev = bool(series.iloc[-2]) if not pd.isna(series.iloc[-2]) else False
        if not now or prev:
            return None
        evidence = evaluate_bar(self._rule, df, -1, series=series)
        price = _last(df, "close")
        stop, target = exit_levels(self._rule.exit, self._rule.direction, price, df)
        if stop is None:
            return None
        passed = [item["label"] for item in evidence.get("conditions", []) if item["pass"]]
        notes = f"Rule {self.name} matched: " + ("; ".join(passed) or "condition tree")
        signal = emit(self, df, snapshot, action=self.action, stop=stop,
                      target=target, notes=notes)
        if signal is not None:
            signal.provenance = "visual_rule"
            signal.rule_evidence = evidence
            if self._rule.exit.max_hold_bars is not None:
                signal.exit_bars_after_entry = int(self._rule.exit.max_hold_bars)
        return signal

    def trail(self, df):
        if not self._rule.exit.on_false or df is None or len(df) < self.MIN_BARS:
            return None
        series = evaluate_series(self._rule, df)
        matched = bool(series.iloc[-1]) if not pd.isna(series.iloc[-1]) else False
        if not matched:
            return None
        price = _last(df, "close")
        stop, _target = exit_levels(self._rule.exit, self._rule.direction, price, df)
        if stop is None:
            return None
        return self.action, stop
