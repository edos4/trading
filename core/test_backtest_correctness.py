"""Deterministic regressions for the backtest/paper correctness plan."""
from zoneinfo import ZoneInfo

import pytest

from core.backtester import _check_exit, _core_backtest_symbol, _open_trade
from core.test_patternsv2_exits import candle, signal
from patterns import _dedup


def test_legacy_tape_keeps_original_indices_beyond_512():
    bars = [candle(100+i, 100+i, 100+i, 100+i, i) for i in range(600)]
    checked = []

    class Probe:
        name = 'probe'
        timeframes = ['1d']
        HORIZON_BARS = 1

        def analyze(self, snapshot, store):
            df = store.get_df(snapshot.symbol, snapshot.timeframe)
            i = _dedup.current_bar(len(df)-1)
            assert df.iloc[i]['close'] == snapshot.candle.close
            assert df.index[i].date() == snapshot.candle.timestamp.astimezone(ZoneInfo("America/New_York")).date()
            checked.append(i)

    _core_backtest_symbol('TEST', '1d', bars, [Probe()], {'market': 'us'})
    assert 30 in checked and 512 in checked and 598 in checked


@pytest.mark.parametrize('short', [False, True])
def test_causal_legacy_stop_gap_fills_at_open(short):
    s = signal(short)
    s.provenance = 'legacy_unknown'
    p = _open_trade(s, candle(100,100,100,100), 0, execution_model='causal-v1')
    bar = candle(110,115,108,112) if short else candle(90,92,85,88)
    assert _check_exit(bar, p, 1) == (bar.open, 'stop_loss')


def test_causal_trail_cannot_execute_against_earlier_range():
    s = signal()
    s.provenance = 'legacy_unknown'
    s.trailing_activation_pct = 0
    p = _open_trade(s, candle(100,100,100,100), 0, execution_model='causal-v1')
    assert _check_exit(candle(100,111,99,110), p, 1) == (None, '')
    assert _check_exit(candle(110,111,103,105), p, 2) == (104.5, 'trailing_stop')


def test_close_condition_waits_for_next_open_and_survives_serialization():
    from core.paper_trader import _trade_from_dict, _trade_to_dict
    s = signal()
    s.provenance = 'legacy_unknown'
    s.stop_loss_on_close = True
    s.trailing_stop_pct = None
    p = _open_trade(s, candle(100,100,100,100), 0, execution_model='causal-v1')
    assert _check_exit(candle(100,101,90,92), p, 1) == (None, '')
    p = _trade_from_dict(_trade_to_dict(p))
    assert _check_exit(candle(88,90,85,89), p, 2) == (88, 'stop_loss')


def test_pending_entry_persists_and_charges_each_fee_once(tmp_path):
    from core.paper_trader import PaperAccount
    from data.ohlcv_store import OHLCVStore
    account = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=0)
    account.assume_session_open = True
    store = OHLCVStore()
    s = signal()
    assert account.queue_position(s, candle(100,100,100,100), store)[0]
    path = tmp_path / 'book.json'
    account.save(path)
    account = PaperAccount.load(path, market='us')
    account.assume_session_open = True
    account.fill_pending('TEST','1d',candle(100,100,100,100),store)
    assert account.open_count() == 0
    next_bar = candle(110,112,109,111,1)
    account.fill_pending('TEST','1d',next_bar,store)
    p = account.latest_position('TEST')
    assert p.entry_price == 110
    assert p.qty == 90
    assert account.equity() == pytest.approx(account.initial_capital-9.9)
    assert account.snapshot_metrics()['unrealized_pnl_dollars'] == pytest.approx(-9.9)
    account.save(path)
    account = PaperAccount.load(path, market='us')
    account.assume_session_open = True
    account.fill_pending('TEST','1d',next_bar,store)
    assert account.open_count() == 1
    # Force a resting target at the unchanged entry for a flat-price exit.
    p = account.latest_position('TEST')
    p.take_profit = 110
    closed = account.on_bar('TEST',candle(110,110,110,110,2),'1d')
    assert len(closed) == 1
    assert closed[0].pnl_usd == pytest.approx(-19.8)
    assert account.cash == pytest.approx(account.initial_capital-19.8)


def test_expired_or_gap_invalid_pending_orders_are_reported():
    from core.paper_trader import PaperAccount
    from data.ohlcv_store import OHLCVStore
    for next_bar in (candle(60,65,55,60,1), candle(100,100,100,100,8)):
        account = PaperAccount(market='us')
        account.assume_session_open = True
        store = OHLCVStore()
        assert account.queue_position(signal(),candle(100,100,100,100),store)[0]
        account.fill_pending('TEST','1d',next_bar,store)
        assert not account.pending_entries and account.open_count() == 0
        assert account.order_events[-1]['status'] == 'rejected'


