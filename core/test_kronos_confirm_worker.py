"""Confirmation worker contracts without torch, trained weights, or a database."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import numpy as np
import pandas as pd
import pytest

from scripts import kronos_confirm_worker as worker


def frame(n=80):
    return pd.DataFrame(dict(open=[100.] * n, high=[101.] * n,
                             low=[99.] * n, close=[100.] * n, volume=[1000.] * n),
                        index=pd.bdate_range(end="2026-10-30", periods=n))


@pytest.mark.parametrize("column,value", [("open", 0), ("close", float("nan")),
                                           ("high", float("inf")), ("low", 101.), ("volume", -1)])
def test_invalid_context_is_rejected_without_silently_moving_the_anchor(column, value):
    data = frame()
    data.iloc[-1, data.columns.get_loc(column)] = value
    assert worker._prep_1d_frame(data, 400) is None


def test_an_old_bad_bar_outside_model_context_does_not_poison_current_input():
    data = frame(600)
    data.iloc[0, data.columns.get_loc("close")] = float("nan")
    assert worker._prep_1d_frame(data, 400) is not None
    predictor = SimpleNamespace(set_seed=lambda _: None,
                                predict=lambda **kw: pd.DataFrame({"close": [102.]}))
    assert worker.forecast_returns(predictor, "TEST", data, 3)["atr_pct"] == pytest.approx(2)


def test_invalid_ranges_are_reported_as_data_errors_without_model_inference():
    data = frame()
    data.iloc[-3, data.columns.get_loc('low')] = 101
    predictor = SimpleNamespace(predict=Mock())
    with pytest.raises(ValueError, match='Invalid OHLC context'):
        worker.forecast_returns(predictor, 'TEST', data, 3)
    predictor.predict.assert_not_called()


def test_one_day_samples_have_distinct_reproducible_seeds_and_daily_atr():
    seeds = []
    predictor = SimpleNamespace(set_seed=seeds.append)
    predictor.predict = Mock(side_effect=[pd.DataFrame({"close": [v]}) for v in (102, 98, 103)])
    row = worker.forecast_returns(predictor, "TEST", frame(), 3)
    assert row["anchor_date"] == "2026-10-30" and row["anchor_close"] == 100
    assert row["returns"] == [.02, -.02, .03]
    assert row["atr_pct"] == pytest.approx(2)
    assert len(set(seeds)) == 3
    assert seeds == [worker._seed("TEST", "2026-10-30", i) for i in range(3)]
    for call in predictor.predict.call_args_list:
        assert call.kwargs["pred_len"] == call.kwargs["sample_count"] == 1
        assert len(call.kwargs["y_timestamp"]) == 1
        assert call.kwargs["x_timestamp"].max() <= pd.Timestamp("2026-10-30")


@pytest.mark.parametrize("price", [0, np.nan, np.inf])
def test_bad_predicted_prices_never_become_directional_votes(price):
    predictor = SimpleNamespace(set_seed=lambda _: None,
                                predict=lambda **kw: pd.DataFrame({"close": [price]}))
    with pytest.raises(ValueError, match="predicted close"):
        worker.forecast_returns(predictor, "TEST", frame(), 3)


def test_selected_finetuned_model_cannot_silently_fall_back_to_base(monkeypatch, tmp_path):
    monkeypatch.setattr("core.kronos_finetune.PREDICTOR_OUT", tmp_path / "missing-predictor")
    monkeypatch.setattr("core.kronos_finetune.TOKENIZER_OUT", tmp_path / "missing-tokenizer")
    load = Mock()
    monkeypatch.setattr(worker, "load_predictor", load)
    with pytest.raises(ValueError, match="fine-tuned.*missing"):
        worker.load_confirmation_predictor("finetuned")
    load.assert_not_called()


def test_explicit_base_model_uses_inference_mode(monkeypatch):
    predictor = SimpleNamespace(model=Mock(), tokenizer=Mock())
    load = Mock(return_value=(predictor, "cpu"))
    monkeypatch.setattr(worker, "load_predictor", load)
    assert worker.load_confirmation_predictor("base") == (predictor, "cpu")
    load.assert_called_once_with(use_finetuned=False)
    predictor.model.eval.assert_called_once()
    predictor.tokenizer.eval.assert_called_once()


def test_database_query_scopes_market_and_historical_cutoff(monkeypatch):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [(pd.Timestamp("2026-10-30").date(), 100, 101, 99, 100, 1000)]
    monkeypatch.setattr("data.db.get_conn", lambda: conn)
    result = worker.load_symbol_df("TEST", "2026-10-30", "ph")
    sql, params = cur.execute.call_args.args
    assert "s.market = %s" in sql and "b.bar_date <= %s" in sql
    assert params == ["TEST", "ph", "2026-10-30"]
    assert result.index[-1] == pd.Timestamp("2026-10-30")
    conn.close.assert_called_once()
