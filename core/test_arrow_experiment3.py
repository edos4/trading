"""Arrow experiment resets are atomic, causal, and scored with a frozen flat band."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from core import arrow_experiment3 as exp

ANCHOR = date(2026, 11, 11)
NOW = datetime.fromisoformat('2026-11-12T07:00:00-05:00')


def metadata():
    return {**exp.schedule(ANCHOR), 'engine': 'Arrow', 'run_id': 'test-run',
            'model': {'version': 'test'}, 'generated_at': NOW.isoformat()}


def result(symbol='ARROW_TEST', rank=1):
    return {'symbol': symbol, 'rank': rank,
            'anchor': {'time': '2026-11-11', 'value': 100.},
            'history': [{'time': '2026-11-11', 'value': 100.}],
            'actuals': [],
            'forecasts': [
                {'horizon': h, 'date': metadata()['target_dates'][str(h)],
                 'direction': 'up' if h == 1 else 'flat', 'abstained': False,
                 'reasons': [], 'probabilities': {'up': .6, 'down': .1, 'flat': .3} if h == 1
                 else {'up': .2, 'down': .1, 'flat': .7}, 'flat_band_return': .01}
                for h in exp.HORIZONS]}


def universe():
    return {'as_of': ANCHOR.isoformat(), 'captured_at': NOW.isoformat(),
            'stocks': [{'symbol': f'ARROW_TEST_{i}', 'rank': i + 1, 'exchange': 'NASDAQ',
                        'type': 'stock', 'close': 100., 'dollar_volume': 10000. - i}
                       for i in range(50)]}


def prepared():
    snapshot = universe()
    inputs = [{**row, 'bars': [{'time': ANCHOR.isoformat(), 'close': 100.}]} for row in snapshot['stocks']]
    payload = {'model': {'name': 'Arrow', 'version': 'test'}, 'settings': {},
               'results': [result(row['symbol'], row['rank']) for row in inputs]}
    return snapshot, inputs, payload


def test_schedule_uses_one_and_three_trading_sessions():
    plan = exp.schedule(ANCHOR)
    assert plan['target_dates'] == {'1': '2026-11-12', '3': '2026-11-16'}
    assert plan['deadline'] == '2026-11-17T09:30:00-05:00'
    assert exp.schedule(date(2026, 11, 25))['target_dates'] == {'1': '2026-11-27', '3': '2026-12-01'}
    with pytest.raises(ValueError):
        exp.schedule(date(2026, 11, 14))


@pytest.mark.parametrize('now,expected', [
    ('2026-11-12T16:29:59-05:00', []),
    ('2026-11-12T16:30:00-05:00', ['2026-11-12']),
    ('2026-11-14T12:00:00-05:00', ['2026-11-12']),
    ('2026-11-16T16:30:00-05:00', ['2026-11-12', '2026-11-16']),
    ('2026-11-17T09:29:59-05:00', ['2026-11-12', '2026-11-16']),
    ('2026-11-17T09:30:00-05:00', []),
])
def test_collection_only_requests_closed_targets(now, expected):
    assert [d.isoformat() for d in exp.due_targets(metadata(), datetime.fromisoformat(now))] == expected


def test_universe_is_fifty_stocks_captured_before_open():
    exp.validate_universe(universe(), ANCHOR)
    for mutate in (
        lambda p: p['stocks'].pop(),
        lambda p: p['stocks'][0].update(type='fund'),
        lambda p: p['stocks'][1].update(symbol=p['stocks'][0]['symbol']),
        lambda p: p['stocks'][0].update(dollar_volume=float('nan')),
        lambda p: p.update(captured_at='2026-11-12T09:30:00-05:00'),
        lambda p: p.update(as_of='2026-11-10'),
    ):
        broken = universe()
        mutate(broken)
        with pytest.raises(ValueError):
            exp.validate_universe(broken, ANCHOR)
    with pytest.raises(ValueError, match='before the next'):
        exp.capture_universe(ANCHOR, datetime.fromisoformat('2026-11-12T09:30:00-05:00'))


@pytest.mark.parametrize('close,actual,correct', [(100., 'flat', False), (100.5, 'flat', False),
                                              (101., 'up', True), (99., 'down', False)])
def test_flat_uses_frozen_atr_band_including_its_boundary(close, actual, correct):
    row = result()
    row['actuals'] = [{'time': '2026-11-12', 'value': close}]
    first, third = exp.direction_results(row)
    assert first['actual'] == actual and first['correct'] is correct
    assert third['actual'] is None and third['correct'] is None


def test_three_day_actual_compares_to_anchor_and_abstentions_are_excluded():
    row = result()
    row['actuals'] = [{'time': '2026-11-12', 'value': 110.}, {'time': '2026-11-16', 'value': 100.5}]
    abstained = deepcopy(row)
    abstained['symbol'] = 'ABSTAINED'
    for f in abstained['forecasts']:
        f.update(direction=None, abstained=True, probabilities=None, reasons=['short history'])
    first, third = exp.score([row, abstained], metadata())['arrow']
    assert (first['observed'], first['count'], first['correct'], first['abstained'], first['pending']) == (2, 1, 1, 1, 0)
    assert first['accuracy_pct'] == 100
    assert third['correct'] == 1 and exp.direction_results(row)[1]['actual'] == 'flat'


def test_report_pending_and_actuals_do_not_change_frozen_predictions(tmp_path):
    row = result()
    row['actuals'] = [{'time': '2026-11-12', 'value': 101.}]
    saved = {'status': 'collecting', 'metadata': metadata(), 'direction_report': exp.score([row], metadata())['arrow']}
    path = exp.save_direction_report(saved, tmp_path / 'report.md')
    assert '| 2026-11-12 | +1D | 1/1 | 1 | 1 | 0 | 0 | 0 | 100.0% |' in path.read_text()
    assert '| 2026-11-16 | +3D | 0/1 | 0 | 0 | 0 | 0 | 1 | Pending |' in path.read_text()
    stamp = path.stat().st_mtime_ns
    exp.save_direction_report(saved, path)
    assert path.stat().st_mtime_ns == stamp
    assert 'No saved Arrow' in exp.render_direction_report({'status': 'not_started'})


def test_incomplete_wrong_horizon_future_or_invalid_scores_are_rejected():
    _, inputs, payload = prepared()
    exp.validate_predictions(payload, inputs, metadata())
    for mutate in (
        lambda p: p['results'].pop(),
        lambda p: p['results'][0]['forecasts'][0].update(horizon=2),
        lambda p: p['results'][0]['forecasts'][0].update(date='2026-11-13'),
        lambda p: p['results'][0]['history'].append({'time': '2026-11-12', 'value': 100.}),
        lambda p: p['results'][0]['forecasts'][0].update(direction='down'),
        lambda p: p['results'][0]['forecasts'][0]['probabilities'].update(up=float('nan')),
        lambda p: p['results'][0]['forecasts'][0].update(flat_band_return=0),
        lambda p: p['results'][0]['forecasts'][0].update(abstained=True),
    ):
        broken = deepcopy(payload)
        mutate(broken)
        with pytest.raises(ValueError):
            exp.validate_predictions(broken, inputs, metadata())


def test_start_reuses_saved_predictions_and_refuses_late_reset(monkeypatch):
    frozen = {'status': 'complete'}
    monkeypatch.setattr(exp, 'read_experiment', lambda now: frozen)
    fetch = Mock(side_effect=AssertionError('must not refetch'))
    monkeypatch.setattr(exp, 'load_inputs', fetch)
    assert exp.start() is frozen
    with pytest.raises(ValueError, match='creation closed'):
        exp.start(datetime.fromisoformat('2026-11-12T09:30:00-05:00'), reset=True, as_of=ANCHOR)
    assert not fetch.called


@pytest.fixture
def store(monkeypatch):
    """Execute production SQL in a disposable schema, including read-only snapshots."""
    from data.db import get_conn
    try:
        connection = get_conn()
    except Exception as exc:
        pytest.skip(f'Local PostgreSQL unavailable: {type(exc).__name__}')
    schema = 'arrow_experiment3_test_' + uuid4().hex

    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def __enter__(self): self.cursor.__enter__(); return self
        def __exit__(self, *args): return self.cursor.__exit__(*args)
        def execute(self, query, params=None):
            return self.cursor.execute(query.replace('kronos_experiment3_arrow', schema), params)
        def fetchone(self): return self.cursor.fetchone()
        def fetchall(self): return self.cursor.fetchall()

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args):
            connection.rollback() if args[0] else connection.commit()
        def cursor(self): return Cursor(connection.cursor())

    wrapped = Connection()
    monkeypatch.setattr(exp, 'get_conn', lambda: wrapped)
    try:
        yield wrapped
    finally:
        connection.rollback()
        connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.commit()
        connection.close()


def insert(store, rows):
    from psycopg.types.json import Jsonb
    with store as conn, conn.cursor() as cur:
        cur.execute(exp.SCHEMA_SQL)
        cur.execute('INSERT INTO kronos_experiment3_arrow.run VALUES (1,%s)', (Jsonb(metadata()),))
        for row in rows:
            cur.execute('INSERT INTO kronos_experiment3_arrow.forecasts VALUES (%s,%s,%s)',
                        (row['symbol'], row['rank'], Jsonb(row)))


def test_database_collects_exact_targets_once_and_stops(store, monkeypatch):
    insert(store, [result()])
    original = deepcopy(result()['forecasts'])
    monkeypatch.setattr(exp, 'provider_bars', lambda symbol: [
        {'time': '2026-11-12', 'close': 101.}, {'time': '2026-11-13', 'close': 999.},
        {'time': '2026-11-16', 'close': 100.5}, {'time': '2026-11-17', 'close': 999.}])
    first = exp.collect(datetime.fromisoformat('2026-11-12T16:30:00-05:00'))
    assert first['actual_count'] == 1 and first['scores']['arrow'][1]['count'] == 0
    assert first['results'][0]['forecasts'] == original
    second = exp.collect(datetime.fromisoformat('2026-11-16T16:30:00-05:00'))
    assert second['status'] == 'complete' and second['actual_count'] == 2
    assert [a['value'] for a in second['results'][0]['actuals']] == [101., 100.5]
    assert second['results'][0]['forecasts'] == original
    fetch = Mock(side_effect=AssertionError('must stop fetching'))
    monkeypatch.setattr(exp, 'provider_bars', fetch)
    assert exp.collect(datetime.fromisoformat('2026-11-17T10:00:00-05:00'))['status'] == 'complete'
    assert not fetch.called


def test_database_missing_actuals_expire_without_fetching(store, monkeypatch):
    assert exp.read_experiment(NOW)['status'] == 'not_started'
    insert(store, [result()])
    fetch = Mock(side_effect=AssertionError('must not fetch'))
    monkeypatch.setattr(exp, 'provider_bars', fetch)
    assert exp.collect(NOW)['actual_count'] == 0
    assert exp.collect(datetime.fromisoformat('2026-11-17T09:30:00-05:00'))['status'] == 'expired_incomplete'
    assert not fetch.called


def test_collector_does_not_write_into_a_replacement_run(store, monkeypatch):
    from psycopg.types.json import Jsonb
    insert(store, [result()])
    def fetch(symbol):
        replacement = {**metadata(), 'run_id': 'replacement-run'}
        with store as conn, conn.cursor() as cur:
            cur.execute('UPDATE kronos_experiment3_arrow.run SET metadata=%s WHERE id=1', (Jsonb(replacement),))
        return [{'time': '2026-11-12', 'close': 101.}]
    monkeypatch.setattr(exp, 'provider_bars', fetch)
    saved = exp.collect(datetime.fromisoformat('2026-11-12T16:30:00-05:00'))
    assert saved['metadata']['run_id'] == 'replacement-run'
    assert saved['actual_count'] == 0


def test_reset_archives_previous_and_failed_preparation_keeps_database(store, tmp_path, monkeypatch):
    insert(store, [result()])
    snapshot, inputs, payload = prepared()
    path = tmp_path / 'universe.json'
    path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(exp, 'load_inputs', lambda *args: inputs)
    monkeypatch.setattr(exp, 'forecast_inputs', lambda *args: {'results': []})
    with pytest.raises(ValueError):
        exp.start(NOW, reset=True, as_of=ANCHOR, universe_file=path)
    assert len(exp.read_experiment(NOW)['results']) == 1
    real_now = exp.ny_now
    monkeypatch.setattr(exp, 'ny_now', lambda value=None: real_now(value or NOW))
    monkeypatch.setattr(exp, 'forecast_inputs', lambda *args: deepcopy(payload))
    saved = exp.start(NOW, reset=True, as_of=ANCHOR, universe_file=path, archive_dir=tmp_path / 'archive')
    assert len(saved['results']) == 50 and saved['actual_count'] == 0
    assert saved['metadata']['model']['version'] == 'test'
    archive = json.loads((tmp_path / 'archive/test-run.json').read_text())
    assert len(archive['forecasts']) == 1 and archive['metadata']['run_id'] == 'test-run'
    assert 'input' not in saved['results'][0]


def test_cli_cron_deadline_does_not_require_database(tmp_path, monkeypatch):
    from scripts import kronos_experiment3 as cli
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    clock = exp.ny_now
    monkeypatch.setattr(exp, 'ny_now', lambda value=None: clock(value or datetime.fromisoformat(metadata()['deadline'])))
    read = Mock(side_effect=AssertionError('must not query expired job'))
    monkeypatch.setattr(exp, 'read_experiment', read)
    update = Mock()
    monkeypatch.setattr(cli, 'update_cron', update)
    assert cli.main(['--collect', '--cron', '--stop-at', metadata()['deadline']]) == 0
    update.assert_called_once_with(False)
    assert not read.called


def test_cli_collect_refreshes_report_and_stops_at_completion(tmp_path, monkeypatch):
    from scripts import kronos_experiment3 as cli
    row = result()
    row['actuals'] = [{'time': '2026-11-12', 'value': 101.}, {'time': '2026-11-16', 'value': 100.}]
    saved = {'status': 'complete', 'metadata': metadata(), 'actual_count': 2,
             'direction_report': exp.score([row], metadata())['arrow']}
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setattr(exp, 'collect', lambda: saved)
    update = Mock()
    monkeypatch.setattr(cli, 'update_cron', update)
    assert cli.main(['--collect', '--cron']) == 0
    update.assert_called_once_with(False)
    assert '100.0%' in (tmp_path / 'logs/kronos_experiment3_direction_report.md').read_text()
