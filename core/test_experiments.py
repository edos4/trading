"""Research timing, source coverage and isolated batch lifecycle regressions."""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from zoneinfo import ZoneInfo
from core.experiments.history import is_session
from pydantic import ValidationError

from core.experiments.adapters import SPECS, variants
from core.experiments.catalog import catalog, sources
from core.experiments.contracts import BatchRequest
from core.experiments.history import HistoryUnavailable, available_symbols, frame, load_symbol, normalize_bars
from core.experiments.replay import Cancelled, replay
from core.experiments.service import ExperimentService
from core.experiments.store import Conflict, ExperimentStore


def bars(n: int = 700, *, flat: bool = False) -> list[dict]:
    day = date(2019, 6, 3)
    output = []
    for i in range(n):
        while not is_session("us", day):
            day += timedelta(days=1)
        c = 100.0 if flat else 100 + i * .03 + 8 * math.sin(i / 13)
        o = c - .2
        output.append({"ts": int(datetime.combine(day, datetime.min.time(), ZoneInfo("America/New_York")).replace(hour=9, minute=30).timestamp()),
                       "date": day.isoformat(), "open": o, "high": c + 1,
                       "low": o - 1, "close": c, "volume": 10000.0})
        day += timedelta(days=1)
    return output


def request(**values) -> BatchRequest:
    return BatchRequest(start_date=values.pop("start_date", "2021-06-01"),
                        end_date=values.pop("end_date", "2021-11-30"),
                        idempotency_key=values.pop("idempotency_key", "test-key"), **values)


def test_all_archive_folders_have_versioned_deployable_records():
    entries = catalog()
    root = Path(__file__).resolve().parents[1]
    assert len(entries) == len(sources()) == 449
    assert len({s["id"] for s in entries}) == 449
    for source in sources():
        assert hashlib.sha256(source["writeup"].encode()).hexdigest() == source["writeup_sha256"]
        assert hashlib.sha256(source["plan"].encode()).hexdigest() == source["plan_sha256"]
        if (root / source["source_path"]).exists():
            assert (root / source["source_path"]).read_text() == source["writeup"]
    assert all(entry["blocker"] for entry in entries if entry["availability"] == "blocked")
    assert next(e for e in entries if e["number"] == 442)["availability"] == "blocked"
    assert all(next(e for e in entries if e["number"] == n)["availability"] == "blocked"
               for n in (331,350,374,412,418,429,432,439))


def test_changed_source_revision_is_blocked_until_adapter_review():
    altered=[{**s,"plan_sha256":"changed"} if s["number"] == 406 else s for s in sources()]
    with patch("core.experiments.catalog.sources",return_value=tuple(altered)):
        entry=next(s for s in catalog() if s["number"] == 406)
    assert entry["availability"] == "blocked"
    assert "re-review" in entry["blocker"]


@pytest.mark.parametrize("values", [{"start_date":"2022-01-01","end_date":"2021-01-01"},
                                   {"initial_capital":float("nan")}, {"market":"both"},
                                   {"symbols":["bad\nticker"]}, {"unexpected":True}])
def test_request_rejects_invalid_inputs(values):
    with pytest.raises(ValidationError):
        request(**values)


def test_history_uses_facade_without_newest_limit_and_preserves_failure():
    with patch("data.history.load_daily_bars", return_value=bars(3)) as loader:
        result = load_symbol("AAPL", "us", date(2020,1,3))
        loader.assert_called_once_with("AAPL", market="us")
        assert len(result) == 3
    with patch("data.history.load_daily_bars", return_value=None):
        with pytest.raises(HistoryUnavailable):
            load_symbol("AAPL", "us", date(2020,1,3))
    with patch("data.history.load_daily_bars", return_value=[]):
        assert load_symbol("AAPL", "us", date(2020,1,3)) == []


def test_history_symbol_failure_and_ph_mapping():
    with patch("data.history.list_history_symbols", return_value=[]):
        with pytest.raises(HistoryUnavailable):
            available_symbols("ph", [])
    with patch("data.history.list_history_symbols", return_value=[{"symbol":"BDO.PS","market":"ph"}]):
        assert available_symbols("ph", ["BDO"]) == ["BDO.PS"]
        with pytest.raises(ValueError):
            available_symbols("ph", ["NOPE"])


