"""Arrow names saved models without changing weights and predicts completed bars."""
from __future__ import annotations

from datetime import date
import json

import pytest

from core.arrow import Arrow, register_arrow
from core.kronos_direction import run_direction_experiment
from core.test_kronos_direction import corpus, frame, small_config
from learn.kronos_weekly_dataset import file_hash


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    data = corpus(tmp_path / 'source', end=date(2026, 10, 3))
    output = tmp_path / 'models'
    run_direction_experiment(data, output, date(2026, 10, 3), small_config(folds=1, epochs=1))
    alias = tmp_path / 'arrow'
    monkeypatch.setattr('core.arrow.last_closed_session_date', lambda _market: date(2026, 11, 12))
    monkeypatch.setattr('core.market.last_closed_session_date', lambda _market: date(2026, 11, 12))
    return output, alias


def test_naming_is_idempotent_and_preserves_locked_models(bundle):
    output, alias = bundle
    paths = ['selection.json', 'selection.sha256', 'direction-1d.npz', 'direction-3d.npz']
    before = {name: file_hash(output / name) for name in paths}
    card = register_arrow(output, alias)
    assert card['name'] == 'Arrow'
    assert card['models']['1']['name'] == 'Arrow 1D'
    assert card == register_arrow(output, alias)
    assert alias.is_symlink() and alias.resolve() == output
    assert {name: file_hash(output / name) for name in paths} == before
    arrow = Arrow.load(alias)
    assert arrow.info()['name'] == 'Arrow'
    assert arrow.info()['validation_status'] == 'final_test_pending'
    assert not arrow.info()['automated_trading_enabled']


def test_alias_never_replaces_a_directory(bundle, tmp_path):
    output, _ = bundle
    occupied = tmp_path / 'occupied';occupied.mkdir()
    with pytest.raises(ValueError, match='cannot replace'):
        register_arrow(output, occupied)
    assert occupied.is_dir()
    assert not (output / 'arrow.json').exists()


def test_arrow_prediction_cuts_future_bars_and_respects_quality(bundle):
    output, alias = bundle;register_arrow(output, alias)
    arrow = Arrow.load(alias)
    history = frame()
    prediction = arrow.predict(history, 'aapl', 'us', 1)
    assert prediction['model'] == 'Arrow' and prediction['symbol'] == 'AAPL'
    assert prediction['as_of'] == '2026-11-12'
    assert prediction['direction'] in ('up', 'down', 'flat')
    assert sum(prediction['probabilities'].values()) == pytest.approx(1)
    future_changed = history.copy()
    future_changed.loc['2026-11-13', ['open','high','low','close']] *= 10
    assert arrow.predict(future_changed, 'aapl', 'us', 1) == prediction
    bad = history.copy();bad.loc[:'2026-11-12', 'volume'] = 0
    assert arrow.predict(bad, 'AAPL', 'us')['abstained']
    assert arrow.predict(history, 'BDO', 'ph', 3)['symbol'] == 'BDO.PS'


@pytest.mark.parametrize('options,match', [
    ({'symbol':'GBDMF'}, 'excluded'),
    ({'horizon':2}, 'horizon'),
    ({'as_of':date(2026,11,13)}, 'latest completed'),
    ({'as_of':date(2026,9,1)}, 'training/selection'),
])
def test_arrow_rejects_invalid_or_in_sample_requests(bundle, options, match):
    output, alias = bundle;register_arrow(output, alias)
    args = dict(symbol='AAPL',market='us',horizon=1);args.update(options)
    with pytest.raises(ValueError, match=match):
        Arrow.load(alias).predict(frame(), **args)


def test_arrow_rejects_stale_history(bundle):
    output, alias = bundle;register_arrow(output, alias)
    with pytest.raises(ValueError, match='stale'):
        Arrow.load(alias).predict(frame().loc[:'2026-11-11'], 'AAPL', 'us')


@pytest.mark.parametrize('name', ['selection.json', 'direction-1d.npz', 'arrow.json'])
def test_arrow_rejects_tampered_identity(bundle, name):
    output, alias = bundle;register_arrow(output, alias)
    if name == 'arrow.json':
        card=json.loads((output/name).read_text());card['name']='Another model'
        (output/name).write_text(json.dumps(card))
    else:
        with (output/name).open('ab') as stream:
            stream.write(b'changed')
    with pytest.raises(ValueError, match='differs'):
        Arrow.load(alias)


def test_arrow_cli_csv_and_info(bundle, tmp_path, capsys):
    from scripts.arrow import main
    output, alias = bundle;register_arrow(output, alias)
    csv = tmp_path / 'daily.csv'
    frame().to_csv(csv, index_label='date')
    capsys.readouterr()
    assert main(['info','--model-dir',str(alias)]) == 0
    assert json.loads(capsys.readouterr().out)['name'] == 'Arrow'
    assert main(['predict','--model-dir',str(alias),'--symbol','AAPL','--market','us',
                 '--horizon','3','--csv',str(csv)]) == 0
    prediction=json.loads(capsys.readouterr().out)
    assert prediction['horizon_sessions'] == 3 and prediction['as_of'] == '2026-11-12'
