"""Regressions from the execution and measurement audit."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from core.experiments.history import normalize_bars


def test_quarantine_holiday_weekend_and_ambiguous_monthly_bars():
    def print_at(day, hour, minute, **extra):
        ts = datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=ZoneInfo("America/New_York"))
        return dict(ts=int(ts.timestamp()), date=day, open=1, high=5, low=1, close=5, volume=500, **extra)

    rows = [print_at("2026-01-01", 0, 0), print_at("2026-01-02", 9, 30),
            print_at("2026-02-01", 0, 0), print_at("2026-04-01", 0, 0),
            print_at("2026-04-02", 9, 30, interval="1mo"),
            print_at("2026-04-06", 0, 0, interval="1d")]
    issues = []
    clean = normalize_bars(rows, "us", date(2026, 4, 10), issues=issues)
    assert [r["date"] for r in clean] == ["2026-01-02", "2026-04-06"]
    assert {r["ts"] for r in issues} == {rows[i]["ts"] for i in (0, 2, 3, 4)}


def test_daily_liquidity_proxy_has_causal_size_and_rejects_impossible_fills():
    from core.experiments.history import frame
    from core.experiments.replay import replay
    from core.test_experiments import bars, request

    rows = bars(35, flat=True)
    rows[4].update(low=102, open=103, high=104, close=103)
    req = request(start_date=rows[4]["date"], end_date=rows[-1]["date"], txn_cost_pct=0,
                  slippage_pct=0, participation=.002)
    def run(volume):
        rows[5]["volume"] = volume
        return replay(48, "TEST", frame(rows), req)
    for v in (100, 1000000):
        trade = run(v)["trades"][0]
        assert trade["entry_filled_qty"] == trade["exit_filled_qty"] == 19
        assert trade["entry_requested_qty"] > 19
    for v in (0, 18):
        assert any(log["date"] == rows[5]["date"] and log.get("filled_qty") == 0 for log in run(v)["logs"])
    rows[25]["volume"] = 0
    result = run(10000)
    assert result["trades"][0]["exit_date"] == rows[26]["date"]


def test_independent_books_aggregate_concurrent_capital_without_reference_nav():
    from core.experiments.service import _empty_summary, _merge, _present
    from core.experiments.history import frame
    from core.experiments.replay import replay
    from core.test_experiments import bars, request

    rows = bars(32, flat=True)
    rows[4].update(low=102, open=103, high=104, close=103)
    rows[7].update(low=94, close=95)
    req = request(start_date=rows[4]["date"], end_date=rows[8]["date"], initial_capital=10000)
    summary = _empty_summary({"kind": "trades"})
    for symbol in ("AAA", "BBB"):
        _merge(summary, replay(48, symbol, frame(rows), req))
    small = _present(summary, req)
    large = _present(summary, req.model_copy(update={"initial_capital": 1000000}))
    assert small == large
    m = small["variants"]["fvg-geometry"]
    assert m["max_drawdown_pct"] is None and m["max_drawdown_cash"] > 0
    assert m["peak_committed_basis"] > req.initial_capital
    assert all(point["equity"] is None for point in small["equity"]["fvg-geometry"])


def test_research_identity_and_candle_transitions():
    import numpy as np
    from core.experiments.adapters import SPECS, candle_turns

    assert len({SPECS[n]["family_id"] for n in (115, 385, 433)}) == 1
    assert "retest" not in SPECS[414]["tested_label"].lower()
    assert not SPECS[414]["source_strategy_implemented"]
    assert candle_turns(np.ones(6), np.array([1, 2, 1, 2, 0, 2])).tolist() == [0, 0, 0, 0, -1, 1]


def test_session_hold_and_missing_next_open():
    from core.experiments.history import frame
    from core.experiments.replay import replay
    from core.test_experiments import bars, request

    rows = bars(45, flat=True)
    rows[4].update(low=102, open=103, high=104, close=103)
    req = request(start_date=rows[4]["date"], end_date=rows[-1]["date"], txn_cost_pct=0, slippage_pct=0)
    trade = replay(48, "X", frame(rows[:12]+rows[15:]), req)["trades"][0]
    assert trade["exit_date"] == trade["scheduled_exit"] == rows[25]["date"]
    assert (trade["elapsed_sessions"], trade["bars"]) == (20,17)
    missing = replay(48, "X", frame(rows[:5]+rows[6:]), req)
    assert any(x["status"]=="expired" and x["signal_date"]==rows[4]["date"] for x in missing["logs"])
    assert not any(t["signal_date"]==rows[4]["date"] for t in missing["trades"])
    assert any(o["date"] == rows[4]["date"] and not o["complete"] for o in missing["outcomes"])


def test_gap_down_cannot_increase_a_queued_share_order():
    from core.experiments.history import frame
    from core.experiments.replay import replay
    from core.test_experiments import bars, request

    rows = bars(32, flat=True)
    rows[4].update(low=102, open=103, high=104, close=103)
    rows[5].update(low=69, open=70)
    req = request(start_date=rows[4]["date"], end_date=rows[-1]["date"], participation=1,
                  txn_cost_pct=0, slippage_pct=0)
    r = replay(48, "X", frame(rows), req)
    assert r["signals"][0]["requested_qty"] == r["trades"][0]["qty"] == 97
    rows[5].update(open=140, high=141)
    r = replay(48, "X", frame(rows), req)
    assert r["trades"][0]["entry_requested_qty"] == 97 and r["trades"][0]["qty"] == 71


def test_borrow_assumption_and_daily_accrual():
    import pytest
    from core.experiments.history import frame
    from core.experiments.replay import replay
    from core.test_experiments import bars, request

    rows = bars(32, flat=True)
    rows[4].update(low=95, open=96, high=97, close=96)
    req = request(start_date=rows[4]["date"], end_date=rows[-1]["date"], txn_cost_pct=0, slippage_pct=0)
    assert replay(48, "X", frame(rows), req)["signals"][0]["status"] == "ineligible"
    result = replay(48, "X", frame(rows), req.model_copy(update={"short_policy":"assume-borrow", "borrow_rate":.365}))
    t = result["trades"][0]
    days = (date.fromisoformat(t["exit_date"]) - date.fromisoformat(t["entry_date"])).days
    assert t["borrow_fee"] == pytest.approx(.001 * days * t["qty"] * t["entry_price"])
    assert t["pnl"] == pytest.approx((t["entry_price"]-t["exit_price"])*t["qty"] - t["borrow_fee"])


def test_ph_band_boundaries_and_dated_seller_tax():
    import pytest
    from core.market import pse_execution_price
    from core.experiments.history import frame
    from core.experiments.replay import fee_rate, replay
    from core.test_experiments import bars, request

    assert pse_execution_price(9.994995, buy=True) == 10
    assert pse_execution_price(9.994995, buy=False) == 9.99
    assert pse_execution_price(.00995, buy=False) == .0099
    req = request(market="ph", initial_capital=1000000, position_notional=100000,
                  start_date="2019-06-03", end_date="2019-06-07", participation=1)
    rows = bars(5, flat=True)
    for row in rows:
        row.update(open=9.99, high=10, low=9.98, close=10, volume=1000000)
    rows[0].update(close=9.98)
    pos = replay(385, "TEST.PS", frame(rows), req)["positions"][0]
    assert pos["entry_price"] == 10 and pos["qty"] % 100 == 0
    assert pos["qty"] * pos["entry_price"] + pos["entry_fee"] <= req.position_notional
    for day, tax in ((date(2017,12,29),.005), (date(2025,6,30),.006), (date(2025,7,1),.001)):
        assert fee_rate(req,day,buy=False) - fee_rate(req,day,buy=True) == pytest.approx(tax)


def test_spy_prior_trail_close_and_gap_evidence():
    import numpy as np
    from unittest.mock import patch
    from core.experiments.adapters import Variant
    from core.experiments.history import frame
    from core.experiments.replay import replay
    from core.test_experiments import bars, request

    rows = bars(10, flat=True)
    for row in rows:
        row.update(open=100, high=102, low=99, close=100)
    rows[3].update(close=110, high=110)
    rows[4].update(low=100, close=106, high=110)
    rows[5].update(close=104, high=106)
    rows[6].update(open=80, low=78)
    entries = np.zeros(10, dtype=int)
    entries[1] = 1
    v = Variant("spy-breakout", 1, entries, np.zeros(10, dtype=bool), lambda i: {"atr14":2},
                kind="trades", policy={"finite_account":True,"risk_fraction":.005,"trail_atr":2.5,
                                       "atr":np.full(10,2)})
    req = request(start_date=rows[1]["date"], end_date=rows[-1]["date"], txn_cost_pct=0, slippage_pct=0, participation=.1)
    with patch("core.experiments.replay.variants", return_value=[v]):
        r = replay(40,"SPY",frame(rows),req)
    t = r["trades"][0]
    assert t["initial_stop"] == 95 and t["exit_trigger"]["active_stop"] == 105
    assert t["exit_trigger"]["date"] == rows[5]["date"] and t["exit_trigger"]["close"] == 104
    assert t["exit_price"] == 80 and t["exit_reason"] == "atr-trail" and t["r"] == -4
    assert r["variants"]["spy-breakout"]["loss_r_over_2"] == 1


def test_missing_final_session_keeps_last_traded_mark():
    from core.experiments.history import frame
    from core.experiments.replay import replay
    from core.test_experiments import bars, request

    rows = bars(10, flat=True)
    rows[4].update(low=102, open=103, high=104, close=103)
    rows[7].update(high=501, close=500, volume=0)
    req = request(start_date=rows[4]["date"], end_date=rows[9]["date"], end_policy="force-close")
    result = replay(48, "X", frame(rows[:8]), req)
    pos = result["positions"][0]
    assert not result["trades"] and pos["current"] == rows[6]["close"]
    assert pos["mark_date"] == rows[6]["date"] and pos["mark_age_days"] > 0
    assert result["variants"]["fvg-geometry"]["stale_exposure"] == pos["value"]
    assert any("final-session" in row.get("reason", "") for row in result["logs"])


def test_short_mark_and_estimated_cover_cost():
    import pytest
    from core.experiments.history import frame
    from core.experiments.replay import replay
    from core.test_experiments import bars, request

    rows = bars(8, flat=True)
    rows[4].update(low=95, open=96, high=97, close=96)
    req = request(start_date=rows[4]["date"], end_date=rows[-1]["date"], short_policy="assume-borrow",
                  txn_cost_pct=.001, slippage_pct=.002)
    p = replay(48, "X", frame(rows), req)["positions"][0]
    assert p["signed_exposure"] == -p["value"]
    cover = p["current"] * 1.002
    assert p["liquidation_pnl_estimate"] == pytest.approx(p["unrealized_pnl"] -
        (cover-p["current"] + cover*.001)*p["qty"])


def test_distribution_and_no_loss_profit_factor():
    from core.experiments.service import _empty_summary, _merge, _present
    from core.experiments.history import frame
    from core.experiments.replay import replay
    from core.test_experiments import bars, request

    rows = bars(32, flat=True)
    rows[4].update(low=102, open=103, high=104, close=103)
    req = request(start_date=rows[4]["date"], end_date=rows[26]["date"], txn_cost_pct=0, slippage_pct=0)
    summary = _empty_summary({"kind":"trades"})
    for symbol, exit_price in (("A",100), ("B",110), ("C",200)):
        rows[25].update(open=exit_price,high=exit_price+1)
        _merge(summary,replay(48,symbol,frame(rows),req))
    stats = _present(summary,req,final=True)["variants"]["fvg-geometry"]
    assert stats["p05_pnl"] < stats["median_pnl"] < stats["p95_pnl"]
    assert stats["profit_factor_status"] == "profits without losses"
    assert list(stats["best_symbols"]) == ["C","B","A"]
    assert abs(stats["realized_without_best_three"]) < .000001


def test_entry_funnel_reconciles():
    from core.experiments.history import frame
    from core.experiments.replay import replay
    from core.test_experiments import bars, request
    from core.experiments.adapters import SPECS

    req = request()
    r = replay(120,"TEST",frame(bars()),req)
    m = r["variants"]["component-measurements"]
    f = m["funnel"]
    assert f["raw"] == f["suppressed"] + f["ineligible"] + f["eligible"]
    assert f["filled"] == f["open"] + f["closed"]
    assert f["queued"] == f["filled"] + f["expired"] + f["rejected"]
    assert f["ineligible"] > 0 and f["exit_signals"] > 0
    assert "ph" not in SPECS[431]["markets"]
