"""Validated requests shared by HTTP and the experiment worker."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BatchRequest(BaseModel):
    """One click schedules every source folder, including explicit blockers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    start_date: date
    end_date: date
    holdout_start: date | None = None
    market: Literal["us", "ph"] = "us"
    # Empty symbols plus a count means the paper book's top liquid names.
    # An explicit symbol list replaces that ranking. Omitted count keeps the
    # full history catalog for older clients.
    n_symbols: int | None = Field(None, ge=5, le=20000)
    symbols: list[str] = Field(default_factory=list, max_length=20000)
    source_ids: list[str] = Field(default_factory=list, max_length=2000)
    initial_capital: float = Field(100000, gt=0, le=1e12)
    position_notional: float = Field(10000, gt=0, le=1e10)
    txn_cost_pct: float | None = Field(None, ge=0, lt=0.1)
    slippage_pct: float = Field(0.0005, ge=0, lt=0.1)
    short_policy: Literal["exclude", "assume-borrow"] = "exclude"
    borrow_rate: float = Field(0.03, ge=0, le=5)  # Annual simple rate on entry notional.
    participation: float = Field(0.01, gt=0, le=1)  # Fraction of trailing daily liquidity.
    end_policy: Literal["keep-open", "force-close"] = "keep-open"
    idempotency_key: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_range(self) -> BatchRequest:
        if self.start_date > self.end_date:
            raise ValueError("Start date must be on or before end date")
        if self.holdout_start and not self.start_date < self.holdout_start <= self.end_date:
            raise ValueError("Holdout must begin after the start and within the selected range")
        if (self.end_date - self.start_date).days > 36525:
            raise ValueError("Choose a date range of at most 100 years")
        cleaned = []
        for raw in self.symbols:
            symbol = raw.strip().upper()
            if not symbol or len(symbol) > 40 or any(ord(c) < 32 for c in symbol):
                raise ValueError("Invalid symbol")
            if symbol not in cleaned:
                cleaned.append(symbol)
        self.symbols = cleaned
        if any(not s or len(s) > 160 for s in self.source_ids):
            raise ValueError("Invalid source selection")
        self.source_ids = list(dict.fromkeys(self.source_ids))
        return self
