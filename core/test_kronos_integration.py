"""Kronos decisions are causal, scoped, and cannot bypass paper protection."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from config import settings
from core.backtester import _make_snapshot
from core.kronos_confirm import KronosConfirmGate
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from core.test_scanner_kronos_confirm import _candle, _signal, _scanner, _vote
from data.ohlcv_store import OHLCVStore


def worker_payload(args, returns=(.01, .02, -.01)):
    symbols = args[args.index("--symbols") + 1:args.index("--samples")]
    return {
        "market": args[args.index("--market") + 1],
        "model": args[args.index("--model") + 1],
        "samples": int(args[args.index("--samples") + 1]),
        "results": [dict(symbol=symbol, returns=returns, anchor_close=100,
                         anchor_date=args[args.index("--as-of") + 1], atr_pct=2) for symbol in symbols],
    }


def test_same_symbol_different_dates_never_share_forecasts_or_poison_cache(monkeypatch):
    calls = []

    def worker(script, args, **kwargs):
        date = args[args.index("--as-of") + 1]
        calls.append(date)
        return worker_payload(args, returns=(-.01, -.02, .01) if date == "2026-10-30" else (.01, .02, -.01))

    monkeypatch.setattr("core.kronos_confirm.run_worker", worker)
    gate = KronosConfirmGate(mode="confirm", exit_mode="exit")
    old, new = _candle(), _candle()
    new.timestamp += timedelta(days=3)
    position = SimpleNamespace(symbol="TEST", timeframe="1d", action="BUY")
    for _ in range(2):
        gate.prepare_exit([position], old, None)
        gate.queue(_signal(), new, None)
        entries, exits = gate.flush_with_exits()
        assert entries[0][2].decision == "confirm"
        assert exits[0][1].decision == "oppose"
        assert entries[0][3]["anchor_date"] == "2026-11-02"
        assert exits[0][2]["anchor_date"] == "2026-10-30"
    assert calls == ["2026-10-30", "2026-11-02"]


@pytest.mark.parametrize("action,expected", [("BUY", "oppose"), ("SELL", "confirm")])
def test_five_votes_require_a_real_majority(action, expected):
    gate = KronosConfirmGate(mode="confirm", samples=5,
                             fetch_fn=_vote(.01, .02, -.01, -.02, -.03))
    gate.queue(_signal(action=action), _candle(), None)
    assert gate.flush()[0][2].decision == expected


def test_missing_votes_cannot_reduce_the_configured_majority():
    gate = KronosConfirmGate(mode="confirm", samples=5, fetch_fn=_vote(.01, .02, -.01))
    gate.queue(_signal(), _candle(), None)
    assert gate.flush()[0][2].decision == "no_opinion"
    gate = KronosConfirmGate(mode="confirm", samples=1, fetch_fn=_vote(.01))
    gate.queue(_signal(), _candle(), None)
    assert gate.flush()[0][2].decision == "confirm"


def test_open_positions_have_priority_when_worker_budget_is_exhausted():
    fetch = Mock(side_effect=_vote(-.01, -.02, .01))
    gate = KronosConfirmGate(mode="confirm", exit_mode="exit", max_checks_per_scan=1, fetch_fn=fetch)
    gate.queue(_signal(symbol="NEW"), _candle(), None)
    p = SimpleNamespace(symbol="HELD", timeframe="1d", action="BUY")
    gate.prepare_exit([p], _candle(), None)
    entries, exits = gate.flush_with_exits()
    fetch.assert_called_once_with(["HELD"])
    assert exits[0][1].decision == "oppose"
    assert entries[0][2].decision == "no_opinion"


@pytest.mark.parametrize("field,value", [
    ("anchor_date", "2026-11-02"), ("anchor_date", "2026-10-29"),
    ("anchor_close", 99), ("atr_pct", float("nan")),
    ("returns", [float("nan"), .01, .02]),
    ("returns", [.01, .02, .03, .04]),
])
def test_wrong_or_malformed_worker_context_cannot_approve_entries_or_exits(monkeypatch, field, value):
    def worker(script, args, **kwargs):
        payload = worker_payload(args)
        payload["results"][0][field] = value
        return payload

    monkeypatch.setattr("core.kronos_confirm.run_worker", worker)
    gate = KronosConfirmGate(mode="confirm", exit_mode="exit")
    gate.queue(_signal(), _candle(), None)
    gate.prepare_exit([SimpleNamespace(symbol="TEST", timeframe="1d", action="SELL")], _candle(), None)
    entries, exits = gate.flush_with_exits()
    assert entries[0][2].decision == exits[0][1].decision == "no_opinion"
    assert not gate._cache


@pytest.mark.parametrize("field,value", [("market", "ph"), ("model", "base"), ("samples", 5)])
def test_wrong_worker_configuration_is_not_used(monkeypatch, field, value):
    def worker(script, args, **kwargs):
        result = worker_payload(args)
        result[field] = value
        return result

    monkeypatch.setattr("core.kronos_confirm.run_worker", worker)
    gate = KronosConfirmGate(mode="confirm")
    gate.queue(_signal(), _candle(), None)
    row = gate.flush()[0]
    assert row[2].decision == "no_opinion"
    assert "settings differ" in row[3]["reason"]


def test_per_symbol_worker_failure_is_not_misreported_as_a_date_mismatch(monkeypatch):
    def worker(script, args, **kwargs):
        result = worker_payload(args)
        result['results'][0] = dict(symbol='BAD', returns=[], error='ValueError: invalid OHLC context')
        return result

    monkeypatch.setattr('core.kronos_confirm.run_worker', worker)
    gate = KronosConfirmGate(mode='confirm', exit_mode='exit')
    gate.queue(_signal(symbol='BAD'), _candle(), None)
    gate.queue(_signal(symbol='GOOD'), _candle(), None)
    p = SimpleNamespace(symbol='BAD', timeframe='1d', action='SELL')
    gate.prepare_exit([p], _candle(), None)
    entries, exits = gate.flush_with_exits()
    assert entries[0][2].decision == exits[0][1].decision == 'no_opinion'
    assert 'invalid OHLC context' in entries[0][3]['reason']
    assert 'date differs' not in entries[0][3]['reason']
    assert entries[1][2].decision == 'confirm'
    assert len(gate._cache) == 1


def test_daily_worker_atr_is_used_for_weekly_signal_band(monkeypatch):
    monkeypatch.setattr("core.kronos_confirm.run_worker", lambda script, args, **kw: worker_payload(args))
    s = _signal()
    s.timeframe = "1W"
    frame = pd.DataFrame(dict(high=[120.] * 60, low=[80.] * 60, close=[100.] * 60))
    gate = KronosConfirmGate(mode="confirm")
    gate.queue(s, _candle(), frame)
    row = gate.flush()[0]
    assert row[3]["atr_pct"] == 2
    assert row[3]["band_pct"] == pytest.approx(.7)
    assert row[3]["forecast_horizon"] == "1d"


def test_revised_anchor_close_invalidates_cached_votes():
    fetch = Mock(side_effect=_vote(.01, .02, -.01))
    gate = KronosConfirmGate(mode="confirm", fetch_fn=fetch)
    for close in (100, 101):
        gate.queue(_signal(), _candle(close), None)
        gate.flush()
    assert fetch.call_count == 2


def test_missing_bar_context_cannot_bypass_entry_confirmation():
    fetch = Mock(side_effect=AssertionError("No worker call without a dated bar"))
    scanner = _scanner("confirm", fetch)
    asyncio.run(scanner._finish_signal(_signal(), None, None))
    asyncio.run(scanner._flush_kronos_confirm())
    scanner._queue_entry.assert_not_awaited()
    fetch.assert_not_called()


@pytest.mark.parametrize("action", ["BUY", "SELL"])
@pytest.mark.parametrize("corruption", [None, "missing", "shadow", "date", "action", "market", "votes", "direction"])
def test_saved_confirmations_are_validated_at_fill_without_new_forecasts(monkeypatch, tmp_path, action, corruption):
    monkeypatch.setattr(settings, "short_entries_enabled", True)
    book = PaperAccount(market="us", txn_cost_pct=.001)
    book.assume_session_open = True
    scanner = MarketScanner(symbols=["TEST"], market="us", paper_account=book,
                            kronos_gate_mode="confirm", kronos_exit_mode="off", volume_gate=False)
    scanner._append_signal_log = Mock()
    returns = (.01, .02, -.01) if action == "BUY" else (-.01, -.02, .01)
    scanner._kronos_gate._fetch_fn = Mock(side_effect=_vote(*returns))
    s, decision = _signal(action=action), _candle()
    s.provenance = "patternsv2"
    s.pattern_evidence = {'entry_boundary_policy': 'frozen-breakout-v1',
                          'breakout_level': 100, 'direction': -1 if action == 'SELL' else 1}
    s.stop_loss, s.take_profit = (120, 80) if action == "SELL" else (80, 120)
    asyncio.run(scanner._finish_signal(s, None, decision))
    asyncio.run(scanner._flush_kronos_confirm())
    assert len(book.pending_entries) == 1
    pending = book.pending_entries[0]
    if corruption == "missing":
        pending.kronos_evidence = None
    elif corruption:
        key, value = {"shadow": ("mode", "shadow"), "date": ("anchor_date", "2026-10-29"),
                      "action": ("action", "CLOSE"), "market": ("market", "ph"),
                      "direction": ("direction", "flat"),
                      "votes": ("returns", [-v for v in returns])}[corruption]
        pending.kronos_evidence[key] = value
    path = tmp_path / "paper.json"
    book.save(path)
    book = PaperAccount.load(path, market="us")
    book.assume_session_open = True
    opening = deepcopy(decision)
    opening.timestamp += timedelta(days=3)
    outcomes = book.fill_pending("TEST", "1d", opening, OHLCVStore(), entry_check=scanner._check_kronos_entry)
    assert outcomes[0][1] is (corruption is None)
    assert book.open_count() == int(corruption is None)
    assert scanner._kronos_gate._fetch_fn.call_count == 1  # No fill-session lookahead.
    if corruption:
        assert "Kronos" in outcomes[0][2]
        assert book.cash == book.initial_capital


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_safety_stop_closes_without_kronos_permission_or_worker(monkeypatch, action):
    monkeypatch.setattr(settings, "short_entries_enabled", True)
    book = PaperAccount(market="us", txn_cost_pct=.001)
    book.assume_session_open = True
    s, initial = _signal(action=action), _candle()
    s.provenance = "patternsv2"
    s.pattern_evidence = {'entry_boundary_policy': 'frozen-breakout-v1',
                          'breakout_level': 100, 'direction': -1 if action == 'SELL' else 1}
    s.stop_loss, s.take_profit = (120, 80) if action == "SELL" else (80, 120)
    assert book.open_position(s, initial, OHLCVStore())[0]
    scanner = MarketScanner(symbols=["TEST"], market="us", paper_account=book,
                            kronos_gate_mode="confirm", kronos_exit_mode="exit", volume_gate=False)
    fetch = scanner._kronos_gate._fetch_fn = Mock(side_effect=AssertionError("Protection must not wait for Kronos"))
    bar = deepcopy(initial)
    bar.timestamp += timedelta(days=3)
    bar.high, bar.low = 110, 90
    scanner._scan_snapshot_symbols, scanner._scan_daily_dates = set(), set()
    scanner._apply_bar("TEST", "1d", _make_snapshot("TEST", "1d", bar))
    assert book.open_count() == 0
    assert book.closed[0].pnl_pct == pytest.approx(-3)
    asyncio.run(scanner._flush_kronos_confirm())
    fetch.assert_not_called()
