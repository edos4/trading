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
