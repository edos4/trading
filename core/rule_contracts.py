"""Typed contracts for the visual indicator (rule) builder.

One validator is shared by the store, the HTTP API and the live detector, so a
hand-crafted JSON body cannot bypass what the editor enforces.
"""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

Operator = Literal[
    "gt", "gte", "lt", "lte", "eq", "ne", "crosses_above", "crosses_below",
]

MAX_DEPTH = 4
MAX_BLOCKS = 12
MAX_CONDITIONS = 40
MAX_NAME = 80


class Operand(BaseModel):
    """One side of a comparison: a catalog value or a literal number."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["indicator", "value"] = "value"
    id: str = ""
    params: dict[str, float] = Field(default_factory=dict)
    field: str = "value"
    offset: int = Field(0, ge=0, le=500)
    value: float | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.source == "value":
            if self.value is None:
                raise ValueError("A literal operand needs a numeric value")
        else:
            if not (self.id or "").strip():
                raise ValueError("An indicator operand needs an id")
        return self


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["condition"] = "condition"
    left: Operand
    operator: Operator
    right: Operand


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["group"] = "group"
    op: Literal["AND", "OR"] = "AND"
    children: list[Union[Condition, "Group"]] = Field(default_factory=list)


Group.model_rebuild()


class StopPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["percent", "atr", "none"] = "percent"
    percent: float = Field(5.0, gt=0, le=50)
    atr_period: int = Field(14, ge=2, le=200)
    atr_mult: float = Field(2.0, gt=0, le=20)


class TargetPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["r_multiple", "percent", "none"] = "r_multiple"
    r: float = Field(2.0, gt=0, le=50)
    percent: float = Field(10.0, gt=0, le=200)


class ExitPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_false: bool = True
    stop: StopPlan = Field(default_factory=StopPlan)
    target: TargetPlan = Field(default_factory=TargetPlan)
    max_hold_bars: int | None = Field(None, ge=1, le=1000)

    @model_validator(mode="after")
    def _check(self):
        if self.stop.kind == "none" and self.target.kind == "none":
            raise ValueError("Give the rule a stop or a target; both cannot be none")
        return self


class RulePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    name: str = Field(..., min_length=1, max_length=MAX_NAME)
    timeframe: Literal["1d", "1W"] = "1d"
    direction: Literal["long", "short"] = "long"
    root: Group
    exit: ExitPlan = Field(default_factory=ExitPlan)

    @model_validator(mode="after")
    def _limits(self):
        if self.root.kind != "group":
            raise ValueError("The rule root must be a group")
        if not self.root.children:
            raise ValueError("Add at least one block with one condition")

        counts = {"blocks": 0, "conditions": 0}

        def walk(node: Union[Condition, Group], depth: int) -> None:
            if depth > MAX_DEPTH:
                raise ValueError(f"Groups can nest at most {MAX_DEPTH} levels")
            if isinstance(node, Condition):
                counts["conditions"] += 1
                return
            counts["blocks"] += 1
            if not node.children:
                raise ValueError("Each block needs at least one condition")
            for child in node.children:
                walk(child, depth + 1)

        walk(self.root, 1)
        if counts["conditions"] > MAX_CONDITIONS:
            raise ValueError(f"At most {MAX_CONDITIONS} conditions")
        if counts["blocks"] > MAX_BLOCKS:
            raise ValueError(f"At most {MAX_BLOCKS} blocks")
        return self


# ── Condition evidence helpers ──────────────────────────────────────────────

OPERATOR_LABELS = {
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "eq": "=",
    "ne": "!=",
    "crosses_above": "crosses above",
    "crosses_below": "crosses below",
}


def describe_operand(operand: Operand) -> str:
    if operand.source == "value":
        value = operand.value
        if value is None:
            return "?"
        return f"{value:g}"
    name = operand.id
    if operand.field and operand.field != "value":
        name = f"{name}.{operand.field}"
    if operand.params:
        args = ", ".join(f"{key}={value:g}" for key, value in sorted(operand.params.items()))
        name = f"{name}({args})"
    if operand.offset:
        name = f"{name}[{operand.offset}]"
    return name


def describe_condition(condition: Condition) -> str:
    return (f"{describe_operand(condition.left)} "
            f"{OPERATOR_LABELS[condition.operator]} "
            f"{describe_operand(condition.right)}")
