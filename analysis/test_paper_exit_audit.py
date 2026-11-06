"""Portable audit regressions; no production ledger/tape/database required."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from analysis.paper_exit_audit import audit_export, replay_trade, verify_close
from core.backtester import _open_trade
from core.test_patternsv2_exits import candle, signal


def fixture(short=False):
    s = signal(short)
    s.trailing_stop_pct = .03
    s.trailing_activation_pct = .0525
    s.take_profit = 94 if short else 106
    s.stop_loss_pct_cap = .03
    entry = candle(100, 101, 99, 100)
    p = _open_trade(s, replace(entry, high=100, low=100), 0, execution_model='causal-v1')
    trade = {
        'symbol': 'TEST', 'action': s.action, 'timeframe': '1d', 'pattern': s.pattern,
        'provenance': 'patternsv2', 'trade_id': p.trade_id, 'execution_model': 'causal-v1',
        'resolved_rules': p.resolved_rules, 'requested_rules': {'take_profit': 70 if short else 130},
        'sim_opened': entry.timestamp.isoformat(), 'daily_marks': [],
    }
    return trade, entry


@pytest.mark.parametrize('short', [False, True])
def test_next_open_entry_can_close_on_entry_day_and_target_preempts_trail(short):
    trade, entry = fixture(short)
    entry = replace(entry, low=93, high=101, close=94.5) if short else replace(
        entry, low=99, high=107, close=105.5)
    row = replay_trade(trade, [entry], entry.timestamp)
    assert row['exit_reason'] == 'take_profit'
    assert row['same_session_exit'] and row['bars'] == 1
    assert not row['trailing_armed']
    assert row['net_r'] < 2  # gross 2R includes both fees in net R
    assert row['pnl'] == pytest.approx(580.6 if short else 579.4)


def test_future_bars_excluded_and_open_remainder_charged_liquidation_costs():
    trade, entry = fixture()
    future = candle(100, 135, 99, 130, 1)
    row = replay_trade(trade, [entry, future], entry.timestamp)
    assert row['exit_reason'] == 'open'
    assert row['realized_pnl'] == 0
    assert row['open_liquidation_pnl'] == pytest.approx(-20)
    assert row['last_bar'] == entry.timestamp.isoformat()


def test_restoring_requested_target_preserves_protection_and_allows_trail():
    trade, entry = fixture()
    bars = [replace(entry, high=107, close=106), candle(106, 107, 101, 102, 1)]
    baseline = replay_trade(trade, bars, bars[-1].timestamp)
    alternate = replay_trade(trade, bars, bars[-1].timestamp, scenario='requested_target')
    assert baseline['exit_reason'] == 'take_profit'
    assert alternate['exit_reason'] == 'trailing_stop'
    assert alternate['trailing_armed']
    assert alternate['fill'] == pytest.approx(102.82)
    assert trade['resolved_rules']['take_profit'] == 106


def test_ambiguous_intrabar_stop_is_reported_but_open_target_is_unambiguous():
    trade, entry = fixture()
    entry = replace(entry, high=107, low=96)
    row = replay_trade(trade, [entry], entry.timestamp)
    assert row['exit_reason'] == 'stop_loss'
    assert row['ambiguous_bars'] == [entry.timestamp.isoformat()]
    gap = candle(107, 108, 96, 106, 1)
    row = replay_trade(trade, [candle(100, 101, 99, 100), gap], gap.timestamp)
    assert row['exit_reason'] == 'take_profit'
    assert not row['ambiguous_bars']


def test_runtime_drift_and_unverified_baselines_fail_closed():
    trade, entry = fixture()
    trade['resolved_rules']['engine_sha256'] = 'changed'
    with pytest.raises(ValueError, match='runtime differs'):
        replay_trade(trade, [entry], entry.timestamp)
    with pytest.raises(ValueError, match='does not reproduce'):
        verify_close({'symbol': 'TEST', 'sim_opened': '', 'sim_closed': 'x'},
                     {'exit_date': None})


def test_multiple_tape_revisions_must_agree_and_source_is_unchanged(tmp_path):
    trade, entry = fixture()
    bars = [replace(entry, high=107, close=106)]
    row = replay_trade(trade, bars, bars[-1].timestamp)
    trade.update(sim_closed=row['exit_date'], exit=row['fill'], pnl=row['pnl'],
                 exit_reason=row['exit_reason'], hold_bars=row['bars'])
    export = tmp_path / 'export.json'
    export.write_text(json.dumps({'exported_at': (entry.timestamp + timedelta(days=1)).isoformat(),
                                 'books': [{'market': 'us', 'currency': 'USD',
                                            'closed_trades': [trade], 'open_positions': []}]}))
    original = export.read_bytes()
    directory = tmp_path / 'tapes' / 'us'
    directory.mkdir(parents=True)
    def write_tape(name, bar):
        path = directory / name
        path.write_text(json.dumps({'rows': [{'timestamp': bar.timestamp.timestamp(),
                                              **{k: getattr(bar, k) for k in ('open', 'high', 'low', 'close', 'volume')}}]}))
    write_tape('TEST_one.json', bars[0])
    result = audit_export(export, tmp_path / 'tapes')
    assert result['books'][0]['baseline_verified']
    assert export.read_bytes() == original
    write_tape('TEST_two.json', replace(bars[0], high=108))
    with pytest.raises(ValueError, match='Conflicting verified OHLC'):
        audit_export(export, tmp_path / 'tapes')
