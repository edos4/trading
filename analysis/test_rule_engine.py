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
    attached = dict(base)
    attached["attachments"] = {"patternsv2": {"position": "before", "mode": "filter"}}
    parsed = RulePayload.model_validate(attached)
    assert parsed.attachments.patternsv2.position == "before"
    for bad in (
        {"patternsv2": {"position": "sideways", "mode": "filter"}},
        {"patternsv2": {"position": "after", "mode": "sometimes"}},
        {"patternsv2": {"position": "after", "mode": "filter", "extra": 1}},
    ):
        broken = dict(base)
        broken["attachments"] = bad
        with pytest.raises(ValueError):
            RulePayload.model_validate(broken)


def test_empty_group_rejected() -> None:
    with pytest.raises(ValueError):
        RulePayload.model_validate({
            "name": "bad",
            "root": {"kind": "group", "op": "AND", "children": []},
        })
