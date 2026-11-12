"""Checks for full-history weekly snapshots and durable release bookkeeping."""
from __future__ import annotations
from datetime import date, datetime, timezone
import json
import pytest
from core.kronos_weekly import snapshot_bounds, record_completion, remove_superseded


def test_bounds_and_saturday():
    assert snapshot_bounds(date(2026, 11, 14)) == (None, date(2026, 11, 14))
    assert snapshot_bounds(date(2020, 2, 29)) == (None, date(2020, 2, 29))
    with pytest.raises(ValueError):
        snapshot_bounds(date(2026, 11, 13))


def test_completion_is_pht_and_replaces(tmp_path):
    release = tmp_path / 'release'
    release.mkdir()
    (release / 'run_manifest.json').write_text('{"run_id":"example"}')
    marker = tmp_path / 'kronos-finetuned-at.txt'
    marker.write_text('old\n')
    stamp = record_completion(tmp_path, release, now=datetime(2026, 11, 13, 23, 12, 34, tzinfo=timezone.utc))
    assert marker.read_text() == '2026-11-14T07:12:34+08:00\n'
    assert json.loads((release / 'run_manifest.json').read_text())['completed_at'] == stamp
    assert not list(tmp_path.glob('.*.tmp'))


def test_cleanup_preserves_active_and_rejects_symlinks(tmp_path):
    runs = tmp_path / 'runs'
    active = runs / 'active'
    active.mkdir(parents=True)
    old = runs / 'old'
    old.mkdir()
    remove_superseded(runs, active)
    assert active.exists() and not old.exists()
    (runs / 'escape').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        remove_superseded(runs, active)
    assert active.exists()


from pathlib import Path
from datetime import timedelta
import numpy as np
import pandas as pd
from core import kronos_weekly as weekly
from learn import kronos_weekly_dataset as dataset


def frame(start='2021-11-14', end='2026-11-13', price=100.):
    index = pd.bdate_range(start, end)
    return pd.DataFrame({'open': price, 'high': price + 1, 'low': price - 1,
                         'close': price, 'volume': 1000.}, index=index)


def corpus(tmp_path, **overrides):
    options = dict(start=date(2021, 11, 14), end=date(2026, 11, 14))
    options.update(overrides)
    return dataset.build_from_frames([('us', 'AAA', frame()), ('ph', 'BDO.PS', frame(price=50))], tmp_path, **options)


def checkpoint(root):
    for component in ('tokenizer', 'predictor'):
        path = root / component / 'best_model'
        path.mkdir(parents=True)
        (path / 'weights.bin').write_bytes(component.encode())
    weekly.write_json(root / 'run_manifest.json', {'run_id': root.name, 'run_date': '2026-11-14'})
    weekly.create_selection_manifest(root, 'corpus', date(2026, 11, 14))


def layout(tmp_path, *, legacy=False):
    home, checkout = tmp_path / 'Kronos', tmp_path / 'checkout'
    checkout.mkdir()
    runs = home / 'weekly' / 'runs'
    runs.mkdir(parents=True)
    candidate = runs / 'candidate'
    checkpoint(candidate)
    active = home / 'finetuned'
    if legacy:
        checkpoint(active)
    else:
        old = runs / 'old'
        checkpoint(old)
        active.symlink_to(old)
    (checkout / 'kronos-finetuned-at.txt').write_text('previous\n')
    return home, checkout, runs, candidate


def test_full_history_snapshot_has_short_heldout_windows_and_balanced_sampling(tmp_path):
    meta = corpus(tmp_path / 'dataset', start=None)
    features, days, stored = dataset.kd.load_corpus(tmp_path / 'dataset')
    assert days.min() >= date(2021, 11, 14).toordinal()
    assert days.max() < date(2026, 11, 14).toordinal()
    for symbol in meta['symbols']:
        for split in ('train', 'val', 'eval'):
            assert symbol['ranges'][split]
            for start, count in symbol['ranges'][split]:
                assert start >= symbol['offset']
                assert start + count - 1 + 267 <= symbol['offset'] + symbol['count']
                lower = date.fromisoformat(meta['train_end'] if split == 'val' else meta['val_end'] if split == 'eval' else meta['window_start']).toordinal()
                upper = date.fromisoformat(meta['train_end'] if split == 'train' else meta['val_end'] if split == 'val' else meta['window_end']).toordinal()
                assert days[start + 256] >= lower
                assert days[start + count - 1 + 266] < upper
    samples = dataset.WeeklyWindowDataset(tmp_path / 'dataset', 256, 10, 5, 42, 1000)
    first = samples._draw()[0]
    samples.set_epoch_seed(0)
    np.testing.assert_array_equal(samples._draw()[0], first)
    for _ in range(999):
        samples._draw()
    assert 400 < samples.sample_counts['ph'] < 600
    assert all(np.isfinite(samples._draw()[0]).ravel())


