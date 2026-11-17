"""Confirmation worker contracts without torch, trained weights, or a database."""

from __future__ import annotations

import json
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
    """The invariant is the vote, not the transport: a bad close never votes.

    It used to abort the symbol with a ValueError, which surfaced the raw
    exception in the Paper signal log on every scan of that anchor date.
    """
    predictor = SimpleNamespace(set_seed=lambda _: None,
                                predict=lambda **kw: pd.DataFrame({"close": [price]}))
    row = worker.forecast_returns(predictor, "TEST", frame(), 3)
    assert row["returns"] == [] and row["predicted"] == [] and row["rejected"] == 3


def test_one_unusable_simulation_does_not_discard_the_others():
    """A degenerate draw must not take the whole symbol's vote set with it."""
    predictor = SimpleNamespace(
        set_seed=lambda _: None,
        predict=Mock(side_effect=[pd.DataFrame({"close": [v]}) for v in (101., 0., 103.)]),
    )
    row = worker.forecast_returns(predictor, "TEST", frame(), 3)
    assert predictor.predict.call_count == 3
    assert row["returns"] == [pytest.approx(0.01), pytest.approx(0.03)]
    assert row["predicted"] == [101.0, 103.0]
    assert row["rejected"] == 1


@pytest.mark.parametrize("price", [-120., 160.])
def test_implausible_simulations_are_rejected_like_invalid_ones(price):
    predictor = SimpleNamespace(set_seed=lambda _: None,
                                predict=lambda **kw: pd.DataFrame({"close": [price]}))
    row = worker.forecast_returns(predictor, "TEST", frame(), 3)
    assert row["returns"] == [] and row["rejected"] == 3


def test_credible_close_boundary_matches_the_gate_budget():
    from core.kronos_confirm import MAX_CREDIBLE_RETURN

    assert worker._credible_close(100.0 * (1 + MAX_CREDIBLE_RETURN), 100.0) is True
    assert worker._credible_close(100.0 * (1 + MAX_CREDIBLE_RETURN) + 0.01, 100.0) is False
    assert worker._credible_close(100.0 * (1 - MAX_CREDIBLE_RETURN) - 0.01, 100.0) is False
    assert worker._credible_close(0.0, 100.0) is False


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
    assert params == ["TEST.PS", "ph", "2026-10-30"]
    assert result.index[-1] == pd.Timestamp("2026-10-30")
    conn.close.assert_called_once()


def test_short_series_is_the_symbol_defect_class_not_a_worker_fault():
    """49 bars is the symbol's own problem, and it must travel as such.

    The message reaches the gate as ``Kronos worker error for SOLS: ...`` either
    way; only the class lets a replay record the miss instead of retrying it
    until the bounded retry stops the run.
    """
    predictor = SimpleNamespace(predict=Mock())
    with pytest.raises(worker.InsufficientHistory, match="only 49 bars; need 60"):
        worker.forecast_returns(predictor, "SOLS", frame(49), 3)
    predictor.predict.assert_not_called()
    assert worker.error_kind(worker.InsufficientHistory("only 49 bars; need 60")) == (
        worker.SYMBOL_HISTORY_ERROR_KIND)
    assert worker.error_kind(RuntimeError("CUDA out of memory")) == "worker_error"


def test_context_refusal_is_not_swallowed_into_the_generic_invalid_context_branch():
    """A too-recent price break is a short series too, not a broken worker."""
    data = frame(80)
    data.iloc[-30:, data.columns.get_indexer(['open', 'high', 'low', 'close'])] *= .000001
    with pytest.raises(worker.InsufficientHistory, match='post-price-break'):
        worker._prep_1d_frame(data, 400)


def test_short_series_symbol_is_tagged_in_the_worker_payload(monkeypatch, tmp_path):
    """The payload tag is what the gate reads; a bare message would be retried."""
    monkeypatch.setattr(worker, "load_confirmation_predictor",
                        lambda *a, **k: (SimpleNamespace(), "cpu"))
    monkeypatch.setattr(worker, "load_symbol_df", lambda *a, **k: frame(49))
    out = tmp_path / "payload.json"
    assert worker.main(["--symbols", "SOLS", "--model", "base", "--out", str(out)]) == 0
    row = json.loads(out.read_text())["results"][0]
    assert row["symbol"] == "SOLS" and row["returns"] == []
    assert row["error_kind"] == worker.SYMBOL_HISTORY_ERROR_KIND
    assert "only 49 bars; need 60" in row["error"]


def test_worker_passes_only_post_collapse_prices_and_reports_context():
    data=frame(256)
    data.iloc[-70:,data.columns.get_indexer(['open','high','low','close'])] *= .000001
    predictor=SimpleNamespace(set_seed=lambda _:None,
        predict=Mock(return_value=pd.DataFrame({'close':[.000101]})))
    row=worker.forecast_returns(predictor,'COLLAPSE',data,1,256)
    assert len(predictor.predict.call_args.kwargs['df'])==70
    assert row['model_context']['trimmed_bars']==186
    assert row['model_context']['policy']=='post-price-collapse-v2'
    assert row['returns']==[.01]


def test_sub_penny_anchor_close_is_not_rounded_to_zero():
    data = frame()
    data[['open', 'high', 'low', 'close']] *= .0000001
    predictor = SimpleNamespace(set_seed=lambda _: None,
                                predict=Mock(return_value=pd.DataFrame({'close': [.0000105]})))
    row = worker.forecast_returns(predictor, 'PEMTF', data, 1)
    assert row['anchor_close'] > 0
    assert row['anchor_close'] == pytest.approx(.00001)
    assert row['returns'] == [pytest.approx(.05)]


def test_recent_collapse_abstention_keeps_specific_reason_and_skips_predictor():
    data=frame(256)
    data.iloc[-30:,data.columns.get_indexer(['open','high','low','close'])] *= .000001
    predictor=SimpleNamespace(predict=Mock())
    with pytest.raises(ValueError,match='Insufficient post-price-break context: 30 bars'):
        worker.forecast_returns(predictor,'COLLAPSE',data,1,256)
    predictor.predict.assert_not_called()
