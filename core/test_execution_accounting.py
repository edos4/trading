"""Flat-notional accounting + the fixed exit ladder (`.cjs` methodology)."""

from datetime import datetime, timezone

from core.backtester import (
    BacktestResult,
    _apply_notional_sizing,
    _check_exit,
    _close_trade,
    _open_trade,
)
from core.engine_defaults import ENGINE
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


def _c(o, h, l, c, t="2026-01-05"):
    return OHLCVCandle(open=o, high=h, low=l, close=c, volume=1_000,
                       timestamp=datetime.fromisoformat(t).replace(tzinfo=timezone.utc))


def _sig(**kw):
    base = dict(symbol="T", action="BUY", pattern="pattern_009_flag_pattern",
               timeframe="1d", confidence=1.0, price=100.0, qty=0.0)
    base.update(kw)
    return TradeSignal(**base)


def test_notional_sizing_floor_vs_fractional():
    s = _sig(price=100.0)
    _apply_notional_sizing(s, 10_000.0, fractional=False)
    assert s.qty == 100.0
    s = _sig(price=333.0)
    _apply_notional_sizing(s, 10_000.0, fractional=False)
    assert s.qty == 30.0  # floor(10000/333)
    s = _sig(price=333.0)
    _apply_notional_sizing(s, 10_000.0, fractional=True)
    assert abs(s.qty - 10_000.0 / 333.0) < 1e-9


def test_pnl_usd_long_and_short_no_cost():
    long = _open_trade(_sig(action="BUY", price=100.0, qty=100.0), _c(100, 100, 100, 100), 0)
    _close_trade(long, 110.0, "take_profit", _c(110, 110, 110, 110), 0.0)
    assert long.pnl == 10.0 and round(long.pnl_usd, 6) == 1000.0

    short = _open_trade(_sig(action="SELL", pattern="pattern_002_double_top",
                             price=100.0, qty=100.0), _c(100, 100, 100, 100), 0)
    _close_trade(short, 93.0, "take_profit", _c(93, 93, 93, 93), 0.0)
    assert short.pnl == 7.0 and round(short.pnl_usd, 6) == 700.0


def test_result_summary_matches_cjs_shape():
    r = BacktestResult(position_notional=10_000.0)
    for px_exit, reason in [(110, "take_profit"), (97, "trailing_stop"), (105, "time_exit")]:
        t = _open_trade(_sig(price=100.0, qty=100.0), _c(100, 100, 100, 100), 0)
        _close_trade(t, float(px_exit), reason, _c(px_exit, px_exit, px_exit, px_exit), 0.0)
        r.trades.append(t)
    s = r._s
    assert s["trades"] == 3 and s["wins"] == 2 and s["losses"] == 1
    assert s["total_usd"] == round(1000 + (-300) + 500, 2)
    assert s["worst_usd"] == -300.0
    assert set(s["by_exit_reason"]) == {"take_profit", "trailing_stop", "time_exit"}


def test_exit_ladder_hard_stop_before_target():
    # long with a close-based stop at 95 and target at 120: a bar closing at 94
    # must exit stop_loss, not wait for the target.
    t = _open_trade(
        _sig(price=100.0, qty=100.0, stop_loss=95.0, stop_loss_on_close=True,
             take_profit=120.0),
        _c(100, 100, 100, 100), 0,
    )
    px, reason = _check_exit(_c(96, 99, 93, 94), t, 1)
    assert reason == "stop_loss" and px == 95.0


def test_dual_stop_cap_tightens():
    # short: structural stop 112, 5% cap -> effective 105.
    t = _open_trade(
        _sig(action="SELL", pattern="pattern_006_upward_channel", price=100.0,
             qty=100.0, stop_loss=112.0, stop_loss_on_close=True, stop_loss_pct_cap=0.05),
        _c(100, 100, 100, 100), 0,
    )
    px, reason = _check_exit(_c(103, 108, 102, 106), t, 1)
    assert reason == "stop_loss" and px == 105.0
