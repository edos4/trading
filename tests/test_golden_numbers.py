"""Golden-number regression for the `.cjs`-parity backtest.

Each case runs the fixed exit ladder over a **pinned** barcache snapshot
(`tests/fixtures/barcache/`, frozen 2026-09-04 — do not refresh) and asserts the
exact trade list the Python engine produced once it was reconciled, symbol for
symbol, against the locked `.cjs` scripts on the same bars:

* double-top  → `backtest_doubletop.cjs` + `backtest_14b.cjs`
* head&shoulders → `backtest_hs_200.cjs`
* rounding-bottom → no `.cjs` golden (two-stage curated list); the case only
  guards the `setup_key` dedup — without it the drain loop opens each anchor 8×.

The fixture universes deliberately include the near-misses (INTU/V/XOM/COP/GILD/
CVX for double-top) so a regression that re-introduces a false positive fails
here, not just one that drops a real trade.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.backtester import _core_backtest_symbol, _iter_pattern_classes, _summarize
from core.market import get_market
from data.barcache import load as _load_barcache

pytestmark = pytest.mark.slow

FIXTURE = str(Path(__file__).parent / "fixtures" / "barcache")

# double-top: 161-name `.cjs` run = these 10 confirmed, same entries/exits.
DOUBLE_TOP_UNIVERSE = [
    "TXN", "DIOD", "CDNS", "PINS", "PYPL", "SYK", "DDOG", "DE", "PCAR", "PG",
    "INTU", "V", "XOM", "COP", "GILD", "CVX",  # near-misses — must stay rejected
]
DOUBLE_TOP_TRADES = {
    "TXN": (+3.31, "trailing_stop"),
    "DIOD": (-3.00, "trailing_stop"),
    "CDNS": (+6.37, "time_exit"),
    "PINS": (-3.00, "trailing_stop"),
    "PYPL": (+9.68, "trailing_stop"),
    "SYK": (+1.18, "trailing_stop"),
    "DDOG": (-3.00, "trailing_stop"),
    "DE": (+1.32, "trailing_stop"),
    "PCAR": (+1.66, "trailing_stop"),
    "PG": (-0.44, "trailing_stop"),
}

# head&shoulders: 171-name `.cjs` run = 11 distinct trades (QRVO/MPWR double-count
# one anchor each in the raw `.cjs` list); QCOM only survives with the widened
# open-cutoff (`len - horizon`, not `len - max(5, horizon)`).
HEAD_SHOULDERS_UNIVERSE = [
    "SWKS", "QRVO", "NET", "SNOW", "C", "LRCX", "REGN", "AVGO", "MPWR", "NVDA",
    "QCOM",
]
HEAD_SHOULDERS_TRADES = {
    "SWKS": (+4.40, "time_exit"),
    "QRVO": (+2.38, "time_exit"),
    "NET": (-3.00, "trailing_stop"),
    "SNOW": (-3.00, "trailing_stop"),
    "C": (-3.00, "trailing_stop"),
    "LRCX": (-3.00, "trailing_stop"),
    "REGN": (+0.27, "time_exit"),
    "AVGO": (+1.60, "trailing_stop"),
    "MPWR": (+5.77, "trailing_stop"),
    "NVDA": (-1.31, "trailing_stop"),
    "QCOM": (+6.61, "data_end"),
}

# rounding-bottom has no `.cjs` golden (its `.cjs` is a two-stage curated-list
# backtest), so this case exists only to lock the `setup_key` dedup: without it
# the multi-position drain loop opens the same anchor 8x.
ROUNDING_BOTTOM_UNIVERSE = ["ON", "ADBE"]
ROUNDING_BOTTOM_TRADES = {
    "ON": (+23.42, "take_profit"),
    "ADBE": (-5.00, "stop_loss"),
}


def _run(pattern_name: str, symbols: list[str]):
    pattern = next(c() for _, c in _iter_pattern_classes() if c().name == pattern_name)
    profile = get_market("us")
    config = {
        "position_notional": 10_000.0,
        "txn_cost_pct": 0.0,
        "min_bars": 100,
        "session_tz": profile.session_tz,
        "market": "us",
        "lot_round": profile.lot_round,
    }
    trades = []
    for sym in symbols:
        candles = _load_barcache("us", sym, root=FIXTURE)
        assert candles, f"fixture barcache missing {sym}"
        got, _, _, _ = _core_backtest_symbol(sym, "1d", candles, [pattern], config)
        trades.extend(got)
    return trades


@pytest.mark.parametrize(
    "pattern_name, universe, expected",
    [
        ("pattern_002_double_top", DOUBLE_TOP_UNIVERSE, DOUBLE_TOP_TRADES),
        ("pattern_008_head_and_shoulders", HEAD_SHOULDERS_UNIVERSE, HEAD_SHOULDERS_TRADES),
        ("pattern_004_rounding_bottom", ROUNDING_BOTTOM_UNIVERSE, ROUNDING_BOTTOM_TRADES),
    ],
)
def test_golden_trade_list(pattern_name, universe, expected):
    trades = _run(pattern_name, universe)
    got = {t.symbol: (round(t.pnl_pct, 2), t.exit_reason) for t in trades}

    assert len(trades) == len(expected), (
        f"{pattern_name}: {len(trades)} trades, expected {len(expected)}; "
        f"got {sorted(got)}"
    )
    assert set(got) == set(expected), (
        f"{pattern_name}: symbol set differs — "
        f"extra {sorted(set(got) - set(expected))}, "
        f"missing {sorted(set(expected) - set(got))}"
    )
    for sym, (pnl, reason) in expected.items():
        assert got[sym][1] == reason, f"{sym}: exit {got[sym][1]} != {reason}"
        assert abs(got[sym][0] - pnl) <= 0.02, f"{sym}: pnl {got[sym][0]} != {pnl}"


def test_golden_double_top_summary():
    s = _summarize(_run("pattern_002_double_top", DOUBLE_TOP_UNIVERSE), 10_000.0)
    assert s["trades"] == 10
    assert s["win_rate_pct"] == 60.0
    assert s["total_usd"] == pytest.approx(1388.0, abs=5.0)
    assert s["by_exit_reason"] == {"trailing_stop": 9, "time_exit": 1}


def test_golden_head_shoulders_summary():
    s = _summarize(_run("pattern_008_head_and_shoulders", HEAD_SHOULDERS_UNIVERSE), 10_000.0)
    assert s["trades"] == 11
    assert s["win_rate_pct"] == pytest.approx(54.5, abs=0.1)
    assert s["total_usd"] == pytest.approx(685.0, abs=5.0)