def test_all_history_keeps_old_bars_and_delisted_training_symbols(tmp_path):
    sources = [(market, symbol, frame(start=first, end=last)) for market in ('us', 'ph')
               for symbol, first, last in [('LIVE', '2010-01-04', '2026-11-20'),
                                            ('OLD', '2000-01-03', '2003-12-31')]]
    meta = dataset.build_from_frames(sources, tmp_path, end=date(2026, 11, 14))
    features, days, stored = dataset.kd.load_corpus(tmp_path)
    assert meta['history_scope'] == stored['history_scope'] == 'all_available'
    assert meta['window_start'] == '2000-01-03'
    assert days.min() == date(2000, 1, 3).toordinal()
    assert days.max() == date(2026, 11, 13).toordinal()
    old_cutoff = date(2021, 11, 14).toordinal()
    for market in ('us', 'ph'):
        assert meta['report']['markets'][market]['training_only_symbols'] == 1
        for symbol in (s for s in meta['symbols'] if s['market'] == market):
            assert symbol['ranges']['train']
            assert any(days[start + 256] < old_cutoff for start, _ in symbol['ranges']['train'])
            for split, lower_key, upper_key in [('train', 'window_start', 'train_end'),
                                                 ('val', 'train_end', 'val_end'),
                                                 ('eval', 'val_end', 'window_end')]:
                for start, count in symbol['ranges'][split]:
                    assert days[start + 256] >= date.fromisoformat(meta[lower_key]).toordinal()
                    assert days[start + count - 1 + 266] < date.fromisoformat(meta[upper_key]).toordinal()


def test_invalid_rows_and_missing_market_block_promotion(tmp_path):
    bad = frame()
    bad.iloc[-1, 4] = -1
    meta = dataset.build_from_frames([('us', 'AAA', bad), ('ph', 'BDO.PS', frame())], tmp_path / 'good',
                                     start=date(2021, 11, 14), end=date(2026, 11, 14))
    assert meta['report']['rejected_rows'] == 1
    with pytest.raises(ValueError, match='ph split coverage'):
        dataset.build_from_frames([('us', 'AAA', frame())], tmp_path / 'bad',
                                  start=date(2021, 11, 14), end=date(2026, 11, 14))


def test_ranges_do_not_cross_suspension():
    dates = list(pd.bdate_range('2025-01-01', periods=300)) + list(pd.bdate_range('2026-06-01', periods=300))
    days = np.array([d.date().toordinal() for d in dates])
    for start, count in dataset.ranges_for_days(days, 0, int(days[0]), int(days[-1]) + 1, 256, 11, 'us'):
        assert start + count - 1 + 267 <= 300 or start >= 300


def test_ranges_drop_lookbacks_that_still_contain_the_old_price_scale():
    dates = list(pd.bdate_range('2024-01-02', periods=600))
    prices = np.concatenate([np.full(300, 20.), np.geomspace(20., 1., 300)])
    assert np.min(prices[1:] / prices[:-1]) > .1
    days = np.array([d.date().toordinal() for d in dates])
    kept = dataset.ranges_for_days(days, 0, int(days[0]), int(days[-1]) + 1, 256, 11, 'us', prices)
    raw = dataset.ranges_for_days(days, 0, int(days[0]), int(days[-1]) + 1, 256, 11, 'us')
    assert sum(count for _, count in raw) > sum(count for _, count in kept) > 0
    for start, count in kept:
        for offset in range(count):
            window = prices[start + offset:start + offset + 256]
            assert np.all(window[:-1] <= 10 * window[-1])


@pytest.mark.parametrize('legacy', [False, True])
def test_activation_deletes_previous_pairs_then_stamps(tmp_path, legacy):
    home, checkout, runs, candidate = layout(tmp_path, legacy=legacy)
    (candidate / 'dataset').mkdir()
    (candidate / 'tokenizer' / 'last.pt').write_text('state')
    observed = []
    weekly.activate(home, checkout, candidate, lambda root: observed.append(root))
    assert observed == [candidate.resolve()]
    assert (home / 'finetuned').resolve() == candidate.resolve()
    assert list(runs.iterdir()) == [candidate]
    assert not (candidate / 'dataset').exists()
    assert not (candidate / 'tokenizer' / 'last.pt').exists()
    stamp = (checkout / 'kronos-finetuned-at.txt').read_text().strip()
    assert datetime.fromisoformat(stamp).utcoffset() == timedelta(hours=8)
    assert json.loads((home / 'weekly' / 'activation.json').read_text())['phase'] == 'complete'


@pytest.mark.parametrize('legacy', [False, True])
def test_failed_smoke_restores_old_model_and_timestamp(tmp_path, legacy):
    home, checkout, runs, candidate = layout(tmp_path, legacy=legacy)
    old = (home / 'finetuned').resolve()
    def fail(_):
        raise RuntimeError('smoke failure')
    with pytest.raises(RuntimeError, match='smoke failure'):
        weekly.activate(home, checkout, candidate, fail)
    assert (home / 'finetuned').resolve() == old
    assert (home / 'finetuned' / 'tokenizer' / 'best_model' / 'weights.bin').exists()
    assert (checkout / 'kronos-finetuned-at.txt').read_text() == 'previous\n'


