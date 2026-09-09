"""`IndicatorEngine.rsi_wilder` must reproduce the `.cjs` `calcRSI14` bar for
bar — the ported pattern gates (H1 RSI >= 70, H2 RSI 50-61, divergence > 3, …)
were all tuned against that exact series.

`.cjs` reference (backtest_doubletop.cjs / backtest_hs_200.cjs / backtest_uc_v14.cjs,
all identical):

    let ag = 0, al = 0;
    for (let i = 1; i <= 14; i++) {
      const ch = closes[i] - closes[i - 1];
      if (ch >= 0) ag += ch; else al -= ch;
    }
    ag /= 14; al /= 14;
    rsi[14] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
    for (let i = 15; i < closes.length; i++) {
      const ch = closes[i] - closes[i - 1];
      ag = (ag * 13 + (ch >= 0 ? ch : 0)) / 14;
      al = (al * 13 + (ch <  0 ? -ch : 0)) / 14;
      rsi[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
    }
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from analysis.indicator_engine import IndicatorEngine
from data.barcache import load as _load_barcache

FIXTURE = str(Path(__file__).parent / "fixtures" / "barcache")
SYMBOLS = ["TXN", "CDNS", "PYPL", "NVDA", "QCOM", "AVGO", "C", "ON", "ADBE"]


def _cjs_rsi14(close: np.ndarray) -> np.ndarray:
    n = len(close)
    rsi = np.full(n, np.nan)
    if n < 16:
        return rsi
    ag = al = 0.0
    for i in range(1, 15):
        ch = close[i] - close[i - 1]
        if ch >= 0:
            ag += ch
        else:
            al -= ch
    ag /= 14.0
    al /= 14.0
    rsi[14] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(15, n):
        ch = close[i] - close[i - 1]
        ag = (ag * 13.0 + (ch if ch >= 0 else 0.0)) / 14.0
        al = (al * 13.0 + (-ch if ch < 0 else 0.0)) / 14.0
        rsi[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return rsi


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_rsi_wilder_matches_cjs_calc_rsi14(symbol):
    candles = _load_barcache("us", symbol, root=FIXTURE)
    assert candles
    df = pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        }
    )
    ours = IndicatorEngine(df).rsi_wilder(14).to_numpy()
    theirs = _cjs_rsi14(df["close"].to_numpy(dtype=float))

    both = np.isfinite(ours) & np.isfinite(theirs)
    assert both.sum() > 100  # a meaningful overlap
    assert np.isfinite(ours).sum() == np.isfinite(theirs).sum()  # same warm-up
    assert np.max(np.abs(ours[both] - theirs[both])) < 1e-9
