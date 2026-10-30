"""Replay tests for visual rule backtests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import core.rule_backtest as rule_backtest
from core.rule_contracts import RulePayload
from data.tv_client import OHLCVCandle


def candles(n=160) -> list[OHLCVCandle]:
    rng = np.random.default_rng(11)
    close = 100.0 + np.cumsum(rng.normal(0.15, 1.0, n))
    out = []
    for index, value in enumerate(close):
        stamp = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
        out.append(OHLCVCandle(
            open=float(value - 0.2), high=float(value + 1.0),
            low=float(value - 1.0), close=float(value),
            volume=1_000_000.0, timestamp=stamp,
        ))
    return out


def payload() -> RulePayload:
    return RulePayload.model_validate({
        "name": "close above sma",
        "timeframe": "1d",
        "direction": "long",
        "root": {"kind": "group", "op": "AND", "children": [{
            "kind": "condition",
            "left": {"source": "indicator", "id": "close"},
            "operator": "gt",
            "right": {"source": "indicator", "id": "sma", "params": {"period": 20}},
        }]},
        "exit": {
            "on_false": True,
            "stop": {"kind": "percent", "percent": 5},
            "target": {"kind": "r_multiple", "r": 2},
            "max_hold_bars": 30,
        },
    })


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(rule_backtest, "load_candles", lambda symbol, market="us": candles())
    return rule_backtest


def test_replay_produces_closed_trades(wired) -> None:
    result = wired.run_rule_backtest(payload(), ["TEST"], market="us")
    assert result["summary"]["trades"] >= 1
    assert result["trades"]
    for trade in result["trades"]:
        assert trade["entry_date"] <= trade["exit_date"]
        assert trade["reason"] in ("rule_false", "stop", "target", "max_hold",
                                   "end_date", "data_end")
        assert trade["bars"] >= 1


def test_preview_returns_rows_and_evidence(wired) -> None:
    result = wired.preview_rule(payload(), "TEST", bars=15)
    assert len(result["bars"]) == 15
    assert result["required_bars"] >= 60
    assert result["bars"][0]["conditions"]
    assert "label" in result["bars"][0]["conditions"][0]


def test_bad_rule_requires_history(wired, monkeypatch) -> None:
    slow = RulePayload.model_validate({
        "name": "slow",
        "root": {"kind": "group", "op": "AND", "children": [{
            "kind": "condition",
            "left": {"source": "indicator", "id": "sma", "params": {"period": 200}},
            "operator": "gt",
            "right": {"source": "value", "value": 1},
        }]},
    })
    with pytest.raises(ValueError):
        wired.preview_rule(slow, "TEST", bars=10)


def test_dii_daily_transition_fills_next_open():
    import pandas as pd
    from analysis.rule_engine import evaluate_series
    from analysis.rule_catalog import compute
    raw = payload().model_dump(mode='json')
    raw['root']['children'][0]['left'] = {'source': 'indicator', 'id': 'directional_imbalance_index', 'field': 'bulls'}
    raw['root']['children'][0]['right'] = {'source': 'value', 'value': 50}
    rule = RulePayload.model_validate(raw)
    close = np.concatenate([np.full(102, 100.), np.arange(101., 131.)])
    df = pd.DataFrame(dict(open=close-.2, high=close+1, low=close-1, close=close, volume=1e6))
    dates = pd.bdate_range('2024-01-01', periods=len(df)).strftime('%Y-%m-%d').tolist()
    state = evaluate_series(rule, df)
    signal = state[state & ~state.shift(1, fill_value=False)].index[0]
    trades = rule_backtest._replay('TEST', df, dates, rule, notional=1000, txn_cost=0, slippage=0, start_date=None, end_date=None)
    assert trades[0]['entry_date'] == dates[signal+1]
    assert trades[0]['entry'] == df.open.iloc[signal+1]
    assert trades[0]['entry_date'] != dates[signal]
    assert compute('directional_imbalance_index', df.iloc[:signal+1], {})['bulls'].iloc[-1] > 50