def test_cleanup_failure_recovers_without_retraining(tmp_path, monkeypatch):
    home, checkout, runs, candidate = layout(tmp_path)
    remove = weekly.remove_superseded
    monkeypatch.setattr(weekly, 'remove_superseded', lambda *_: (_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError):
        weekly.activate(home, checkout, candidate, lambda _: None)
    assert (home / 'finetuned').resolve() == candidate.resolve()
    assert (checkout / 'kronos-finetuned-at.txt').read_text() == 'previous\n'
    assert json.loads((candidate / 'run_manifest.json').read_text())['status'] == 'cleanup_failed'
    monkeypatch.setattr(weekly, 'remove_superseded', remove)
    assert weekly.recover(home, checkout)
    assert list(runs.iterdir()) == [candidate]
    assert (checkout / 'kronos-finetuned-at.txt').read_text() != 'previous\n'


def test_timestamp_write_failure_recovers_same_completion_time(tmp_path, monkeypatch):
    home, checkout, runs, candidate = layout(tmp_path)
    write = weekly.atomic_text
    def fail_marker(path, value):
        if path.name == 'kronos-finetuned-at.txt':
            raise PermissionError('marker denied')
        write(path, value)
    monkeypatch.setattr(weekly, 'atomic_text', fail_marker)
    with pytest.raises(PermissionError):
        weekly.activate(home, checkout, candidate, lambda _: None)
    saved = json.loads((candidate / 'run_manifest.json').read_text())
    assert saved['status'] == 'timestamp_write_failed'
    assert (checkout / 'kronos-finetuned-at.txt').read_text() == 'previous\n'
    monkeypatch.setattr(weekly, 'atomic_text', write)
    weekly.recover(home, checkout)
    assert (checkout / 'kronos-finetuned-at.txt').read_text() == saved['completed_at'] + '\n'


def test_prepared_crash_restores_legacy_directory(tmp_path):
    home, checkout, runs, candidate = layout(tmp_path, legacy=True)
    legacy = runs / 'legacy-saved'
    weekly.write_json(home / 'weekly' / 'activation.json', {'phase': 'prepared', 'candidate': candidate.name,
                                                         'previous': legacy.name, 'legacy': True})
    (home / 'finetuned').rename(legacy)
    weekly.replace_pointer(home / 'finetuned', candidate)
    assert not weekly.recover(home, checkout)
    assert not (home / 'finetuned').is_symlink()
    assert (home / 'finetuned' / 'predictor' / 'best_model' / 'weights.bin').exists()


def test_expected_sessions_skip_holidays_and_weekends():
    assert dataset.expected_session(date(2026, 7, 4), 'us') == date(2026, 7, 2)
    assert dataset.expected_session(date(2026, 11, 14), 'ph') == date(2026, 11, 13)


def test_stale_market_times_out_without_mutation():
    result = {'us': {'fresh_fraction': 1}, 'ph': {'fresh_fraction': .5}}
    with pytest.raises(RuntimeError, match='Stale weekly history'):
        weekly.wait_ready(date(2026, 11, 14), probe=lambda _: result)


def test_lock_prevents_duplicate_job(tmp_path):
    with weekly.exclusive_lock(tmp_path / 'job.lock'):
        with pytest.raises(RuntimeError, match='Lock busy'):
            with weekly.exclusive_lock(tmp_path / 'job.lock'):
                pytest.fail('Duplicate lock acquired')


def test_quality_gate_requires_both_markets_and_horizons():
    from core.kronos_weekly_eval import acceptance
    baseline = {f'{m}_{h}d': dict(symbols=30, windows=60, invalid=0, direction_accuracy=.6, mean_absolute_return_error=.01)
                for m in ('us', 'ph') for h in (1, 3)}
    candidate = json.loads(json.dumps(baseline))
    assert acceptance(candidate, baseline) == []
    candidate['ph_1d']['direction_accuracy'] = .5
    assert any('ph_1d: direction regression' == failure for failure in acceptance(candidate, baseline))
    candidate['ph_3d']['symbols'] = 1
    assert any('coverage' in failure for failure in acceptance(candidate, baseline))


def test_dry_run_has_no_db_gpu_or_file_writes(tmp_path, capsys):
    from scripts.kronos_weekly_finetune import main
    assert main(['--run-date', '2026-11-14', '--dry-run', '--kronos-home', str(tmp_path / 'absent')]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['window_start'] is None
    assert value['history_scope'] == 'all_available'
    assert value['window_end_exclusive'] == '2026-11-14'
    assert not (tmp_path / 'absent').exists()


def test_installer_renders_pht_and_does_not_enable_by_default():
    from scripts.install_kronos_weekly import units, UNIT
    rendered = units(Path('/tmp/a path/100%'))
    assert 'OnCalendar=Sat *-*-* 06:00:00 Asia/Manila' in rendered[f'{UNIT}.timer']
    assert 'Persistent=false' in rendered[f'{UNIT}.timer']
    assert '100%%' in rendered[f'{UNIT}.service']
    assert 'ExecStart="/tmp/a path/100%%/.venv-kronos/bin/python"' in rendered[f'{UNIT}.service']


def test_frozen_evaluation_uses_confirmation_and_evaluator_forecasts(tmp_path):
    from core.kronos_weekly_eval import select_anchors, evaluate_model
    corpus(tmp_path / 'dataset')
    found = select_anchors(tmp_path / 'dataset', symbols_per_market=1, lookback=256)
    anchors = [next(row for row in found if row['market'] == market) for market in ('us', 'ph')]
    calls = []
    class Predictor:
        def set_seed(self, seed):
            pass
        def predict(self, **kwargs):
            assert kwargs['x_timestamp'].max() < kwargs['y_timestamp'].min()
            calls.append(kwargs)
            return pd.DataFrame({'close': [float(kwargs['df'].close.iloc[-1])] * 3})
    result = evaluate_model(Predictor(), tmp_path / 'dataset', anchors)
    one_day = [call for call in calls if call['pred_len'] == 1]
    three_day = [call for call in calls if call['pred_len'] == 3]
    assert len(one_day) == 3 * len(anchors) and all(call['sample_count'] == 1 for call in one_day)
    assert len(three_day) == 3 * len(anchors) and all(call['sample_count'] == 1 for call in three_day)
    assert {row['forecast_path'] for row in result['rows']} == {'confirmation', 'evaluator'}
    assert all(row['valid'] for row in result['rows'])
    assert all(metric['direction_accuracy'] == 1 for metric in result['metrics'].values())


def test_failed_forecast_rows_have_no_nan_for_json():
    from core.kronos_weekly_eval import _score_row, summarize
    row = _score_row({'market': 'us', 'symbol': 'AT', 'context_start': 0, 'target_start': 1},
                     1, float('nan'), float('nan'), error='ValueError: no credible sample',
                     band=float('nan'), preparation=None, path='confirmation')
    json.dumps({'rows': [row], 'metrics': summarize([row])}, allow_nan=False)
    assert row['actual_return'] is None and row['forecast_return'] is None and row['valid'] is False
    quiet = _score_row({'market': 'us', 'symbol': 'QMEI', 'context_start': 0, 'target_start': 1},
                       1, 0., float('nan'), error=None, band=.01, preparation=None,
                       path='confirmation', abstained=True)
    metrics = summarize([quiet])
    json.dumps({'rows': [quiet], 'metrics': metrics}, allow_nan=False)
    assert quiet['abstained'] is True and quiet['valid'] is False
    assert metrics['us_1d']['invalid'] == 0 and metrics['us_1d']['abstained'] == 1


def fake_pipeline(tmp_path, monkeypatch):
    from scripts.kronos_weekly_finetune import parser, configuration
    import core.kronos_finetune as training
    home, checkout = tmp_path / 'Kronos', tmp_path / 'checkout'
    home.mkdir()
    checkout.mkdir()
    for label, attribute in (('tokenizer', 'TOKENIZER_PATH'), ('predictor', 'MODEL_PATH')):
        base = tmp_path / f'base-{label}'
        base.mkdir()
        (base / 'config.json').write_text('{}')
        monkeypatch.setattr(training, attribute, base)
    monkeypatch.setattr(weekly, 'source_fingerprint', lambda _: 'source')
    config = configuration(parser().parse_args([]))
    config['min_free_gb'] = .000001
    def builder(out, **options):
        return dataset.build_from_frames([('us', 'AAA', frame()), ('ph', 'BDO.PS', frame())], out, **options)
    def trainer(release, config, resume=False):
        for component in ('tokenizer', 'predictor'):
            root = release / component / 'best_model'
            root.mkdir(parents=True)
            (root / 'weights.bin').write_bytes(b'weights')
            (root.parent / 'training_metrics.json').write_text('{"best_val_loss":1.0}')
    def evaluate(*_):
        return {'base': {'metrics': {}}, 'candidate': {'metrics': {}}, 'failures': []}
    injected = dict(ready_probe=lambda *a, **k: {'us': {'fresh_fraction': 1}, 'ph': {'fresh_fraction': 1}},
                    builder=builder, trainer=trainer, evaluator=evaluate, smoker=lambda *_: None)
    return home, checkout, config, injected


def test_complete_orchestrator_transaction_and_idempotent_repeat(tmp_path, monkeypatch):
    home, checkout, config, injected = fake_pipeline(tmp_path, monkeypatch)
    release = weekly.run_weekly(home, checkout, date(2026, 11, 7), config, **injected)
    assert (home / 'finetuned').resolve() == release
    assert (checkout / 'kronos-finetuned-at.txt').exists()
    assert not (release / 'dataset').exists()
    manifest = json.loads((release / 'run_manifest.json').read_text())
    assert manifest['status'] == 'complete'
    assert manifest['history_scope'] == 'all_available'
    assert manifest['window_start'] == '2021-11-15'
    injected['trainer'] = lambda *_args, **_kw: pytest.fail('Repeated successful week trained again')
    assert weekly.run_weekly(home, checkout, date(2026, 11, 7), config, **injected) == release


def test_all_history_replaces_same_week_five_year_model(tmp_path, monkeypatch):
    home, checkout, config, injected = fake_pipeline(tmp_path, monkeypatch)
    old = weekly.run_weekly(home, checkout, date(2026, 11, 7), config, **injected)
    manifest = json.loads((old / 'run_manifest.json').read_text())
    manifest.pop('history_scope')
    weekly.write_json(old / 'run_manifest.json', manifest)
    release = weekly.run_weekly(home, checkout, date(2026, 11, 7), config, **injected)
    assert release != old
    assert (home / 'finetuned').resolve() == release
    assert not old.exists()


def test_rehearsal_does_not_activate_delete_or_stamp(tmp_path, monkeypatch):
    home, checkout, config, injected = fake_pipeline(tmp_path, monkeypatch)
    checkpoint(home / 'finetuned')
    release = weekly.run_weekly(home, checkout, date(2026, 11, 7), config, rehearsal=True, **injected)
    assert not (home / 'finetuned').is_symlink()
    assert not (checkout / 'kronos-finetuned-at.txt').exists()
    assert (release / 'dataset').exists()
    assert json.loads((release / 'run_manifest.json').read_text())['status'] == 'rehearsed'


def test_rejected_run_preserves_model_and_keeps_reviewable_candidate(tmp_path, monkeypatch):
    home, checkout, config, injected = fake_pipeline(tmp_path, monkeypatch)
    checkpoint(home / 'finetuned')
    injected['evaluator'] = lambda *_: {'base': {'metrics': {}}, 'candidate': {'metrics': {}}, 'failures': ['PH regression']}
    with pytest.raises(ValueError, match='PH regression'):
        weekly.run_weekly(home, checkout, date(2026, 11, 7), config, **injected)
    assert not (home / 'finetuned').is_symlink()
    candidate = next((home / 'weekly' / 'runs').iterdir())
    assert (candidate / 'predictor' / 'best_model' / 'weights.bin').exists()
    saved = json.loads((candidate / 'run_manifest.json').read_text())
    assert saved['status'] == 'rejected' and saved['failures'] == ['PH regression']
    assert (candidate / 'evaluation.json').exists()
    assert not (checkout / 'kronos-finetuned-at.txt').exists()
    assert len(list((home / 'weekly' / 'reports').glob('*.json'))) == 1


def test_resume_rejects_changed_configuration(tmp_path, monkeypatch):
    home, checkout, config, injected = fake_pipeline(tmp_path, monkeypatch)
    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt()
    injected['trainer'] = interrupted
    with pytest.raises(KeyboardInterrupt):
        weekly.run_weekly(home, checkout, date(2026, 11, 7), config, **injected)
    release = next((home / 'weekly' / 'runs').iterdir())
    changed = dict(config, seed=123)
    with pytest.raises(ValueError, match='identity mismatch'):
        weekly.run_weekly(home, checkout, date(2026, 11, 7), changed, resume_id=release.name, **injected)


def test_cleanup_includes_only_inventoried_legacy_versions(tmp_path):
    home, checkout, runs, candidate = layout(tmp_path)
    for name in weekly.LEGACY_RELEASE_NAMES:
        checkpoint(home / name)
    (home / 'weights').mkdir()
    (home / 'weights' / 'original').write_text('keep')
    weekly.activate(home, checkout, candidate, lambda _: None)
    assert not list(home.glob('finetuned_*'))
    assert (home / 'weights' / 'original').read_text() == 'keep'


def test_identity_changes_invalidate_forecast_cache(monkeypatch):
    from core.kronos_confirm import KronosConfirmGate
    identity = ['one']
    monkeypatch.setattr('core.kronos_provenance.active_model_identity', lambda: identity[0])
    gate = KronosConfirmGate(mode='shadow', fetch_fn=lambda _: {})
    gate._cache['cached'] = (9999999, object())
    gate.flush_with_exits()
    assert not gate._cache and gate._model_identity == 'one'
    gate._cache['cached'] = (9999999, object())
    identity[0] = 'two'
    gate.flush_with_exits()
    assert not gate._cache and gate._model_identity == 'two'


@pytest.mark.parametrize('start', [None, date(2021, 11, 14)])
def test_readonly_snapshot_sql_preserves_requested_history_and_session_dates(tmp_path, monkeypatch, start):
    import psycopg
    commands = []
    source = frame(start='2010-01-04')
    rows = [(market, symbol, stamp.date(), 0, *record) for market, symbol in [('us', 'AAA'), ('ph', 'BDO.PS')]
            for stamp, record in zip(source.index, source.to_numpy())]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, sql, params): commands.append((sql, params))
        def __iter__(self): return iter(rows)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, sql): commands.append((sql, None))
        def cursor(self, **kwargs):
            assert kwargs['name'] == 'weekly_kronos_snapshot'
            return Cursor()
    monkeypatch.setattr(psycopg, 'connect', lambda *_: Connection())
    meta = dataset.build_snapshot(tmp_path / 'data', start=start, end=date(2026, 11, 14))
    assert commands[0][0] == 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'
    if start is None:
        assert 'b.bar_date >=' not in commands[1][0]
        assert 'b.bar_date < %s' in commands[1][0]
        assert commands[1][1] == (date(2026, 11, 14),)
        assert meta['window_start'] == '2010-01-04'
    else:
        assert 'b.bar_date >= %s AND b.bar_date < %s' in commands[1][0]
        assert commands[1][1] == (start, date(2026, 11, 14))
        assert meta['window_start'] == start.isoformat()
    assert meta['report']['markets']['ph']['bars'] > 0
    # ts=0 on every source row proves dates came from bar_date, not UTC timestamps.
    assert meta['report']['markets']['us']['bars'] > 1000


