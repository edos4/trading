"""Detector-level tests: edge trigger, bracket side, trail flag."""
from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.rule_detector import VisualRuleIndicator, exit_levels
from core.rule_contracts import ExitPlan, RulePayload


def frame(n=40) -> pd.DataFrame:
    close = pd.Series(np.linspace(100, 120, n))
    return pd.DataFrame({
        "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": pd.Series([1_000_000.0] * n),
    })


def payload() -> dict:
    return {
        "name": "above 110",
        "timeframe": "1d",
        "direction": "long",
        "root": {"kind": "group", "op": "AND", "children": [{
            "kind": "condition",
            "left": {"source": "indicator", "id": "close"},
            "operator": "gt",
            "right": {"source": "value", "value": 110},
        }]},
        "exit": {
            "on_false": True,
            "stop": {"kind": "percent", "percent": 5},
            "target": {"kind": "r_multiple", "r": 2},
        },
    }


class Store:
    def __init__(self, df: pd.DataFrame):
        self.df = df

    def get_df(self, symbol, timeframe, min_bars=2):
        return self.df if len(self.df) >= min_bars else None


class Snapshot:
    symbol = "TEST"
    timeframe = "1d"


class Candle:
    def __init__(self, stamp):
        self.timestamp = stamp


def test_exit_levels_are_on_the_correct_side() -> None:
    df = frame()
    plan = ExitPlan.model_validate(payload()["exit"])
    stop, target = exit_levels(plan, "long", 100.0, df)
    assert stop < 100.0 < target
    stop, target = exit_levels(plan, "short", 100.0, df)
    assert target < 100.0 < stop


def test_trails_flag_follows_on_false() -> None:
    on = VisualRuleIndicator({"id": "1", "name": "x", "payload": payload()})
    assert on.trails is True
    raw = payload()
    raw["exit"]["on_false"] = False
    off = VisualRuleIndicator({"id": "2", "name": "x", "payload": raw})
    assert off.trails is False


def test_fires_on_the_edge_only() -> None:
    df = frame(120)
    indicator = VisualRuleIndicator({"id": "1", "name": "x", "payload": payload()})
    fired_at = []
    for index in range(indicator.MIN_BARS, len(df) + 1):
        snapshot = Snapshot()
        snapshot.candle = Candle(pd.Timestamp("2025-01-01") + pd.Timedelta(days=index - 1))
        signal = indicator.analyze(snapshot, Store(df.iloc[:index]))
        if signal is not None:
            fired_at.append(index)
    assert fired_at, "rule should fire when close crosses above 110"
    assert fired_at == [fired_at[0]], "only the false→true edge may fire"


def test_trail_returns_none_when_rule_stops_matching() -> None:
    indicator = VisualRuleIndicator({"id": "1", "name": "x", "payload": payload()})
    action, stop = indicator.trail(frame(60))
    assert action == "BUY" and stop < 120.0
    quiet = frame(60).copy()
    quiet["close"] = 100.0
    quiet["open"] = 100.0
    quiet["high"] = 101.0
    quiet["low"] = 99.0
    assert indicator.trail(quiet) is None
