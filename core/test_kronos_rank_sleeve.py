"""Unit checks for kronos_rank_sleeve ranking (no Kronos weights)."""

from __future__ import annotations

import pandas as pd

from core.kronos_rank_sleeve import (
    PATTERN_NAME,
    ForecastRow,
    is_kronos_rank_signal,
    rank_and_emit,
)
from patterns.base_pattern import TradeSignal


def _row(sym: str, pred: float) -> ForecastRow:
    return ForecastRow(
        symbol=sym,
        pred_1w=pred,
        last_close=100.0,
        asof=pd.Timestamp("2026-06-01"),
    )


def test_rank_long_only_top_k():
    rows = [
        _row("A", 0.12),
        _row("B", 0.08),
        _row("C", 0.04),  # below 6% floor
        _row("D", -0.10),
    ]
    sigs = rank_and_emit(rows, top_k=2, bottom_k=2, long_only=True, min_move=0.06)
    assert len(sigs) == 2
    assert all(s.action == "BUY" for s in sigs)
    assert [s.symbol for s in sigs] == ["A", "B"]
    assert all(s.pattern == PATTERN_NAME for s in sigs)
    assert all(is_kronos_rank_signal(s) for s in sigs)


def test_rank_with_shorts():
    rows = [
        _row("A", 0.15),
        _row("B", 0.07),
        _row("C", -0.02),
        _row("D", -0.11),
        _row("E", -0.20),
    ]
    sigs = rank_and_emit(rows, top_k=1, bottom_k=2, long_only=False, min_move=0.06)
    buys = [s for s in sigs if s.action == "BUY"]
    sells = [s for s in sigs if s.action == "SELL"]
    assert len(buys) == 1 and buys[0].symbol == "A"
    assert [s.symbol for s in sells] == ["E", "D"]
    assert buys[0].take_profit is not None and buys[0].stop_loss is not None


def test_empty_and_is_helper():
    assert rank_and_emit([]) == []
    fake = TradeSignal(
        symbol="X", action="BUY", pattern="pattern_003_double_bottom",
        timeframe="1d", confidence=0.9, price=1.0, qty=1,
    )
    assert not is_kronos_rank_signal(fake)


if __name__ == "__main__":
    test_rank_long_only_top_k()
    test_rank_with_shorts()
    test_empty_and_is_helper()
    print("kronos_rank_sleeve tests OK")