def test_live_provenance_cannot_make_historical_replay_eligible(tmp_path, monkeypatch):
    from core.kronos_provenance import decision_provenance, checkpoint_provenance
    from datetime import timezone
    release = tmp_path / 'release'
    checkpoint(release)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr('core.market.last_closed_session_date', lambda *_: date(2026, 11, 6))
    # A date inside the selection window still forecasts, without a holdout claim.
    early = checkpoint_provenance(release, '2026-11-13', required=True)
    assert early['identity_verified'] and not early['out_of_sample_eligible']
    with pytest.raises(ValueError, match='latest completed'):
        decision_provenance(release, '2026-11-05', market='us', decision_at=now.isoformat(), required=True)


def test_audit_retention_is_bounded_for_failures(tmp_path):
    for number in range(55):
        weekly.write_report(tmp_path, {'run_id': f'failed-{number:03}', 'status': 'data_not_ready'})
    reports = list((tmp_path / 'reports').glob('*.json'))
    assert len(reports) == 52
    assert (tmp_path / 'reports' / 'failed-054.json').exists()


def test_current_live_decision_can_use_prior_session_without_claiming_holdout(tmp_path, monkeypatch):
    from core.kronos_provenance import decision_provenance, checkpoint_provenance
    from datetime import timezone
    release = tmp_path / 'release'
    checkpoint(release)
    weekly.create_selection_manifest(release, 'corpus', date(2026, 11, 7))
    monkeypatch.setattr('core.market.last_closed_session_date', lambda *_: date(2026, 11, 6))
    early = checkpoint_provenance(release, '2026-11-06', required=True)
    assert early['identity_verified'] and not early['out_of_sample_eligible']
    value = decision_provenance(release, '2026-11-06', market='us',
                                decision_at=datetime.now(timezone.utc).isoformat(), required=True)
    assert value['identity_verified']
    assert value['forecast_mode'] == 'live'
    assert not value['out_of_sample_eligible']


