"""Backtest and paper share `_open_trade` / `_check_exit` / `_close_trade`; this
proves they still produce the *same* fills.

The backtester's walk over a fixture symbol yields the golden trade; the same
bars are then streamed one-by-one through a `PaperAccount` (open on the bar the
pattern fires, `on_bar` every bar after) and the closed trade must match on
entry / exit / reason / pnl.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.backtester import (
    _core_backtest_symbol,
    _iter_pattern_classes,
    _make_snapshot,
)
from core.market import get_market
from core.paper_trader import PaperAccount
from data.barcache import load as _load_barcache
from data.ohlcv_store import OHLCVStore
from patterns import _dedup

pytestmark = pytest.mark.slow

FIXTURE = str(Path(__file__).parent / "fixtures" / "barcache")

# (pattern, symbol) — each is a single golden trade in the pinned fixture that
# exits through the ladder (paper never force-closes at data end, so a
# `data_end` trade like QCOM can't have paper parity by design).
CASES = [
    ("pattern_002_double_top", "TXN"),           # trailing_stop
    ("pattern_002_double_top", "CDNS"),          # time_exit
    ("pattern_008_head_and_shoulders", "NET"),   # trailing_stop
    ("pattern_008_head_and_shoulders", "SWKS"),  # time_exit
    ("pattern_004_rounding_bottom", "ON"),       # take_profit, fractional qty
]


def _pattern(name):
    return next(c() for _, c in _iter_pattern_classes() if c().name == name)


def _backtest_trade(pattern, symbol, candles):
    profile = get_market("us")
    config = {
        "position_notional": 10_000.0, "txn_cost_pct": 0.0, "min_bars": 100,
        "session_tz": profile.session_tz, "market": "us",
        "lot_round": profile.lot_round,
    }
    trades, *_ = _core_backtest_symbol(symbol, "1d", candles, [pattern], config)
    assert len(trades) == 1, f"{symbol}: expected one golden trade, got {len(trades)}"
    return trades[0]


def _paper_trade(pattern, symbol, candles):
    # match the golden backtest config: flat notional, no cost, no slippage
    acct = PaperAccount(
        initial_capital=1_000_000.0, market="us", slippage_pct=0.0, txn_cost_pct=0.0,
    )
    acct.assume_session_open = True
    store = OHLCVStore(session_tz=get_market("us").session_tz)
    store.replace_all(symbol, "1d", candles)  # full history, like the walk
    _dedup.reset()

    min_bars = 30
    for i in range(min_bars, len(candles)):
        _dedup.set_current(i)
        candle = candles[i]
        # exits first (mirrors the walk: manage open positions, then scan)
        acct.on_bar(symbol, candle, "1d", is_new_bar=True)
        if acct.open_for(symbol):
            continue
        sig = pattern.analyze(_make_snapshot(symbol, "1d", candle), store)
        if sig is not None:
            ok, msg = acct.open_position(sig, candle, store)
            assert ok, msg
    _dedup.set_current(None)

    assert not acct.open_for(symbol), (
        f"{symbol}: position still open — pick a ladder-exit case, not data_end"
    )
    closed = [t for t in acct.closed if t.symbol == symbol]
    assert len(closed) == 1, f"{symbol}: expected one paper trade, got {len(closed)}"
    return closed[0]


@pytest.mark.parametrize("pattern_name, symbol", CASES)
def test_backtest_and_paper_fill_identically(pattern_name, symbol):
    pattern = _pattern(pattern_name)
    candles = _load_barcache("us", symbol, root=FIXTURE)
    assert candles

    bt = _backtest_trade(pattern, symbol, candles)
    pp = _paper_trade(pattern, symbol, candles)

    assert pp.entry_price == pytest.approx(bt.entry_price, abs=1e-6)
    assert pp.exit_price == pytest.approx(bt.exit_price, abs=1e-6)
    assert pp.exit_reason == bt.exit_reason
    assert pp.pnl_pct == pytest.approx(bt.pnl_pct, abs=1e-6)
    assert pp.qty == pytest.approx(bt.qty, abs=1e-6)
