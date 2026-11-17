"""Price-break context selection stays causal and preserves normal stocks."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from core.kronos_input import InsufficientHistory, prepare_context


def frame(prices):
    close=np.asarray(prices,dtype=float)
    return pd.DataFrame({'open':close,'high':close*1.01,'low':close*.99,'close':close,'volume':1000.},
                        index=pd.bdate_range('2025-01-01',periods=len(close)))


def test_discontinuity_uses_post_break_data_without_changing_prices():
    raw=frame([4.9]*180+[.00595]*10+[.0001]*70)
    prepared=prepare_context(raw,256)
    assert len(prepared.frame)==70
    assert prepared.trimmed_bars==186
    pd.testing.assert_frame_equal(prepared.frame,raw.tail(70))
    assert prepared.frame.index[-1]==raw.index[-1]


def test_recent_break_abstains_instead_of_reusing_old_regime():
    # The typed class matters: a replay must classify this as the symbol's own
    # short context, not as a retryable worker fault (retrying stops the run).
    with pytest.raises(InsufficientHistory,match='post-price-break'):
        prepare_context(frame([100.]*200+[1.]*30),256)


def test_normal_frame_and_low_priced_stock_keep_full_history():
    for prices in (np.linspace(100.,110.,300),np.linspace(.0001,.00011,300)):
        raw=frame(prices)
        selected=prepare_context(raw,256)
        pd.testing.assert_frame_equal(selected.frame,raw.tail(256))
        assert selected.trimmed_bars==0


def test_future_bars_cannot_change_selection_for_a_pinned_anchor():
    raw=frame([100.]*180+[1.]*80)
    anchor=raw.index[-1]
    future=frame([100.]*180+[1.]*80+[.001]*30)
    a=prepare_context(raw.loc[:anchor],256)
    b=prepare_context(future.loc[:anchor],256)
    pd.testing.assert_frame_equal(a.frame,b.frame)


def test_gradual_tenfold_decline_drops_the_old_price_scale():
    prices = np.geomspace(16, .40, 400)
    assert np.min(prices[1:] / prices[:-1]) > .1
    raw = frame(prices)
    prepared = prepare_context(raw, 400)
    last = int(np.flatnonzero(prices[:-1] > 10 * prices[-1])[-1])
    assert prepared.trimmed_bars == last + 1
    assert len(prepared.frame) == 400 - last - 1 >= 60
    assert prepared.regime_start == raw.index[last + 1].date().isoformat()
    pd.testing.assert_frame_equal(prepared.frame, raw.iloc[last + 1:])


def test_upward_reverse_split_does_not_trigger_collapse_abstention():
    raw=frame([.241]*238+[5.98]*18)
    selected=prepare_context(raw,256)
    pd.testing.assert_frame_equal(selected.frame,raw)
    assert selected.trimmed_bars==0