def test_relative_invalid_policy_keeps_zero_as_default_and_requires_nonregression():
    from core.kronos_weekly_eval import acceptance
    base = {f'{m}_{h}d': dict(symbols=30, windows=1000, invalid=5, direction_accuracy=.6, mean_absolute_return_error=.01)
            for m in ('us','ph') for h in (1,3)}
    candidate=json.loads(json.dumps(base))
    for metric in candidate.values(): metric['invalid']=2
    assert acceptance(candidate,base)
    assert not acceptance(candidate,base,invalid_policy='relative',max_invalid_rate=.01)
    candidate['us_1d']['invalid']=6
    assert any('invalid rate' in e for e in acceptance(candidate,base,invalid_policy='relative'))
    candidate['us_1d']['invalid']=11
    base['us_1d']['invalid']=20
    assert any('invalid rate' in e for e in acceptance(candidate,base,invalid_policy='relative'))


def test_saved_candidate_can_be_reviewed_and_reevaluated_without_retraining(tmp_path, monkeypatch):
    home, checkout, config, injected = fake_pipeline(tmp_path, monkeypatch)
    checkpoint(home / 'finetuned')
    injected['evaluator'] = lambda *_: {'base': {'metrics': {}}, 'candidate': {'metrics': {}}, 'failures': ['invalid base forecasts']}
    with pytest.raises(ValueError):
        weekly.run_weekly(home, checkout, date(2026,11,7), config, **injected)
    release = next((home / 'weekly' / 'runs').iterdir())
    calls = []
    def passed(dataset, candidate, settings):
        calls.append(settings)
        return {'base': {'metrics': {}}, 'candidate': {'metrics': {}}, 'failures': []}
    weekly.evaluate_saved(home, checkout, release.name, invalid_policy='relative', max_invalid_rate=.01,
                          evaluator=passed, smoker=lambda *_: None)
    assert calls[0]['invalid_policy'] == 'relative'
    assert (home / 'finetuned').resolve() == release
    saved = json.loads((release / 'run_manifest.json').read_text())
    assert saved['evaluation_history'][0]['invalid_policy'] == 'zero'
    assert saved['status'] == 'complete'
    assert (checkout / 'kronos-finetuned-at.txt').exists()


