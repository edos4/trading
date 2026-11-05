"""Symmetric R trails and settlement of saved long/short positions."""

from __future__ import annotations

from copy import deepcopy

import pytest

from config import settings
from core.backtester import _check_exit, _open_trade, trade_risk_dollars
from core.execution import opening_candidate
from core.paper_trader import PaperAccount, _trade_from_dict, _trade_to_dict, exit_state
from core.test_patternsv2_exits import candle, signal


SAVED_ENGINE = "83461f8baa2449746a5269e32c197edc024544fdf5bd611f66820450c74d934e"


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("opening", [98, 100, 102])
def test_fixed_r_giveback_at_actual_fill_is_symmetric_and_causal(short, opening):
    s = signal(short)
    s.trailing_stop_basis = "entry"
    s.trailing_stop_pct = s.trailing_activation_pct = .10
    s.signal_bar_timestamp = candle(100, 100, 100, 100).timestamp
    fill = opening_candidate(s, candle(opening, opening, opening, opening, 1), 10000, "us")
    p = _open_trade(fill, candle(opening, opening, opening, opening, 1), 0,
                    execution_model="causal-v1")
    risk_per_share = trade_risk_dollars(p) / p.qty
    direction = -1 if short else 1
    best = opening + direction * 2.5 * risk_per_share
    # Only a completed close establishes the trail. The activation session's
    # wide range must not execute the newly calculated order retroactively.
    activation = candle(opening, max(opening, best), min(opening, best), best, 2)
    assert _check_exit(activation, p, 1) == (None, "")
    assert exit_state(p)["trailing_stop"] == pytest.approx(best - direction*2*risk_per_share)
    p = _trade_from_dict(_trade_to_dict(p))
    assert p.resolved_rules["trailing_stop_basis"] == "entry"
    retrace = candle(best, max(best, opening), min(best, opening), opening, 3)
    price, reason = _check_exit(retrace, p, 2)
    assert reason == "trailing_stop"
    assert direction*(price-opening)/risk_per_share == pytest.approx(.5, abs=.00001)


@pytest.mark.parametrize("short", [False, True])
def test_old_saved_positions_keep_their_exact_trail_and_can_close_after_reload(short):
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0,
                    execution_model="causal-v1")
    activation = candle(100, 104, 90, 90, 1) if short else candle(100, 110, 96, 110, 1)
    assert _check_exit(activation, p, 1) == (None, "")
    raw = _trade_to_dict(p)
    del raw["trailing_stop_basis"]
    raw["resolved_rules"].pop("trailing_stop_basis")
    raw["resolved_rules"]["engine_sha256"] = SAVED_ENGINE
    original_rules = deepcopy(raw["resolved_rules"])
    restored = _trade_from_dict(raw)
    assert restored.trailing_stop_basis == "reference"
    expected = 94.5 if short else 104.5
    assert exit_state(restored)["trailing_stop"] == pytest.approx(expected)
    bar = candle(90, 100, 89, 98, 2) if short else candle(110, 111, 100, 102, 2)
    assert _check_exit(bar, restored, 2) == (pytest.approx(expected), "trailing_stop")
    assert restored.resolved_rules == original_rules
    restored.resolved_rules["engine_sha256"] = "unaudited-runtime"
    with pytest.raises(RuntimeError, match="runtime changed"):
        _check_exit(bar, restored, 2)


@pytest.mark.parametrize("short", [False, True])
def test_profitable_saved_position_queues_then_settles_next_open_with_costs(monkeypatch, tmp_path, short):
    from core.scanner import MarketScanner
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "kronos_exit_min_adverse_atr", .5)
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0,
                    execution_model="causal-v1")
    p.resolved_rules["engine_sha256"] = SAVED_ENGINE
    p.resolved_rules.pop("trailing_stop_basis")
    account = PaperAccount(market="us", txn_cost_pct=.001, slippage_pct=.002)
    p.entry_fee_paid = 10
    account.cash += (1 if short else -1)*100*p.qty - p.entry_fee_paid
    account.positions[p.symbol] = [p]
    anchor = 94 if short else 106
    activation = candle(100, 102, 94, anchor, 1) if short else candle(100, 106, 98, anchor, 1)
    assert account.on_bar(p.symbol, activation, "1d") == []
    scanner = MarketScanner(symbols=[p.symbol], market="us", paper_account=account,
                            kronos_gate_mode="off", kronos_exit_mode="exit")
    scanner._handle_kronos_exit(p, SimpleNamespace(decision="oppose", reason="opposing vote"),
                              {"anchor_close": anchor, "atr_pct": 4, "decision": "oppose"})
    assert p.pending_exit_reason == "kronos_exit" and not account.closed
    path = tmp_path/"account.json"
    account.save(path)
    account = PaperAccount.load(path, market="us")
    # The fill is next open, including an adverse gap and buy-to-cover costs.
    opening = 96 if short else 104
    closed = account.on_bar(p.symbol, candle(opening, 107, 93, anchor, 2), "1d")
    assert len(closed) == 1 and account.open_count() == 0
    t = closed[0]
    assert t.exit_price == pytest.approx(opening*(1.002 if short else .998))
    assert t.exit_reason == "kronos_exit"
    assert t.sim_exit_date == candle(opening, 107, 93, anchor, 2).timestamp
    assert t.kronos_exit_evidence["exit_action"] == "queued_next_open"
    assert account.cash == pytest.approx(account.initial_capital + t.pnl_usd)
    assert account.equity() == pytest.approx(account.cash)
    assert account.on_bar(p.symbol, candle(opening, 107, 93, anchor, 2), "1d") == []
    assert len(account.closed) == 1


def test_new_converter_selects_entry_basis_without_changing_old_signals():
    import pandas as pd
    from patterns_v2.signals import _to_signal

    frame = pd.DataFrame({"close": [100.]}, index=pd.to_datetime(["2026-01-05"]))
    raw = {"pattern": "double_bottom", "direction": 1, "signal": 0, "level": 100,
           "points": [(0, 70, "low 1")]}
    assert _to_signal(frame, raw, symbol="TEST", timeframe="1d", latest=0).trailing_stop_basis == "entry"
    assert signal().trailing_stop_basis == "reference"
