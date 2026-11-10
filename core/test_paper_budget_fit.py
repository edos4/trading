"""Budget admission uses prior volatility without learning from the entry bar."""
from __future__ import annotations

import pandas as pd
import pytest

from config import settings
from core.paper_trader import PaperAccount
from core.test_patternsv2_exits import candle, signal
from data.ohlcv_store import OHLCVStore
from patterns_v2.signals import atr_price_for


@pytest.mark.parametrize('short', [False, True])
@pytest.mark.parametrize('atr,allowed', [(1, True), (5, False)])
def test_budget_fit_veto_precedes_charges_and_is_mirrored(monkeypatch, short, atr, allowed):
    monkeypatch.setattr(settings, 'paper_v2_min_stop_atr', 1)
    book = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=.0005)
    book.assume_session_open = True
    s = signal(short)
    s.signal_bar_timestamp = candle(100, 100, 100, 100).timestamp
    s.pattern_evidence.update(atr_price=atr, atr_as_of=s.signal_bar_timestamp.date().isoformat())
    ok, reason = book.open_position(s, candle(100, 100, 100, 100), OHLCVStore())
    assert ok is allowed
    if allowed:
        evidence = book.open_for('TEST')[0].execution_risk['budget_fit']
        assert evidence['initial_stop_atr'] > 2.7 and evidence['passed']
    else:
        assert 'below 1 ATR' in reason
        assert book.cash == book.initial_capital and not book.open_count()


@pytest.mark.parametrize('stamp', [None, '2026-01-06'])
def test_enabled_budget_fit_cannot_use_missing_or_future_atr(monkeypatch, stamp):
    monkeypatch.setattr(settings, 'paper_v2_min_stop_atr', 1)
    book = PaperAccount(market='us')
    book.assume_session_open = True
    s = signal()
    s.signal_bar_timestamp = candle(100, 100, 100, 100).timestamp
    s.pattern_evidence.update(atr_price=1, atr_as_of=stamp)
    ok, reason = book.open_position(s, candle(100, 100, 100, 100), OHLCVStore())
    assert not ok and 'decision-time ATR' in reason
    assert not book.open_count() and book.cash == book.initial_capital


def test_shadow_records_missing_evidence_without_inventing_a_volatility_cutoff(monkeypatch):
    monkeypatch.setattr(settings, 'paper_v2_min_stop_atr', 0)
    book = PaperAccount(market='us')
    book.assume_session_open = True
    assert book.open_position(signal(), candle(100, 100, 100, 100), OHLCVStore())[0]
    evidence = book.open_for('TEST')[0].execution_risk['budget_fit']
    assert evidence['mode'] == 'shadow' and not evidence['verified']
    assert evidence['initial_stop_atr'] is None


def test_atr_evidence_never_uses_future_execution_range():
    frame = pd.DataFrame({'high': [102, 102, 999], 'low': [98, 98, 1], 'close': [100, 100, 500]})
    assert atr_price_for(frame, 1) == 4
    assert atr_price_for(frame.iloc[:2], 1) == 4


def test_recorded_evidence_is_json_native_for_a_numpy_entry_price(monkeypatch):
    """A numpy-typed candle must not leak a numpy scalar into the ledger.

    The scanner's candles are float64, so ``paper_initial_distance`` is a
    numpy scalar and ``ratio >= minimum`` used to be a numpy.bool_. That value
    is persisted with the trade and served to the paper tab, where it broke
    json.dumps (ledger never written) and FastAPI's encoder (500 -> blank tab).
    """
    import json

    import numpy as np

    monkeypatch.setattr(settings, 'paper_v2_min_stop_atr', 0)
    book = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=.0005)
    book.assume_session_open = True
    s = signal()
    s.signal_bar_timestamp = candle(100, 100, 100, 100).timestamp
    s.pattern_evidence.update(atr_price=1.0, atr_as_of=s.signal_bar_timestamp.date().isoformat())
    bar = candle(np.float64(100), np.float64(101), np.float64(99), np.float64(100))
    assert book.open_position(s, bar, OHLCVStore())[0]

    evidence = book.open_for('TEST')[0].execution_risk['budget_fit']
    assert type(evidence['passed']) is bool and evidence['passed'] is True
    assert type(evidence['verified']) is bool
    assert type(evidence['initial_stop_atr']) is float
    assert type(evidence['atr_price']) is float
    json.dumps(evidence)