def test_evaluation_exception_preserves_trained_pair_and_failure_report(tmp_path, monkeypatch):
    home, checkout, config, injected = fake_pipeline(tmp_path, monkeypatch)
    def fail(*_):
        raise RuntimeError('provider evaluation error')
    injected['evaluator'] = fail
    with pytest.raises(RuntimeError):
        weekly.run_weekly(home, checkout, date(2026,11,7), config, **injected)
    release = next((home / 'weekly' / 'runs').iterdir())
    assert (release / 'predictor' / 'best_model' / 'weights.bin').exists()
    saved = json.loads((release / 'run_manifest.json').read_text())
    assert saved['status'] == 'evaluation_failed'
    assert saved['training_metrics']['predictor']['best_val_loss'] == 1
    assert not (checkout / 'kronos-finetuned-at.txt').exists()


def test_smoke_contexts_use_evaluated_usable_pairs_not_empty_lowest_liquidity_symbol(tmp_path):
    def row(market,symbol,horizon,valid,target):
        return dict(market=market,symbol=symbol,horizon=horizon,valid=valid,
                    target_start=target,context_start=target-256)
    rows=[row('us','BAD',h,False,500) for h in (1,3)]
    rows += [row(m,s,h,True,t) for m,s,t in [('us','AAPL',600),('ph','BDO.PS',900)] for h in (1,3)]
    (tmp_path/'evaluation.json').write_text(json.dumps({'candidate':{'rows':rows}}))
    sample=weekly.smoke_anchors(tmp_path,tmp_path/'unused',{'lookback':256})
    assert [r['symbol'] for r in sample] == ['AAPL','BDO.PS']
    assert all(r['horizon']==3 for r in sample)
    rows=[r for r in rows if r['market']!='ph']
    (tmp_path/'evaluation.json').write_text(json.dumps({'candidate':{'rows':rows}}))
    with pytest.raises(ValueError,match='usable evaluated ph'):
        weekly.smoke_anchors(tmp_path,tmp_path/'unused',{'lookback':256})