def test_multisymbol_scanner_and_replay_match_top_four_gates_and_equity(monkeypatch, tmp_path):
    import asyncio
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from core.backtest_params import settings_from_values
    from core.paper_trader import PaperAccount
    from core.scanner import MarketScanner
    from core.stream_backtest import StreamReplay
    from core.backtester import _make_snapshot
    from data.tv_client import OHLCVCandle
    import analysis.price_volume
    symbols = ['AAA','BBB','CCC','DDD','EEE','FFF']
    # Flat bars keep the DII inside its 40–60% band for BUY, with 100+ candles.
    bars = [OHLCVCandle(open=100, high=101, low=99, close=100, volume=1000,
                       timestamp=datetime(2024,1,1,21,tzinfo=timezone.utc)+timedelta(days=i))
            for i in range(130)]
    fire = 120

    class Pattern:
        name = 'double_bottom'
        timeframes = ['1d']
        MIN_BARS = 2

        def analyze(self, snapshot, store):
            if snapshot.candle.timestamp != bars[fire].timestamp:
                return None
            s = signal()
            s.symbol = snapshot.symbol
            s.setup_key = (0,1)
            return s

    monkeypatch.setattr(analysis.price_volume, 'volume_confirm_gate',
                        lambda s, store, **kwargs: SimpleNamespace(passed=s.symbol!='AAA', reason='fixture'))
    import core.scanner as scanner_module
    monkeypatch.setattr(scanner_module, 'volume_confirm_gate', analysis.price_volume.volume_confirm_gate)
    account = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=.001)
    account.assume_session_open = True
    scanner = MarketScanner(symbols=symbols, paper_account=account, market='us',
                            volume_gate=True, collect_first=True, collect_first_top_n=4, pattern_only=True)
    scanner._scan_snapshot_symbols = set()
    scanner._scan_daily_dates = set()
    scanner._patterns = [Pattern()]
    scanner._record_detection = lambda s: None
    decisions=[]
    scanner._append_signal_log = lambda signal, **kw: decisions.append((signal.symbol,kw['status']))
    for i, bar in enumerate(bars):
        snapshots=[]
        for symbol in symbols:
            scanner._store.apply_candle(symbol,'1d',bar)
            _, snapshot = scanner._apply_bar(symbol,'1d',_make_snapshot(symbol,'1d',bar))
            if snapshot is not None and i>=fire:
                snapshots.append(snapshot)
        asyncio.run(scanner._run_analyzes(snapshots))
        asyncio.run(scanner._flush_collect_first())
    rows=[[b.timestamp.isoformat(),b.open,b.high,b.low,b.close,b.volume] for b in bars]
    settings=settings_from_values(dict(mode='historical-stream', market='us', symbols=symbols,
                                      start_date=bars[fire].timestamp.date().isoformat(),
                                      end_date=bars[-1].timestamp.date().isoformat(),warmup_bars=fire,
                                      txn_cost_pct=.001,slippage_pct=.001,collect_first=4,volume_gate=True))
    replay=StreamReplay(dataset={s:rows for s in symbols}, patterns=[Pattern()],settings=settings,
                        session_tz='America/New_York')
    result=replay.run()
    paper_positions=sorted((sym,p.entry_price,p.qty,p.entry_fee_paid) for sym,p in account.positions_snapshot())
    replay_positions=sorted((sym,p.entry_price,p.qty,p.entry_fee_paid) for sym,ss in replay.series.items()
                            for p in ss.open_positions)
    assert paper_positions == replay_positions
    assert [p[0] for p in paper_positions] == ['BBB','CCC','DDD']
    assert account.equity() == pytest.approx(result['equity'][-1]['equity'])
    assert sorted(decisions) == sorted((e['symbol'],e['status']) for e in result['signals']['events']
                                       if e['status'] in ('queued','filled','rejected'))


def test_stable_anchor_identity_survives_window_eviction(tmp_path):
    from core.paper_trader import PaperAccount
    from data.ohlcv_store import OHLCVStore
    store=OHLCVStore(window=512)
    bars=[candle(100,100,100,100,i) for i in range(600)]
    store.replace_all('TEST','1d',bars[:512])
    account=PaperAccount(market='us')
    s=signal(); s.setup_key=(100,110)
    assert account.queue_position(s,bars[511],store)[0]
    for bar in bars[512:]:
        store.apply_candle('TEST','1d',bar)
    s=signal(); s.setup_key=(12,22)
    assert not account.queue_position(s,bars[-1],store)[0]