def test_date_filter_is_inclusive_and_respects_explicit_session_date():
    rows = bars(4)
    labeled = [{**row, "interval": "1d", "ts": row["ts"] - 10 * 3600} for row in rows]
    result = normalize_bars(labeled, "ph", date(2019,6,7), closed_through=date(2019,6,4))
    assert [r["date"] for r in result] == ["2019-06-03", "2019-06-04"]
    assert normalize_bars([rows[0],rows[0]],"us",date(2020,1,5)) == [rows[0]]
    with pytest.raises(ValueError, match="Conflicting"):
        normalize_bars([rows[0],{**rows[0],"volume":1}],"us",date(2020,1,5))
    for key,value in [("close",10000),("volume",float("nan")),("low",-1)]:
        with pytest.raises(ValueError):
            normalize_bars([{**rows[0],key:value}],"us",date(2020,1,5))
    # One bad print does not discard the rest of the symbol.
    bad = {**rows[1], "low": -1}
    kept = normalize_bars([rows[0], bad, rows[2]], "us", date(2020,1,5))
    assert [row["date"] for row in kept] == [rows[0]["date"], rows[2]["date"]]


def test_session_conflict_keeps_the_daily_open_bar_and_drops_unresolved_prints():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/New_York")
    day = date(2010, 2, 1)
    def bar(clock, close, volume):
        hour, minute = clock
        ts = int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).timestamp())
        return {"ts": ts, "date": day.isoformat(), "open": 20.0, "high": max(22.0, close),
                "low": 19.0, "close": close, "volume": volume}
    daily = bar((9, 30), 20.8, 5_000_000)
    monthly = bar((0, 0), 22.5, 100_000_000)
    kept = normalize_bars([monthly, daily], "us", day, closed_through=day)
    assert kept == [daily]
    # Intraday prints without a 09:30 daily bar are not a daily session.
    later = date(2010, 2, 2)
    def intra(hour, minute, close):
        ts = int(datetime(later.year, later.month, later.day, hour, minute, tzinfo=zone).timestamp())
        return {"ts": ts, "date": later.isoformat(), "open": 10.0, "high": 11.0,
                "low": 9.0, "close": close, "volume": 100.0}
    kept = normalize_bars([daily, intra(12, 30, 10.2), intra(13, 30, 10.8)], "us", later, closed_through=later)
    assert kept == [daily]


@pytest.mark.parametrize("number", sorted(SPECS))
def test_adapter_signals_are_prefix_invariant_and_never_use_post_end_data(number):
    rows = bars(700)
    prefix = frame(rows[:650])
    full = frame(rows)
    for a,b in zip(variants(number,prefix),variants(number,full)):
        assert list(a.entry) == list(b.entry[:650])
        assert list(a.exit) == list(b.exit[:650])
    req = request(end_date=rows[649]["date"])
    a = replay(number,"SPY",prefix,req)
    full.loc[650:,"close"] = 900000
    b = replay(number,"SPY",full,req)
    assert a == b


def test_next_open_fills_and_cross_exits_without_invented_brackets():
    rows = bars(620,flat=True)
    for i in range(510,550):
        rows[i].update(close=120,high=121,low=99)
    rows[511].update(open=125,high=126,low=119)
    req = request(start_date="2021-05-01",end_date=rows[-1]["date"],txn_cost_pct=.001,slippage_pct=.001)
    result = replay(406,"SPY",frame(rows),req)
    assert result["trades"]
    for trade in result["trades"]:
        assert trade["entry_date"] > trade["signal_date"]
        assert trade["entry_price"] == pytest.approx(125 * 1.001)
        assert trade["stop_loss"] is None and trade["take_profit"] is None
        assert trade["exit_reason"] == "signal-next-open"
        expected = (trade["exit_price"]-trade["entry_price"])*trade["qty"]-trade["fees"]
        assert trade["pnl"] == pytest.approx(expected)


def test_last_bar_signal_cannot_fill_after_end_date():
    rows = bars(512,flat=True)
    rows[-1].update(close=120, high=121)
    result = replay(406,"SPY",frame(rows),request(start_date="2021-05-01",end_date=rows[-1]["date"]))
    assert result["signals"] and not result["trades"] and not result["positions"]
    assert any(row["status"] == "unfilled" for row in result["logs"])