def test_zero_invalid_gate_requires_clean_candidate_and_usable_base():
    from core.kronos_weekly_eval import acceptance
    base={f'{m}_{h}d':dict(symbols=30,windows=100,invalid=2,valid_windows=98,
                          direction_accuracy=.6,mean_absolute_return_error=.01)
          for m in ('us','ph') for h in (1,3)}
    candidate=json.loads(json.dumps(base))
    for metrics in candidate.values():
        metrics.update(invalid=0,valid_windows=100)
    assert not acceptance(candidate,base)
    candidate['us_1d']['invalid']=1
    assert 'us_1d: invalid candidate forecasts' in acceptance(candidate,base)
    candidate['us_1d']['invalid']=0
    base['us_1d'].update(valid_windows=0,invalid=100)
    assert any('usable base' in reason for reason in acceptance(candidate,base))


def test_skill_comparison_uses_same_valid_windows_without_hiding_invalids():
    from core.kronos_weekly_eval import summarize,pair_comparison
    def row(target,valid,forecast):
        return dict(market='us',symbol='AAA',target_start=target,horizon=1,
                    valid=valid,actual_return=.1,forecast_return=forecast,sideways_band_return=.01)
    base={'rows':[row(1,True,.1),row(2,False,None),row(3,True,-.1)]}
    candidate={'rows':[row(1,True,.08),row(2,True,.5),row(3,False,None)]}
    for result in (base,candidate): result['metrics']=summarize(result['rows'])
    pair_comparison(base,candidate)
    for result in (base,candidate):
        metric=result['metrics']['us_1d']
        assert metric['invalid']==1 and metric['windows']==3
        assert metric['comparison_windows']==1 and metric['comparison_symbols']==1
        assert metric['direction_accuracy']==1
    assert base['metrics']['us_1d']['mean_absolute_return_error']==0
    assert candidate['metrics']['us_1d']['mean_absolute_return_error']==pytest.approx(.02)


def test_vram_budget_matches_campaign_floor():
    assert weekly.required_vram_bytes(16) == 10_000 * 1024 * 1024
    assert weekly.required_vram_bytes(32) > weekly.required_vram_bytes(16)
    with pytest.raises(RuntimeError, match='needs 10000 MiB'):
        weekly.ensure_vram(9_000 * 1024 * 1024, 16)


