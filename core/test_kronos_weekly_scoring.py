"""Weekly accuracy uses the live up/down/flat classes and unrounded labels."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from core.kronos_weekly_eval import DIRECTION_SCORING, evaluate_model, select_anchors, summarize
from learn.kronos_weekly_dataset import build_from_frames


@pytest.mark.parametrize('actual,forecast,actual_class,forecast_class', [
    (0., .0005, 'flat', 'flat'),
    (-.0005, .0005, 'flat', 'flat'),
    (4.75e-8, 0., 'flat', 'flat'),
    (.0025, .003, 'up', 'up'),
    (-.0025, -.003, 'down', 'down'),
    (.003, .0005, 'up', 'flat'),
    (-.003, .003, 'down', 'up'),
])
def test_direction_accuracy_uses_the_same_band_for_both_sides(actual, forecast, actual_class, forecast_class):
    row = dict(market='us', symbol='AAA', horizon=1, valid=True,
               actual_return=actual, forecast_return=forecast, sideways_band_return=.0025)
    metric = summarize([row])['us_1d']
    assert metric['direction_accuracy'] == float(actual_class == forecast_class)
    assert metric['direction_scoring'] == DIRECTION_SCORING
    assert metric['direction_confusion'][actual_class][forecast_class] == 1
    assert sum(sum(values.values()) for values in metric['direction_confusion'].values()) == 1
    assert metric['sideways_fraction'] == float(forecast_class == 'flat')


@pytest.mark.parametrize('band', [None, 0., -1., float('nan'), float('inf')])
def test_valid_forecasts_cannot_silently_fall_back_to_sign_scoring(band):
    row = dict(market='us', symbol='AAA', horizon=1, valid=True,
               actual_return=0., forecast_return=0., sideways_band_return=band)
    with pytest.raises(ValueError, match='positive sideways band'):
        summarize([row])


@pytest.mark.parametrize('price', [.001, .00015])
def test_one_day_labels_do_not_use_rounded_worker_anchor(tmp_path, monkeypatch, price):
    frame = pd.DataFrame(dict(open=price, high=price * 1.01, low=price * .99,
                              close=price, volume=1000.),
                         index=pd.bdate_range('2021-11-15', '2026-11-13'))
    build_from_frames([('us', 'AAA', frame), ('ph', 'BDO.PS', frame)], tmp_path,
                      end=date(2026, 11, 14))
    anchors = select_anchors(tmp_path, symbols_per_market=1, lookback=256)
    anchors = [next(row for row in anchors if row['market'] == market) for market in ('us', 'ph')]
    def confirmed(_predictor, _symbol, history, *_args):
        return dict(returns=[0., 0., 0.], anchor_close=round(float(history.close.iloc[-1]), 4), rejected=0)
    monkeypatch.setattr('scripts.kronos_confirm_worker.forecast_returns', confirmed)
    class Predictor:
        def set_seed(self, _seed):
            pass
        def predict(self, **kwargs):
            return pd.DataFrame({'close': [float(kwargs['df'].close.iloc[-1])] * kwargs['pred_len']})
    result = evaluate_model(Predictor(), tmp_path, anchors)
    assert all(row['actual_return'] == 0. for row in result['rows'])
    assert all(metric['direction_accuracy'] == 1. for metric in result['metrics'].values())
    assert all(metric['direction_confusion']['flat']['flat'] == 1 for metric in result['metrics'].values())
