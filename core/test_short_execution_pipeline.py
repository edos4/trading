"""Bearish detection through next-open paper fills, protection and settlement."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.usefixtures("confirmed_buy_dii")

from config import settings
from core.backtester import _check_exit, _open_trade
from core.execution import opening_candidate
from core.paper_trader import PaperAccount, exit_state
from core.scanner import MarketScanner
from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal
from patterns_v2.signals import detect_trade_signals


def bar(price: float, day: int = 0, **prices) -> OHLCVCandle:
    return OHLCVCandle(
        **{k: prices.get(k, price) for k in ("open", "high", "low", "close")},
        volume=1000, timestamp=datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(days=day),
    )


def candidate(short: bool) -> TradeSignal:
    return TradeSignal(
        symbol="TEST", timeframe="1d", pattern="double_top" if short else "double_bottom",
        action="SELL" if short else "BUY", price=100, qty=0, confidence=.5,
        stop_loss=120 if short else 80, take_profit=70 if short else 130,
        stop_loss_pct_cap=.06, trailing_stop_pct=.12, trailing_activation_pct=.12,
        trailing_stop_mode="lowest_close" if short else "highest_close",
        signal_bar_timestamp=bar(100).timestamp, provenance="patternsv2",
        pattern_evidence={'entry_boundary_policy': 'frozen-breakout-v1',
                          'breakout_level': 110 if short else 90,
                          'direction': -1 if short else 1},
    )


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("opening,stop", [(100, None), (102, None), (100, 4)])
def test_fill_keeps_v2_ladder_in_actual_r_despite_legacy_settings(monkeypatch, short, opening, stop):
    s = candidate(short)
    if stop:
        s.stop_loss = 100 + stop if short else 100 - stop
    monkeypatch.setattr(settings, "stop_loss_trail_r", .1)
    monkeypatch.setattr(settings, "stop_loss_arm_r", .2)
    scanner = MarketScanner(symbols=["TEST"], market="us", patterns_v2=True,
                            kronos_gate_mode="off", kronos_exit_mode="off")
    original = replace(s)
    scanner._apply_fixed_stop(s, bar(100))
    assert s == original
    fill = opening_candidate(s, bar(opening, 1), 10000, "us")
    effective = (min(fill.stop_loss, opening * 1.06) if short
                 else max(fill.stop_loss, opening * .94))
    risk = abs(opening - effective) / opening
    assert fill.trailing_stop_pct == pytest.approx(2 * risk, abs=.000001)
    assert fill.trailing_activation_pct == pytest.approx(2 * risk, abs=.000001)
    assert fill.stop_loss == s.stop_loss and fill.take_profit == s.take_profit
    assert s == original


@pytest.mark.parametrize("short", [False, True])
def test_missing_trail_is_not_invented_at_the_open(short):
    s = candidate(short)
    s.trailing_stop_pct = s.trailing_activation_pct = s.trailing_stop_mode = None
    fill = opening_candidate(s, bar(100, 1), 10000, "us")
    assert fill.trailing_stop_pct is None and fill.trailing_activation_pct is None


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("level", ["stop", "target"])
@pytest.mark.parametrize("path", ["queue_position", "open_position"])
def test_bad_directional_geometry_never_opens_or_consumes_a_setup(short, level, path):
    book = PaperAccount(market="us", txn_cost_pct=.001)
    book.assume_session_open = True
    s = candidate(short)
    if level == "stop":
        s.stop_loss = 99 if short else 101
    else:
        s.take_profit = 101 if short else 99
    ok, reason = getattr(book, path)(s, bar(100), OHLCVStore())
    assert not ok and "crossed" in reason
    assert book.cash == book.initial_capital
    assert not book.positions and not book.pending_entries and not book.consumed_anchors


@pytest.mark.parametrize("price,reason", [(121, "structural stop"), (69, "target"), (119, "breakout boundary")])
def test_short_opening_gap_is_rejected_before_fees_or_a_position(price, reason):
    book = PaperAccount(market="us", txn_cost_pct=.001)
    book.assume_session_open = True
    store = OHLCVStore()
    assert book.queue_position(candidate(True), bar(100), store)[0]
    outcome = book.fill_pending("TEST", "1d", bar(price, 1), store)
    assert not outcome[0][1] and reason in outcome[0][2]
    assert not book.positions and not book.pending_entries
    assert book.cash == book.initial_capital


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("exit_kind", ["take_profit", "stop_loss", "trailing_stop"])
@pytest.mark.parametrize("slippage", [0, .002])
def test_real_detection_to_scanner_queue_restart_fill_and_settlement(
        monkeypatch, tmp_path, short, exit_kind, slippage):
    # The detector itself runs on successive causal prefixes. A positive
    # reflected M must survive conversion to a SELL with overhead protection.
    close = np.interp(np.arange(80), [0, 20, 35, 50, 65, 79], [100, 140, 90, 110, 91, 115])
    if short:
        close = 250 - close
    frame = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                          "close": close, "volume": 1000},
                         index=pd.date_range("2026-01-05", periods=80, tz="UTC"))
    name = "double_top" if short else "double_bottom"
    signals = []
    for n in range(60, len(frame) + 1):
        signals = [s for s in detect_trade_signals(
            frame.iloc[:n], symbol="TEST", timeframe="1d", latest_bar_only=True) if s.pattern == name]
        if signals:
            break
    assert signals, "The real detector must emit an actionable reversal"
    s = signals[0]
    assert s.action == ("SELL" if short else "BUY")
    assert s.stop_loss > s.price > s.take_profit if short else s.stop_loss < s.price < s.take_profit
    original = replace(s)
    monkeypatch.setattr(settings, "vision_confirmation_enabled", False)
    monkeypatch.setattr("analysis.pattern_dii.describe_pattern_dii_rejection", lambda *a, **k: None)
    book = PaperAccount(market="us", txn_cost_pct=.001, slippage_pct=slippage)
    book.assume_session_open = True
    scanner = MarketScanner(symbols=["TEST"], market="us", paper_account=book,
                            patterns_v2=True, volume_gate=False, collect_first=False,
                            kronos_gate_mode="off", kronos_exit_mode="off")
    scanner._append_signal_log = Mock()
    decision = replace(bar(s.price), timestamp=frame.index[n - 1].to_pydatetime())
    asyncio.run(scanner._process_signal(s, candle=decision))
    assert len(book.pending_entries) == 1
    assert s.stop_loss == original.stop_loss and s.trailing_stop_pct == original.trailing_stop_pct
    path = tmp_path / "book.json"
    book.save(path)
    book = PaperAccount.load(path, market="us")
    book.assume_session_open = True
    opened = replace(bar(s.price), timestamp=decision.timestamp + timedelta(days=1))
    assert book.fill_pending("TEST", "1d", opened, scanner._store)[0][1]
    assert book.on_bar("TEST", opened, "1d") == []
    position = book.latest_position("TEST")
    assert position.entry_price == pytest.approx(s.price * (1 - slippage if short else 1 + slippage))
    assert book.equity() == pytest.approx(
        book.initial_capital - position.entry_fee_paid - s.price * slippage * position.qty)
    if exit_kind == "trailing_stop":
        activation = position.entry_price * (1 + (-1 if short else 1) * position.trailing_activation_pct * 1.1)
        active_bar = replace(opened, high=max(opened.open, activation), low=min(opened.open, activation),
                             close=activation, timestamp=opened.timestamp + timedelta(days=1))
        assert book.on_bar("TEST", active_bar, "1d") == []
        assert position._trailing_activated
        trigger = exit_state(position)["trailing_stop"]
        raw_exit = trigger * (1.01 if short else .99)
    elif exit_kind == "stop_loss":
        trigger = exit_state(position)["hard_stop"]
        raw_exit = trigger * (1.01 if short else .99)
    else:
        trigger = position.take_profit
        raw_exit = trigger * (.99 if short else 1.01)
    # Existing position and its armed trail also survive a restart.
    book.save(path)
    book = PaperAccount.load(path, market="us")
    closing = replace(bar(raw_exit), timestamp=opened.timestamp + timedelta(days=3))
    closed = book.on_bar("TEST", closing, "1d")
    assert len(closed) == 1 and closed[0].exit_reason == exit_kind
    position = closed[0]
    assert position.exit_price == pytest.approx(raw_exit * (1 + slippage if short else 1 - slippage))
    sign = -1 if short else 1
    gross = sign * (position.exit_price - position.entry_price) * position.qty
    fees = (position.entry_price + position.exit_price) * position.qty * .001
    assert position.pnl_usd == pytest.approx(gross - fees)
    assert book.cash == pytest.approx(book.initial_capital + position.pnl_usd)
    assert book.equity() == pytest.approx(book.cash)
    assert not book.positions


def test_prior_audited_causal_engine_still_protects_existing_shorts():
    s = candidate(True)
    s.qty = 100
    position = _open_trade(s, bar(100), 0, execution_model="causal-v1")
    position.resolved_rules["engine_sha256"] = "6c9e5cb88fc36896f885a55b18a80617a1cc46ddeb4712dff23bfbcbade04d7e"
    assert _check_exit(bar(110, 1), position, 1) == (110, "stop_loss")
    position.resolved_rules["engine_sha256"] = "unverified-engine"
    with pytest.raises(RuntimeError, match="runtime changed"):
        _check_exit(bar(110, 1), position, 1)


def test_pse_remains_long_only_when_us_shorts_are_enabled():
    book = PaperAccount(market="ph")
    assert settings.short_entries_enabled is True
    ok, reason = book.queue_position(candidate(True), bar(100), OHLCVStore())
    assert not ok and "disabled in this market" in reason
    assert not book.pending_entries


def test_rejected_short_keeps_its_decision_rules_and_gate_count(monkeypatch):
    monkeypatch.setattr(settings, "short_entries_enabled", False)
    monkeypatch.setattr("core.scanner.append_signal_log", lambda *a: None)
    scanner = MarketScanner(symbols=["TEST"], market="us", patterns_v2=True,
                            kronos_gate_mode="off", kronos_exit_mode="off")
    s = candidate(True)
    asyncio.run(scanner._process_signal(s, candle=bar(100)))
    saved = scanner.signal_log_snapshot()[-1]
    assert saved["requested_rules"]["stop_loss"] == 120
    assert saved["requested_rules"]["trailing_stop_pct"] == .12
    assert saved["requested_rules"]["entry_mode"] == "decision-close"
    assert scanner.stats["rejection_by_gate"]["short_disabled"] == 1
