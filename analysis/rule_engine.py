"""Vectorized evaluation of visual indicator rule trees.

Series are computed once per distinct (indicator, params, output) and reused by
every condition that references them. Crossings compare the previous and the
current bar, so a caller that needs only the latest bar can pass a cached
``series`` from :func:`evaluate_series`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.rule_catalog import compute, label as catalog_label, warmup
from core.rule_contracts import (
    Condition, Group, Operand, OPERATOR_LABELS, RulePayload,
)

MIN_WARMUP = 60


def _is_group(node) -> bool:
    return isinstance(node, Group)


def _operand_series(operand: Operand, df: pd.DataFrame,
                    cache: dict[tuple, pd.Series]) -> pd.Series:
    if operand.source == "value":
        value = operand.value if operand.value is not None else np.nan
        return pd.Series(np.full(len(df), float(value)), index=df.index, dtype=float)
    key = (operand.id, tuple(sorted(operand.params.items())), operand.field)
    if key not in cache:
        outputs = compute(operand.id, df, operand.params)
        field = operand.field or "value"
        if field not in outputs:
            # A multi-output indicator (MACD, Bollinger, Stochastic) has no
            # natural "value"; an omitted field means its first output.
            if field in ("", "value") and outputs:
                field = next(iter(outputs))
            else:
                raise ValueError(
                    f"{operand.id} has no output {operand.field!r}; "
                    f"choose one of {', '.join(sorted(outputs))}")
        cache[key] = outputs[field].reindex(df.index)
    series = cache[key]
    return series.shift(int(operand.offset)) if operand.offset else series


def _finite(series: pd.Series) -> bool:
    return bool(pd.notna(series).all())


def _compare(left: pd.Series, right: pd.Series, operator: str) -> pd.Series:
    if operator == "gt":
        result = left > right
    elif operator == "gte":
        result = left >= right
    elif operator == "lt":
        result = left < right
    elif operator == "lte":
        result = left <= right
    elif operator == "eq":
        result = pd.Series(
            np.isclose(left.to_numpy(dtype=float), right.to_numpy(dtype=float),
                       rtol=1e-9, atol=1e-9),
            index=left.index,
        )
    elif operator == "ne":
        result = pd.Series(
            ~np.isclose(left.to_numpy(dtype=float), right.to_numpy(dtype=float),
                        rtol=1e-9, atol=1e-9),
            index=left.index,
        )
    elif operator == "crosses_above":
        result = (left.shift(1) <= right.shift(1)) & (left > right)
    elif operator == "crosses_below":
        result = (left.shift(1) >= right.shift(1)) & (left < right)
    else:
        raise ValueError(f"Unknown operator {operator!r}")
    # A NaN on either side is never a match; for `ne` this also stops a
    # missing value from looking like "different".
    result = result & left.notna() & right.notna()
    return result.fillna(False).astype(bool)


def _condition_series(condition: Condition, df: pd.DataFrame,
                      cache: dict[tuple, pd.Series]) -> pd.Series:
    left = _operand_series(condition.left, df, cache)
    right = _operand_series(condition.right, df, cache)
    return _compare(left, right, condition.operator)


def _group_series(group: Group, df: pd.DataFrame,
                  cache: dict[tuple, pd.Series]) -> pd.Series:
    parts = []
    for child in group.children:
        if _is_group(child):
            parts.append(_group_series(child, df, cache))
        else:
            parts.append(_condition_series(child, df, cache))
    if not parts:
        return pd.Series(np.zeros(len(df), dtype=bool), index=df.index)
    result = parts[0]
    for part in parts[1:]:
        result = (result & part) if group.op == "AND" else (result | part)
    return result.astype(bool)


def evaluate_series(rule: RulePayload, df: pd.DataFrame) -> pd.Series:
    """Boolean match for every bar. Input index is preserved."""
    if df is None or len(df) == 0:
        return pd.Series(dtype=bool)
    base = df.reset_index(drop=True)
    result = _group_series(rule.root, base, {})
    result.index = df.index
    return result.fillna(False).astype(bool)


def _value_at(series: pd.Series, index: int) -> float | None:
    if index < 0 or index >= len(series):
        return None
    value = series.iloc[index]
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def evaluate_bar(rule: RulePayload, df: pd.DataFrame, index: int = -1,
                 series: pd.Series | None = None) -> dict:
    """Latest-bar decision plus per-condition evidence for logs and the UI."""
    if df is None or len(df) == 0:
        return {"matched": False, "warm": False, "conditions": [],
                "reason": "no bars"}
    base = df.reset_index(drop=True)
    position = index if index >= 0 else len(base) + index
    if position < 0 or position >= len(base):
        return {"matched": False, "warm": False, "conditions": [],
                "reason": "bar index out of range"}
    cache: dict[tuple, pd.Series] = {}
    if series is None:
        series = evaluate_series(rule, df)
    matched = bool(series.iloc[position]) if position < len(series) else False

    conditions: list[dict] = []
    warm = True

    def walk(node) -> None:
        nonlocal warm
        if _is_group(node):
            for child in node.children:
                walk(child)
            return
        left = _operand_series(node.left, base, cache)
        right = _operand_series(node.right, base, cache)
        left_value = _value_at(left, position)
        right_value = _value_at(right, position)
        if left_value is None or right_value is None:
            warm = False
        passed = bool(_condition_series(node, base, cache).iloc[position])
        conditions.append({
            "label": (f"{operand_label(node.left)} "
                      f"{OPERATOR_LABELS[node.operator]} "
                      f"{operand_label(node.right)}"),
            "left": left_value,
            "right": right_value,
            "pass": passed,
        })

    walk(rule.root)
    return {
        "matched": bool(matched and warm),
        "warm": warm,
        "conditions": conditions,
    }


def required_bars(rule: RulePayload) -> int:
    """Bars needed before the first condition can be evaluated."""
    needed = 0

    def walk(node) -> None:
        nonlocal needed
        if _is_group(node):
            for child in node.children:
                walk(child)
            return
        for operand in (node.left, node.right):
            if operand.source != "indicator":
                continue
            needed = max(needed, warmup(operand.id, operand.params) + int(operand.offset))

    walk(rule.root)
    return max(MIN_WARMUP, needed + 2)


def operand_label(operand: Operand) -> str:
    if operand.source == "value":
        return f"{operand.value:g}" if operand.value is not None else "?"
    try:
        name = catalog_label(operand.id, operand.params, operand.field)
    except ValueError:
        name = operand.id
    if operand.offset:
        name = f"{name}[{operand.offset}]"
    return name