def test_ph_uses_historical_lots_and_its_own_costs():
    from core.market import PH, pse_board_lot

    result=replay(406,"BDO.PS",frame(bars()),request(market="ph",position_notional=100000))
    assert result["trades"]
    for trade in result["trades"]:
        lot=pse_board_lot(trade["entry_price"],as_of=date.fromisoformat(trade["entry_date"]))
        assert trade["qty"] % lot == 0
        assert trade["action"] == "BUY"
        expected=(trade["entry_price"]+trade["exit_price"])*trade["qty"]*PH.txn_cost_pct
        assert trade["fees"] == pytest.approx(expected)


def test_spy_risk_sizing_is_cash_capped_and_cost_aware():
    rows = bars()
    for i,row in enumerate(rows):
        c = 100 + 2*i
        row.update(open=c-.2,close=c,high=c+.5,low=c-.5)
    req = request(txn_cost_pct=.001,slippage_pct=.001)
    result = replay(40,"SPY",frame(rows),req)
    positions = result["trades"] + result["positions"]
    assert positions
    equity = req.initial_capital
    for position in positions:
        assert position["entry_price"]*position["qty"]+position["entry_fee"] <= equity + 1e-8
        assert position["planned_risk"] <= equity*.005 + 1e-8
        equity += position.get("pnl",0)


def test_research_books_cover_every_former_event_study():
    sample = frame(bars(1100))
    for number, spec in SPECS.items():
        if number in {40, 406}:
            assert spec["kind"] == "trades"
            continue
        assert spec["kind"] == "trades"
        assert spec["parameters"]["hold_bars"] == 20
        found = variants(number, sample)
        assert found
        assert all(item.kind == "trades" and item.policy["hold_bars"] == 20 for item in found)


def test_research_hold_realizes_pnl_and_ph_skips_shorts():
    rows = bars(45, flat=True)
    rows[10].update(open=101, high=102, low=100.6, close=101.2)
    rows[8].update(open=99.0, high=99.5, low=98.9, close=99.2)
    req = request(start_date=rows[0]["date"], end_date=rows[-1]["date"], txn_cost_pct=0, slippage_pct=0)
    result = replay(48, "AAPL", frame(rows), req)
    assert result["trades"] and result["trades"][0]["action"] == "BUY"
    trade = result["trades"][0]
    assert trade["exit_reason"] == "hold-20"
    assert trade["entry_date"] > trade["signal_date"]
    assert trade["pnl"] == pytest.approx((trade["exit_price"] - trade["entry_price"]) * trade["qty"])
    assert result["variants"]["fvg-geometry"]["realized_pnl"] == pytest.approx(trade["pnl"])
    assert result["variants"]["fvg-geometry"]["realized_pnl"] is not None

    short_rows = bars(45, flat=True)
    short_rows[10].update(open=98, high=98.5, low=97, close=97.5)
    short_req = request(start_date=short_rows[0]["date"], end_date=short_rows[-1]["date"], txn_cost_pct=0, slippage_pct=0)
    short = replay(48, "AAPL", frame(short_rows), short_req)
    assert short["trades"] and short["trades"][0]["action"] == "SELL"
    sold = short["trades"][0]
    assert sold["exit_reason"] == "hold-20"
    assert sold["pnl"] == pytest.approx((sold["entry_price"] - sold["exit_price"]) * sold["qty"])

    ph = replay(48, "BDO.PS", frame(short_rows), request(
        market="ph", start_date=short_rows[0]["date"], end_date=short_rows[-1]["date"],
        position_notional=100000, txn_cost_pct=0, slippage_pct=0))
    assert not any(row["action"] == "SELL" for row in ph["trades"])
    assert any(row.get("reason") == "PH book is long-only" for row in ph["logs"])

    rows[9].update(open=100.3, high=101, close=100)  # First real color change.
    anatomy = replay(385, "AAPL", frame(rows), req)
    assert anatomy["trades"] and anatomy["trades"][0]["pnl"] is not None
    assert anatomy["variants"]["candle-anatomy"]["kind"] == "trades"


def test_cancellation_is_checked_inside_replay():
    with pytest.raises(Cancelled):
        replay(385,"AAPL",frame(bars()),request(),cancel=lambda:True)