def test_causal_earnings_gate_cannot_read_future_occurrence_dates(monkeypatch):
    import pandas as pd
    from patterns import _rules
    _dedup.reset()
    monkeypatch.setattr(_rules,'_earnings_cache',lambda: pytest.fail('Future earnings cache was read'))
    assert _rules.earnings_blackout(pd.DataFrame(), 'TEST', 0, 15) is None


def test_replay_window_is_part_of_frozen_effective_identity():
    from core.backtest_params import effective_parameters, settings_from_values
    values=dict(mode='historical-stream', symbols=['TEST'], start_date='2024-01-01',
                end_date='2024-06-01')
    first=effective_parameters(settings_from_values(values))
    second=effective_parameters(settings_from_values({**values,'start_date':'2024-02-01'}))
    assert first != second
    names=[p.name for p in first]
    assert len(names) == len(set(names))


@pytest.mark.parametrize('short', [False, True])
def test_entry_day_protection_and_two_leg_costs(short):
    from core.paper_trader import PaperAccount
    from data.ohlcv_store import OHLCVStore
    account=PaperAccount(market='us',txn_cost_pct=.001,slippage_pct=0)
    account.assume_session_open=True
    store=OHLCVStore()
    assert account.queue_position(signal(short),candle(100,100,100,100),store)[0]
    bar=candle(100,110,90,100,1)
    account.fill_pending('TEST','1d',bar,store)
    assert account.equity() == pytest.approx(account.initial_capital-10)
    closed=account.on_bar('TEST',bar,'1d')
    assert len(closed)==1
    expected_fill=105 if short else 95
    assert closed[0].exit_price == expected_fill
    assert account.cash == pytest.approx(account.initial_capital-500-(100+expected_fill)*.001*100)
    assert closed[0].position_marks[-1]['mtm'] == pytest.approx(closed[0].pnl_usd)


def test_old_book_keeps_deferred_fees_and_legacy_rules(tmp_path):
    import json
    from core.paper_trader import PaperAccount, _trade_to_dict
    original=signal()
    original.provenance='legacy_unknown'
    trade=_open_trade(original,candle(100,100,100,100),0)
    raw=_trade_to_dict(trade)
    for key in ('execution_model','entry_fee_paid','decision_date','pending_exit_reason'):
        raw.pop(key)
    path=tmp_path/'old.json'
    path.write_text(json.dumps(dict(market='us',initial_capital=100000,cash=90000,
                                   txn_cost_pct=.001,positions={'TEST':[raw]})))
    account=PaperAccount.load(path,market='us')
    assert account.latest_position('TEST').execution_model=='legacy'
    assert account.equity()==100000
    assert account.execution_model=='causal-v1'
    closed=account.on_bar('TEST',candle(90,92,85,88,1),'1d')
    assert closed[0].exit_price==95  # The original legacy contract is retained.
    assert account.cash==pytest.approx(account.initial_capital+closed[0].pnl_usd)
    account.save(path)
    assert PaperAccount.load(path,market='us').cash == account.cash


def test_changed_engine_is_never_allowlisted_for_causal_positions():
    position=_open_trade(signal(),candle(100,100,100,100),0,execution_model='causal-v1')
    position.resolved_rules['engine_sha256']='f7826eea5c0be44afd549d892ddd4ca4d54860ce1dd1ae91935ddaa526d9c83b'
    with pytest.raises(RuntimeError,match='runtime changed'):
        _check_exit(candle(90,92,85,88,1),position,1)


def test_pending_filter_rechecks_at_execution_and_failure_does_not_skip_exits():
    from core.paper_trader import PaperAccount
    from data.ohlcv_store import OHLCVStore
    account=PaperAccount(market='us',txn_cost_pct=0)
    account.assume_session_open=True
    store=OHLCVStore()
    candidate=signal()
    candidate.laya_assessment={'mode':'filter','outcome':'allow'}
    assert account.queue_position(candidate,candle(100,100,100,100),store)[0]
    bar=candle(100,105,99,103,1)
    def fail(*args):
        raise RuntimeError('Model unavailable')
    outcomes=account.fill_pending('TEST','1d',bar,store,recheck=fail)
    assert len(outcomes)==1 and not outcomes[0][1]
    assert 'Model unavailable' in outcomes[0][2]
    assert account.open_count()==0