def test_freshness_excludes_names_that_stopped_before_the_prior_session():
    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def execute(self, sql, params):
            calls.append((sql, params))
        def fetchall(self):
            return [('LIVE', True, True), ('HALTED', True, False), ('DELISTED', False, False)]
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def execute(self, sql): calls.append((sql, None))
        def cursor(self): return Cursor()
    result = weekly.readiness(date(2026, 11, 14), connection_factory=lambda: Connection(),
                              no_trade_loader=lambda _session: set())
    us = result['us']
    assert us['reference_symbols'] == ['LIVE', 'HALTED']
    assert us['fresh_count'] == 1 and us['fresh_fraction'] == .5
    assert us['excluded_inactive_symbols'] == ['DELISTED']
    assert us['excluded_no_trade_count'] == 0 and us['no_trade_report'] == 'not_applicable'
    expected = dataset.expected_session(date(2026, 11, 14), 'us')
    prior = dataset.expected_session(expected, 'us')
    assert calls[1][1] == ('us', expected - timedelta(days=14), expected, prior, expected)


def test_philippine_no_trade_names_do_not_reduce_freshness():
    rows = [('LIVE', True, True), ('QUIET', True, False), ('MISSED', True, False), ('OLD', False, False)]
    counts = weekly.freshness_counts(rows, {'QUIET'})
    assert counts['reference_symbols'] == ['LIVE', 'MISSED']
    assert counts['fresh_count'] == 1 and counts['fresh_fraction'] == .5
    assert counts['excluded_no_trade_symbols'] == ['QUIET']
    assert counts['excluded_inactive_symbols'] == ['OLD']


def test_long_gaps_and_training_only_symbols_are_reported(tmp_path):
    full = frame()
    short_index = pd.bdate_range('2021-11-14', periods=520)
    short = pd.DataFrame({'open': 10., 'high': 11., 'low': 9., 'close': 10., 'volume': 1000.}, index=short_index)
    gapped_index = list(pd.bdate_range('2021-11-14', periods=300)) + list(pd.bdate_range('2024-01-02', periods=700))
    gapped = pd.DataFrame({'open': 20., 'high': 21., 'low': 19., 'close': 20., 'volume': 1000.}, index=gapped_index)
    meta = dataset.build_from_frames(
        [('us', 'FULL', full), ('us', 'SHORT', short), ('us', 'GAP', gapped), ('ph', 'BDO.PS', full)],
        tmp_path, start=date(2021, 11, 14), end=date(2026, 11, 14))
    us = meta['report']['markets']['us']
    assert us['training_only_symbols'] >= 1
    assert us['validation_symbols'] >= 1 and us['evaluation_symbols'] >= 1
    assert us['long_gaps'] >= 1 and meta['report']['long_gaps'] >= 1


def test_interrupted_evaluation_can_resume_or_be_reevaluated(tmp_path, monkeypatch):
    home, checkout, config, injected = fake_pipeline(tmp_path, monkeypatch)
    def interrupt(*_):
        raise KeyboardInterrupt()
    injected['evaluator'] = interrupt
    with pytest.raises(KeyboardInterrupt):
        weekly.run_weekly(home, checkout, date(2026, 11, 7), config, **injected)
    release = next((home / 'weekly' / 'runs').iterdir())
    assert json.loads((release / 'run_manifest.json').read_text())['status'] == 'evaluation_interrupted'
    calls = []
    injected['trainer'] = lambda *_args, **_kwargs: calls.append('train')
    injected['evaluator'] = lambda *_: {'base': {'metrics': {}}, 'candidate': {'metrics': {}}, 'failures': []}
    weekly.run_weekly(home, checkout, date(2026, 11, 7), config, resume_id=release.name, **injected)
    assert calls == []
    second = tmp_path / 'again'
    second.mkdir()
    home2, checkout2, config2, injected2 = fake_pipeline(second, monkeypatch)
    injected2['evaluator'] = interrupt
    with pytest.raises(KeyboardInterrupt):
        weekly.run_weekly(home2, checkout2, date(2026, 11, 7), config2, **injected2)
    saved = next((home2 / 'weekly' / 'runs').iterdir())
    manifest = json.loads((saved / 'run_manifest.json').read_text())
    manifest['status'] = 'evaluating'
    (saved / 'run_manifest.json').write_text(json.dumps(manifest))
    weekly.evaluate_saved(home2, checkout2, saved.name, evaluator=injected['evaluator'], smoker=lambda *_: None)
    assert (home2 / 'finetuned').resolve() == saved


def test_low_vram_stops_before_training(tmp_path, monkeypatch):
    home, checkout, config, injected = fake_pipeline(tmp_path, monkeypatch)
    def fail(_config):
        raise RuntimeError('Only 1000 MiB VRAM free; batch 16 needs 10000 MiB')
    with pytest.raises(RuntimeError, match='VRAM'):
        weekly.run_weekly(home, checkout, date(2026, 11, 7), config, gpu_check=fail, **injected)
    release = next((home / 'weekly' / 'runs').iterdir())
    assert json.loads((release / 'run_manifest.json').read_text())['status'] == 'training'
    assert not (checkout / 'kronos-finetuned-at.txt').exists()
    assert not (home / 'finetuned').exists()