def test_complete_batch_runs_every_folder_and_keeps_blockers(tmp_path):
    store = ExperimentStore(tmp_path)
    service = ExperimentService(store,enumerate_symbols=lambda *_:["SPY"],loader=lambda *_:bars(1200))
    req=request(start_date="2023-06-01",end_date="2024-04-14")
    result = service.submit(req,background=False)
    assert len(result["runs"]) == 449
    assert result["state"] == "completed-with-blockers"
    assert result["counts"]["completed"] == len(SPECS) == 22
    assert result["counts"]["blocked"] == 449-len(SPECS)
    assert result["progress"]["completed_symbols"] == 1
    folder = next(s["id"] for s in catalog() if s["number"] == 406)
    detail = service.detail(result["id"],folder)
    summary = detail["run"]["summary"]
    for name, values in summary["variants"].items():
        assert summary["equity"][name][-1]["pnl"] == pytest.approx(values["net_pnl"])
    assert store.chart(result["id"],"SPY") == [r for r in bars(1200) if r["date"] <= req.end_date.isoformat()]
    assert service.submit(req,background=False)["id"] == result["id"]
    assert len(store.listing()) == 1


def test_partial_symbol_history_failure_keeps_the_evaluated_folders(tmp_path):
    def loader(symbol, *_):
        if symbol == "BAD":
            raise ValueError("Invalid OHLCV on 2020-01-01")
        return bars(1200)
    service = ExperimentService(ExperimentStore(tmp_path),
                                enumerate_symbols=lambda *_: ["SPY", "BAD"], loader=loader)
    result = service.submit(request(start_date="2023-06-01", end_date="2024-04-14"), background=False)
    assert result["state"] == "completed-with-blockers"
    assert result["counts"].get("failed", 0) == 0
    ready = [run for run in result["runs"] if run["state"] == "completed"]
    assert ready and all(run["summary"]["coverage"]["history_failed"] == 1 for run in ready)
    assert any(run["summary"]["coverage"]["evaluated_symbols"] >= 1 for run in ready)
    assert {row["symbol"] for row in result["progress"]["data_errors"]} == {"BAD"}


def test_data_failure_is_not_a_successful_zero_trade_batch(tmp_path):
    def fail(*_): raise HistoryUnavailable("transport")
    service = ExperimentService(ExperimentStore(tmp_path),enumerate_symbols=lambda *_:["SPY"],loader=fail)
    result = service.submit(request(),background=False)
    assert result["state"] == "failed"
    assert result["counts"]["failed"] == len(SPECS)
    assert result["progress"]["completed_symbols"] == 1
    assert result["progress"]["data_errors"]


def test_store_idempotency_active_conflicts_and_interruption(tmp_path):
    store = ExperimentStore(tmp_path)
    manifest = [{"id":"one","availability":"ready"}]
    raw = request().model_dump(mode="json")
    batch, created = store.create(raw,manifest)
    assert created and store.create(raw,manifest) == (batch,False)
    with pytest.raises(Conflict):
        store.create({**raw,"end_date":"2022-01-01"},manifest)
    with pytest.raises(Conflict):
        ExperimentStore(tmp_path).create({**raw,"idempotency_key":"two"},manifest)
    with store.connect() as con:
        con.execute("UPDATE batches SET lease_until=0 WHERE id=?",(batch,))
    assert store.batch(batch)["state"] == "interrupted"
    assert store.runs(batch)[0]["state"] == "interrupted"
    assert store.create({**raw,"idempotency_key":"two"},manifest)[1]


def test_evidence_pagination_is_complete_and_batch_isolated(tmp_path):
    store = ExperimentStore(tmp_path)
    manifest = [{"id":"one","availability":"ready"}]
    batch,_ = store.create(request().model_dump(mode="json"),manifest)
    store.save_result(batch,"one","SPY",{"signals":[{"symbol":"SPY","date":str(i)} for i in range(5)]})
    first=store.evidence(batch,"one","signals",limit=2)
    second=store.evidence(batch,"one","signals",limit=2,after=first["next"])
    third=store.evidence(batch,"one","signals",limit=2,after=second["next"])
    assert [r["date"] for page in (first,second,third) for r in page["rows"]] == list(map(str,range(5)))
    assert third["next"] is None and first["total"] == 5
    assert not store.evidence("other","one","signals")["rows"]


def test_cancelled_batch_preserves_completed_and_blocked_records(tmp_path):
    store = ExperimentStore(tmp_path)
    def loader(*_):
        batch = store.listing()[0]
        store.cancel(batch["id"])
        return bars()
    service=ExperimentService(store,enumerate_symbols=lambda *_:["SPY"],loader=loader)
    result=service.submit(request(),background=False)
    assert result["state"] == "cancelled"
    assert result["counts"]["cancelled"] == len(SPECS) and result["counts"]["blocked"] == 449-len(SPECS)
    with pytest.raises(KeyError):
        store.chart(result["id"],"SPY")