def test_invalid_frozen_prices_are_rejected():
    from core.stream_backtest import rows_to_candles
    from core.pattern_edit_store import EditError
    with pytest.raises(EditError,match='invalid OHLCV'):
        rows_to_candles([['2024-01-01T21:00:00Z',100,90,99,100,1000]])


@pytest.mark.parametrize('short', [False, True])
def test_causal_protection_wins_ambiguity_but_open_target_has_precedence(short):
    p=_open_trade(signal(short),candle(100,100,100,100),0,execution_model='causal-v1')
    ambiguous=candle(100,110,65,90,1) if short else candle(100,135,90,110,1)
    assert _check_exit(ambiguous,p,1)==(105 if short else 95,'stop_loss')
    p=_open_trade(signal(short),candle(100,100,100,100),0,execution_model='causal-v1')
    gap=candle(65,110,60,90,1) if short else candle(135,140,90,110,1)
    assert _check_exit(gap,p,1)==(gap.open,'take_profit')


def test_last_bar_signal_is_unfilled_and_never_fabricates_an_entry():
    from core.stream_backtest import StreamReplay
    from core.backtest_params import settings_from_values
    rows=[[b.timestamp.isoformat(),b.open,b.high,b.low,b.close,b.volume]
          for b in (candle(100,100,100,100,i) for i in range(32))]
    class LastBar:
        name='double_bottom'
        timeframes=['1d']
        def analyze(self,snapshot,store):
            if snapshot.candle.timestamp.isoformat() == rows[-1][0]:
                return signal()
    settings=settings_from_values(dict(mode='historical-stream',symbols=['TEST'],
                                       start_date='2026-01-01',end_date='2026-02-10'))
    result=StreamReplay(dataset={'TEST':rows},patterns=[LastBar()],settings=settings,
                        session_tz='America/New_York').run()
    assert result['metrics']['trade_count']==0 and result['metrics']['open_count']==0
    assert any(e['status']=='unfilled' for e in result['signals']['events'])


def test_execution_recheck_cannot_see_the_rest_of_the_bar():
    from core.execution import opening_observation
    observed=opening_observation(candle(100,150,80,140,1),'us')
    assert (observed.open,observed.high,observed.low,observed.close,observed.volume)==(100,100,100,100,0)
    assert (observed.timestamp.hour,observed.timestamp.minute)==(9,30)


def test_pending_runtime_mismatch_is_rejected_without_a_fill():
    from core.paper_trader import PaperAccount
    from data.ohlcv_store import OHLCVStore
    account=PaperAccount(market='us')
    account.assume_session_open=True
    store=OHLCVStore()
    assert account.queue_position(signal(),candle(100,100,100,100),store)[0]
    account.pending_entries[0].requested_rules['engine_sha256']='obsolete'
    outcomes=account.fill_pending('TEST','1d',candle(100,101,99,100,1),store)
    assert not outcomes[0][1] and 'runtime changed' in outcomes[0][2]
    assert account.open_count()==0


def test_unanchored_signals_are_deduplicated_per_decision_bar():
    from core.paper_trader import PaperAccount
    from data.ohlcv_store import OHLCVStore
    account=PaperAccount(market='us')
    store=OHLCVStore()
    candidate=signal()
    candidate.setup_key=None
    assert account.queue_position(candidate,candle(100,100,100,100),store)[0]
    assert not account.queue_position(candidate,candle(100,100,100,100),store)[0]
    assert account.queue_position(candidate,candle(100,100,100,100,1),store)[0]


def test_multiple_prints_in_one_session_use_only_the_last_print():
    from core.stream_backtest import StreamReplay
    from core.backtest_params import settings_from_values
    from datetime import datetime, timezone
    rows=[['2024-01-02T14:30:00Z',100,101,99,100,1000],
          ['2024-01-02T21:00:00Z',100,102,99,101,2000]]
    settings=settings_from_values(dict(mode='historical-stream',symbols=['TEST'],
                                       start_date='2024-01-02',end_date='2024-01-02'))
    replay=StreamReplay(dataset={'TEST':rows},patterns=[],settings=settings,
                        session_tz='America/New_York')
    assert len(replay.series['TEST'].candles)==1
    assert replay.series['TEST'].candles[0].close==101
    assert replay.events[0]['status']=='duplicate-bar'


def test_nonfinite_saved_entry_fee_is_rejected():
    from core.paper_trader import _trade_from_dict, _trade_to_dict
    trade=_open_trade(signal(),candle(100,100,100,100),0)
    raw=_trade_to_dict(trade)
    raw['entry_fee_paid']=float('nan')
    with pytest.raises(ValueError,match='entry fee'):
        _trade_from_dict(raw)
