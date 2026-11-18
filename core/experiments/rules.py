"""Bounded, data-only daily rules. Model output is never Python or an expression to eval."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.experiments.adapters import Variant, cross_above
from indicators._math import ema, wilder_atr

VERSION = "daily-rules-v1"
RAW = {"open", "high", "low", "close", "volume"}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class Feature(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    kind: Literal["ema", "sma", "highest", "lowest", "atr"]
    price: Literal["open", "high", "low", "close", "volume"]
    period: int = Field(ge=1, le=500)
    lag: int = Field(ge=0, le=500)


class Condition(Contract):
    op: Literal["above", "below", "cross_above", "cross_below", "all", "any"]
    left: str | float | int | None = None
    right: str | float | int | None = None
    conditions: list[Condition] = Field(default_factory=list, max_length=8)


class RuleVariant(Contract):
    name: str = Field(min_length=1, max_length=80)
    side: Literal["long", "short"]
    entry: Condition
    exit: Condition
    evidence: str = Field(min_length=20, max_length=4000)


class RuleSpecification(Contract):
    version: Literal[1]
    timeframe: Literal["daily"]
    execution: Literal["next-session-open"]
    sizing: Literal["independent-fixed-notional"]
    markets: list[Literal["us", "ph"]] = Field(min_length=1, max_length=2)
    symbols: list[str] = Field(max_length=100)
    features: list[Feature] = Field(min_length=1, max_length=16)
    variants: list[RuleVariant] = Field(min_length=1, max_length=8)
    source_evidence: str = Field(min_length=40, max_length=8000)

    @model_validator(mode="after")
    def validate_rules(self) -> RuleSpecification:
        names = [f.name for f in self.features]
        if len(names) != len(set(names)) or set(names) & RAW:
            raise ValueError("Feature names must be unique and cannot replace OHLCV")
        if any(f.kind == "atr" and f.price != "close" for f in self.features):
            raise ValueError("Wilder ATR requires high/low/close; its price field must be close")
        if len({v.name for v in self.variants}) != len(self.variants):
            raise ValueError("Variant names must be unique")
        if len(set(self.markets)) != len(self.markets):
            raise ValueError("Duplicate markets")
        for symbol in self.symbols:
            if not symbol or len(symbol) > 40 or not all(c.isalnum() or c in '.-^_' for c in symbol):
                raise ValueError("Invalid source symbol")
        for variant in self.variants:
            for condition in (variant.entry, variant.exit):
                self._check(condition, set(names) | RAW, 0, [0])
        return self

    @staticmethod
    def _check(condition: Condition, names: set[str], depth: int, count: list[int]) -> None:
        count[0] += 1
        if depth > 5 or count[0] > 32:
            raise ValueError("Rule condition is too complex")
        if condition.op in {"all", "any"}:
            if not condition.conditions or condition.left is not None or condition.right is not None:
                raise ValueError("Boolean rules require only a nonempty conditions list")
            for child in condition.conditions:
                RuleSpecification._check(child, names, depth + 1, count)
        else:
            if condition.conditions or condition.left is None or condition.right is None:
                raise ValueError("Comparison rules require left and right operands")
            for operand in (condition.left, condition.right):
                if isinstance(operand, str) and operand not in names:
                    raise ValueError("Unknown feature reference")


def spec_hash(spec: dict) -> str:
    validated = RuleSpecification.model_validate(spec).model_dump()
    return hashlib.sha256(json.dumps(validated, sort_keys=True).encode()).hexdigest()


def definition(spec: dict) -> dict:
    rule = RuleSpecification.model_validate(spec)
    return {"availability": "ready", "adapter_version": VERSION, "kind": "trades",
            "scope": "AI-generated daily signal rules; next-session-open research replay",
            "tested_label": "Generated signal-rule replay · independent fixed notional",
            "blocker": None, "markets": rule.markets, "symbols": rule.symbols,
            "requires_borrow": all(v.side == "short" for v in rule.variants),
            "parameters": rule.model_dump(), "rule_spec_sha256": spec_hash(spec),
            "family_id": "url-" + spec_hash(spec)[:16], "policy_id": VERSION,
            "source_strategy_implemented": False,
            "assumptions": ["Generated interpretation, not a human-reviewed source adapter.",
                            "Daily OHLCV only; signals use completed bars and fill at the next session open.",
                            "Independent fixed-notional accounts; user-selected costs, liquidity and end policy apply.",
                            "One position per symbol/variant. New entries while held are suppressed.",
                            "EMA seeds at its first available input with adjust=False; warm-up is five periods."],
            "limitations": ["Inspect the source and generated rules before drawing conclusions.",
                            "No intraday, proprietary, profile, options, synthetic-price, bracket or discretionary rules.",
                            "No finite portfolio cash/exposure model or invented protective stop/target.",
                            "Backtest P&L measures this declared research book, not verified source performance."]}


def variants(spec: dict, df: pd.DataFrame) -> list[Variant]:
    rule = RuleSpecification.model_validate(spec)
    values = {key: df[key].to_numpy(dtype=float) for key in RAW}
    warmup = 1
    for feature in rule.features:
        src = values[feature.price]
        if feature.kind == "ema":
            array = ema(src, feature.period)
        elif feature.kind == "atr":
            array = wilder_atr(values["high"], values["low"], values["close"], feature.period)
        else:
            rolling = pd.Series(src).rolling(feature.period)
            array = getattr(rolling, {"sma":"mean", "highest":"max", "lowest":"min"}[feature.kind])().to_numpy()
        values[feature.name] = pd.Series(array).shift(feature.lag).to_numpy()
        warmup = max(warmup, (5 * feature.period if feature.kind == "ema" else feature.period)
                     + feature.lag + 1)

    def operand(value):
        return values[value] if isinstance(value, str) else np.full(len(df), value, dtype=float)

    def evaluate(condition: Condition) -> np.ndarray:
        if condition.op in {"all", "any"}:
            arrays = [evaluate(child) for child in condition.conditions]
            return (np.logical_and if condition.op == "all" else np.logical_or).reduce(arrays)
        left, right = operand(condition.left), operand(condition.right)
        valid = np.isfinite(left) & np.isfinite(right)
        if condition.op == "above":
            return valid & (left > right)
        if condition.op == "below":
            return valid & (left < right)
        previous_valid = np.r_[False, valid[:-1]]
        return valid & previous_valid & (cross_above(left, right) if condition.op == "cross_above"
                                         else cross_above(right, left))

    def evidence(index: int) -> dict:
        return {name: float(array[index]) for name, array in values.items() if np.isfinite(array[index])}

    return [Variant(v.name, warmup, evaluate(v.entry).astype(int) * (1 if v.side == "long" else -1),
                    evaluate(v.exit), evidence, kind="trades") for v in rule.variants]
