"""Portable exit-evidence review: no ledger, mutable export or database needed."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json

import pytest

from analysis.paper_trade_review import replay_trade, review_export, summarize
from core.backtester import _open_trade
from core.test_patternsv2_exits import candle, signal


def saved_trade(short=False, *, opened=False, slip=0):
    s = signal(short)
    s.stop_loss = 103 if short else 97
    s.stop_loss_pct_cap = .03
    s.trailing_stop_pct = .03
    s.trailing_activation_pct = .0525
    first = candle(100/(1-slip if short else 1+slip), 101 if short else 107,
                   93 if short else 99, 94 if short else 106)
    entry = candle(100, 100, 100, 100)
    p = _open_trade(s, entry, 0, execution_model="causal-v1")
    orders = {
        "hard_stop": s.stop_loss, "hard_stop_on_close": False,
        "trailing_stop": None, "trailing_stop_on_close": False,
        "trailing_activated": False, "target": s.take_profit,
        "target_on_close": False, "pending_exit_reason": None,
    }
    def mark(bar, before, fill=None):
        return {
            "date": bar.timestamp.date().isoformat(), "sim_bar": bar.timestamp.isoformat(),
            "close": fill if fill else bar.close, "mtm": 590,
            "exit_check": {
                "orders_before_bar": before,
                "ohlc": {k: getattr(bar, k) for k in ("open", "high", "low", "close")},
                "raw_fill": fill, "reason": "trailing_stop" if fill else None,
            },
        }
    trade = {
        "trade_id": p.trade_id, "symbol": "TEST", "timeframe": "1d", "pattern": s.pattern,
        "action": s.action, "provenance": "patternsv2", "execution_model": "causal-v1",
        "resolved_rules": p.resolved_rules, "entry_fee_paid": 10,
        "entry": 100, "qty": 100, "sim_opened": entry.timestamp.isoformat(),
        "decision_date": (entry.timestamp-timedelta(days=1)).isoformat(),
        "daily_marks": [mark(first, orders)], "mtm": 590,
    }
    if not opened:
        last = candle(95, 98, 94, 97, 1) if short else candle(105, 106, 102, 103, 1)
        trail = 96.82 if short else 102.82
        orders = {**orders, "trailing_stop": trail, "trailing_activated": True}
        trade["daily_marks"].append(mark(last, orders, trail))
        fill = trail * (1+slip if short else 1-slip)
        fee = (100+fill)*.001*100
        net = ((100-fill if short else fill-100)*100)-fee
        trade.update(exit_reason="trailing_stop", sim_closed=last.timestamp.isoformat(),
                     exit=fill, pnl=net, hold_bars=2)
        trade["daily_marks"][-1]["exit_check"]["fees"] = fee
        trade["daily_marks"][-1]["mtm"] = net
    return trade


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("slip", [0, .002])
def test_baseline_reproduces_directional_fills_costs_and_close_activated_trail(short, slip):
    trade = saved_trade(short, slip=slip)
    original = deepcopy(trade)
    row = replay_trade(trade)
    assert row["pnl"] == pytest.approx(trade["pnl"])
    assert row["slippage_pct_per_side"] == pytest.approx(slip)
    assert row["txn_cost_pct_per_side"] == pytest.approx(.001)
    assert row["trailing_armed"] and row["bars"] == 2
    assert row["net_r"] == pytest.approx(trade["pnl"]/300)
    assert trade == original


@pytest.mark.parametrize("short,expected", [(False, 579.4), (True, 580.6)])
def test_2r_cap_executes_on_first_session_without_using_later_bar(short, expected):
    trade = saved_trade(short)
    capped = replay_trade(trade, target_max_r=2)
    assert capped["pnl"] == pytest.approx(expected)
    assert capped["exit_reason"] == "take_profit" and capped["same_session_exit"]
    assert capped["bars"] == 1 and not capped["trailing_armed"]
    assert trade["resolved_rules"]["take_profit"] == (70 if short else 130)


@pytest.mark.parametrize("short", [False, True])
def test_open_mark_is_separate_from_hypothetical_liquidation(short):
    row = replay_trade(saved_trade(short, opened=True))
    assert row["exit_reason"] == "open" and row["marked_open_pnl"] == 590
    assert row["open_liquidation_pnl"] == pytest.approx(580.6 if short else 579.4)


@pytest.mark.parametrize("field", ["pnl", "exit", "hold_bars", "sim_closed"])
def test_changed_saved_close_is_rejected(field):
    trade = saved_trade()
    trade[field] = "changed" if field == "sim_closed" else trade[field] + 1
    with pytest.raises(ValueError, match="does not reproduce"):
        replay_trade(trade)


def test_runtime_drift_missing_bars_and_changed_orders_are_rejected():
    trade = saved_trade()
    trade["resolved_rules"]["engine_sha256"] = "changed"
    with pytest.raises(ValueError, match="runtime differs"):
        replay_trade(trade)
    trade = saved_trade()
    trade["daily_marks"][0]["exit_check"] = None
    with pytest.raises(ValueError, match="OHLC exit evidence"):
        replay_trade(trade)
    trade = saved_trade()
    trade["daily_marks"][1]["exit_check"]["orders_before_bar"]["trailing_stop"] += 1
    with pytest.raises(ValueError, match="prior orders"):
        replay_trade(trade)


def export_book(closed, opened, *, market="us", currency="USD"):
    rows = [replay_trade(t) for t in closed+opened]
    return {
        "market": market, "currency": currency, "closed_trades": closed,
        "open_positions": opened, "signal_logs": [],
        "metrics": {
            "realized_pnl_dollars": sum(t["pnl"] for t in closed),
            "unrealized_pnl_dollars": sum(t["mtm"] for t in opened),
            "fees": sum(r["fees_paid"] for r in rows),
        },
    }


def test_review_keeps_currencies_open_trades_and_counterfactuals_separate(tmp_path):
    path = tmp_path/"export.json"
    data = {"exported_at": "2026-01-07T00:00:00+00:00", "books": [
        export_book([saved_trade()], [saved_trade(True, opened=True)]),
        export_book([saved_trade()], [], market="ph", currency="PHP"),
    ]}
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    result = review_export(path)
    assert [b["currency"] for b in result["books"]] == ["USD", "PHP"]
    us = result["books"][0]
    assert us["baseline_verified"] and us["summary"]["count"] == 1
    assert us["open_marked_pnl"] == 590
    assert us["comparisons"]["long_only_closed"]["count"] == 1
    # Cap can also realize a previously open position; its count is explicit.
    assert us["comparisons"]["target_cap_2r_closed"]["count"] == 2
    assert us["extra_friction_per_side_stress"]["0.001"] < us["summary"]["net_pnl"]
    json.dumps(result, allow_nan=False)
    assert path.read_bytes() == original
    data["books"][0]["metrics"]["fees"] += 1
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="total fees"):
        review_export(path)


def test_empty_and_zero_pnl_samples_are_not_reported_as_winners():
    assert summarize([])["win_rate_pct"] is None
    summary = summarize([{"pnl": 0, "net_r": 0, "exit_reason": "time_exit"}])
    assert summary["wins"] == summary["losses"] == 0
    assert summary["profit_factor"] is None
