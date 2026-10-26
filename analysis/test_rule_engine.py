"""Truth-table tests for the visual rule engine."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analysis.rule_engine import evaluate_bar, evaluate_series, required_bars
from core.rule_contracts import RulePayload


def frame(close, **extra) -> pd.DataFrame:
    values = pd.Series([float(x) for x in close])
    data = {
        "open": values,
        "high": values + 1.0,
        "low": values - 1.0,
        "close": values,
        "volume": pd.Series([1_000_000.0] * len(values)),
    }
    data.update(extra)
    return pd.DataFrame(data)


def condition(indicator_id="close", operator="gt", right=None, **left):
    left_operand = {"source": "indicator", "id": indicator_id, **left}
    right_operand = right if isinstance(right, dict) else {"source": "value", "value": right}
    return {"kind": "condition", "left": left_operand, "operator": operator,
            "right": right_operand}


def rule(children, op="AND"):
    return RulePayload.model_validate({
        "name": "test",
        "root": {"kind": "group", "op": op, "children": children},
    })


def test_comparisons_and_series_index() -> None:
    df = frame(range(10))
    series = evaluate_series(rule([condition(right=5)]), df)
    assert list(series.astype(int)) == [0, 0, 0, 0, 0, 0, 1, 1, 1, 1]
    assert list(series.index) == list(df.index)


def test_nested_and_or() -> None:
    df = frame(range(10))
    tree = rule([
        condition(right=100),                       # false everywhere
        {"kind": "group", "op": "OR", "children": [
            condition("close", "gte", 5),           # true from bar 5
            condition("close", "lt", 2),            # true at 0,1
        ]},
    ], op="OR")
    series = evaluate_series(tree, df)
    assert not series.iloc[4]
    assert series.iloc[5]
    assert series.iloc[0]
    # Root AND would be wrong here; prove the operator is honoured.
    root_and = rule([
        condition(right=100),
        {"kind": "group", "op": "OR", "children": [
            condition("close", "gte", 5),
            condition("close", "lt", 2),
        ]},
    ], op="AND")
    assert not evaluate_series(root_and, df).any()


def test_root_or_and_cross() -> None:
    df = frame([1, 2, 3, 2, 1, 2, 3])
    cross = condition(operator="crosses_above", right=2)
    series = evaluate_series(rule([cross]), df)
    assert list(series.astype(int)) == [0, 0, 1, 0, 0, 0, 1]


def test_offset_compares_previous_bar() -> None:
    df = frame([1, 2, 1, 3, 2])
    up = {"kind": "condition",
          "left": {"source": "indicator", "id": "close"},
          "operator": "gt",
          "right": {"source": "indicator", "id": "close", "offset": 1}}
    assert list(evaluate_series(rule([up]), df).astype(int)) == [0, 1, 0, 1, 0]


def test_warm_is_false_without_enough_history() -> None:
    df = frame(range(10))
    sma = condition("sma", "gt", 1, params={"period": 30})
    evidence = evaluate_bar(rule([sma]), df)
    assert evidence["warm"] is False
    assert evidence["matched"] is False


def test_required_bars_floor_and_warmup() -> None:
    assert required_bars(rule([condition(right=1)])) == 60
    weekly = RulePayload.model_validate({
        "name": "slow", "timeframe": "1W",
        "root": {"kind": "group", "op": "AND", "children": [
            condition("sma", "gt", 1, params={"period": 200})]},
    })
    assert required_bars(weekly) >= 202


def test_unknown_indicator_and_bad_param_rejected() -> None:
    df = frame(range(10))
    with pytest.raises(ValueError):
        evaluate_series(rule([condition("nope", right=1)]), df)
    with pytest.raises(ValueError):
        evaluate_series(rule([condition("rsi", right=1, params={"period": 0})]), df)


def test_catalog_validation_rejects_unknown_ids_params_and_fields() -> None:
    from analysis.rule_catalog import validate_rule

    validate_rule(rule([condition("rsi", right=1, params={"period": 14})]))
    with pytest.raises(ValueError):
        validate_rule(rule([condition("nope", right=1)]))
    with pytest.raises(ValueError):
        validate_rule(rule([condition("rsi", right=1, params={"period": 0})]))
    with pytest.raises(ValueError):
        validate_rule(rule([condition("rsi", right=1, params={"bogus": 1})]))
    with pytest.raises(ValueError):
        validate_rule(rule([condition("macd", right=1, field="bogus")]))


def test_value_operand_requires_value() -> None:
    with pytest.raises(ValueError):
        RulePayload.model_validate({
            "name": "bad",
            "root": {"kind": "group", "op": "AND", "children": [{
                "kind": "condition",
                "left": {"source": "indicator", "id": "close"},
                "operator": "gt",
                "right": {"source": "value"},
            }]},
        })


def test_patternsv2_attachment_contract() -> None:
    base = rule([condition(right=1)]).model_dump(mode="json")
    assert base["attachments"]["patternsv2"]["position"] == "off"
    assert base["attachments"]["patternsv2"]["pattern"] == "all"
    attached = dict(base)
    attached["attachments"] = {
        "patternsv2": {"pattern": "double_bottom",
                       "position": "before", "mode": "filter"},
    }
    parsed = RulePayload.model_validate(attached)
    assert parsed.attachments.patternsv2.position == "before"
    assert parsed.attachments.patternsv2.pattern == "double_bottom"
    for bad in (
        {"patternsv2": {"position": "sideways", "mode": "filter"}},
        {"patternsv2": {"position": "after", "mode": "sometimes"}},
        {"patternsv2": {"position": "after", "mode": "filter", "extra": 1}},
    ):
        broken = dict(base)
        broken["attachments"] = bad
        with pytest.raises(ValueError):
            RulePayload.model_validate(broken)

    from analysis.rule_catalog import validate_rule

    broken = dict(base)
    broken["attachments"] = {"patternsv2": {
        "pattern": "not_a_pattern", "position": "after", "mode": "filter"}}
    with pytest.raises(ValueError):
        validate_rule(RulePayload.model_validate(broken))


def test_empty_group_rejected() -> None:
    with pytest.raises(ValueError):
        RulePayload.model_validate({
            "name": "bad",
            "root": {"kind": "group", "op": "AND", "children": []},
        })


DII = 'directional_imbalance_index'


def test_dii_catalog_and_outputs():
    from analysis.rule_catalog import catalog_payload, compute, resolve_params, warmup, validate_rule
    entries = [item for item in catalog_payload() if item['id'] == DII]
    assert len(entries) == 1
    assert entries[0]['outputs'] == ['bulls', 'bears', 'up', 'down', 'upper', 'lower']
    assert resolve_params(DII, None) == {'length': 10, 'period': 70}
    assert warmup(DII, None) == 80
    df = frame(range(100, 260))
    for params in ({}, {'length': 3, 'period': 7}):
        outputs = compute(DII, df, params)
        for field, values in outputs.items():
            tree = rule([condition(DII, 'eq', float(values.iloc[-1]), params=params, field=field)])
            validate_rule(tree)
            assert evaluate_bar(tree, df)['matched']
    implicit = rule([condition(DII, 'gt', 50)])
    assert required_bars(implicit) == 82
    assert evaluate_series(implicit, df).equals(evaluate_series(rule([condition(DII, 'gt', 50, field='bulls')]), df))
    assert required_bars(rule([condition(DII, 'gt', 50, offset=4)])) == 86
    assert required_bars(rule([condition(DII, 'gt', 50, params={'length': 30, 'period': 40})])) == 72
    for params in ({'nope': 1}, {'length': 0}, {'period': 251}):
        with pytest.raises(ValueError): validate_rule(rule([condition(DII, 'gt', 50, params=params)]))
    with pytest.raises(ValueError): validate_rule(rule([condition(DII, 'gt', 50, field='nope')]))


def test_dii_dominance_crossing_offsets_and_unavailable():
    df = frame([200.] * 100 + list(np.arange(201., 301.)))
    bullish = rule([condition(DII, 'gt', 50, field='bulls')])
    relative = rule([condition(DII, 'gt', {'source': 'indicator', 'id': DII, 'field': 'bears'}, field='bulls')])
    state = evaluate_series(bullish, df)
    assert state.equals(evaluate_series(relative, df))
    cross = evaluate_series(rule([condition(DII, 'crosses_above', 50, field='bulls')]), df)
    assert required_bars(rule([condition(DII, 'crosses_above', 50, field='bulls')])) == 82
    assert cross.sum() == 1
    assert cross[cross].index[0] == state[state].index[0]
    shifted = evaluate_series(rule([condition(DII, 'gt', 50, field='bulls', offset=2)]), df)
    assert shifted.equals(state.shift(2, fill_value=False))
    contracting = frame(range(100))
    contracting['high'], contracting['low'] = 500-np.arange(100.), np.arange(100.)
    for operator in ('gt', 'ne', 'eq'):
        assert not evaluate_series(rule([condition(DII, operator, 50, field='bulls')]), contracting).any()
