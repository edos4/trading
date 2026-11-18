"""Research timing, source coverage and isolated batch lifecycle regressions."""
from __future__ import annotations

import hashlib
import math
import time
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
from core.experiments.history import (
    HistoryUnavailable, available_symbols, frame, load_symbol, normalize_bars, resolve_universe,
)
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
                                   {"symbols":["bad\nticker"]}, {"unexpected":True},
                                   {"n_symbols":4}, {"n_symbols":20001}])
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


def test_top_n_universe_follows_the_paper_ranking_and_skips_names_without_history():
    listed = [{"symbol": "MSFT", "market": "us"}, {"symbol": "AAPL", "market": "us"},
              {"symbol": "STALE", "market": "us"}]
    with patch("data.history.list_history_symbols", return_value=listed), \
         patch("data.tv_client.TVClient.fetch_universe_cached",
               return_value=[("NVDA", "NASDAQ"), ("AAPL", "NASDAQ"), ("MSFT", "NASDAQ")]) as fetch:
        assert resolve_universe("us", [], 50) == ["AAPL", "MSFT"]
        fetch.assert_called_once()
        assert fetch.call_args.args[:2] == (50, "us")
    with patch("data.history.list_history_symbols", return_value=listed), \
         patch("data.tv_client.TVClient.fetch_universe_cached", side_effect=AssertionError("screener")):
        assert resolve_universe("us", ["MSFT"], 50) == ["MSFT"]
    with patch("data.history.list_history_symbols", return_value=[{"symbol": "BDO.PS", "market": "ph"}]), \
         patch("data.tv_client.TVClient.fetch_universe_cached",
               return_value=[("BDO", "PSE"), ("TEL", "PSE")]):
        assert resolve_universe("ph", [], 30) == ["BDO.PS"]
    with patch("data.tv_client.TVClient.fetch_universe_cached", return_value=[]):
        with pytest.raises(HistoryUnavailable, match="No top symbols"):
            resolve_universe("us", [], 50)
    with patch("data.history.list_history_symbols", return_value=listed), \
         patch("data.tv_client.TVClient.fetch_universe_cached", return_value=[("NVDA", "NASDAQ")]):
        with pytest.raises(HistoryUnavailable, match="have daily history"):
            resolve_universe("us", [], 50)
    with patch("data.history.list_history_symbols", return_value=listed):
        assert resolve_universe("us", []) == ["AAPL", "MSFT", "STALE"]


def test_batch_forwards_the_top_n_universe(tmp_path):
    seen = {}

    def enumerate_symbols(market, selected, n_symbols=None):
        seen.update(market=market, selected=list(selected), n_symbols=n_symbols)
        return ["SPY"]

    folder = next(s["id"] for s in catalog() if s["number"] == 406)
    service = ExperimentService(ExperimentStore(tmp_path), enumerate_symbols=enumerate_symbols,
                                loader=lambda *_: bars(1200))
    result = service.submit(request(
        n_symbols=40, source_ids=[folder], idempotency_key="top-n", short_policy="assume-borrow",
    ), background=False)
    assert seen == {"market": "us", "selected": [], "n_symbols": 40}
    assert result["request"]["n_symbols"] == 40
    assert result["progress"]["symbols"] == ["SPY"]
    assert result["progress"]["universe_rank"] == {"requested": 40, "with_history": 1, "order": "value"}

    explicit = service.submit(request(
        n_symbols=40, symbols=["qqq"], source_ids=[folder], idempotency_key="explicit",
        short_policy="assume-borrow",
    ), background=False)
    assert seen["selected"] == ["QQQ"] and seen["n_symbols"] == 40
    assert "universe_rank" not in explicit["progress"]


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
    from core.market import pse_board_lot

    result=replay(406,"BDO.PS",frame(bars()),request(market="ph",position_notional=100000))
    assert result["trades"]
    for trade in result["trades"]:
        lot=pse_board_lot(trade["entry_price"],as_of=date.fromisoformat(trade["entry_date"]))
        assert trade["qty"] % lot == 0
        assert trade["action"] == "BUY"
        expected=(trade["entry_price"]*.003006+trade["exit_price"]*.009006)*trade["qty"]
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
    short_req = request(short_policy="assume-borrow", borrow_rate=0, start_date=short_rows[0]["date"], end_date=short_rows[-1]["date"], txn_cost_pct=0, slippage_pct=0)
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
    req=request(start_date="2023-06-01",end_date="2024-04-14",short_policy="assume-borrow")
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


def test_empty_and_out_of_range_history_have_their_own_coverage_buckets(tmp_path):
    def loader(symbol, *_):
        if symbol == "EMPTY":
            return []
        if symbol == "OLD":
            return bars(30)
        return bars(1200)
    service = ExperimentService(ExperimentStore(tmp_path),
                                enumerate_symbols=lambda *_: ["SPY", "EMPTY", "OLD"], loader=loader)
    result = service.submit(request(start_date="2022-10-01", end_date="2023-04-14"), background=False)
    folder = next(s["id"] for s in catalog() if s["number"] == 115)
    coverage = next(run["summary"]["coverage"] for run in result["runs"] if run["source"] == folder)
    assert coverage == {"evaluated_symbols": 1, "history_failed": 0, "insufficient_history": 0,
                        "outside_source_universe": 0, "no_history": 1, "no_in_range_bars": 1}
    for run in result["runs"]:
        counted = run["summary"].get("coverage")
        if counted:
            assert sum(counted.values()) == 3


def test_partial_symbol_history_failure_keeps_the_evaluated_folders(tmp_path):
    def loader(symbol, *_):
        if symbol == "BAD":
            raise ValueError("Invalid OHLCV on 2020-01-01")
        return bars(1200)
    service = ExperimentService(ExperimentStore(tmp_path),
                                enumerate_symbols=lambda *_: ["SPY", "BAD"], loader=loader)
    result = service.submit(request(start_date="2023-06-01", end_date="2024-04-14",short_policy="assume-borrow"), background=False)
    assert result["state"] == "completed-with-blockers"
    assert result["counts"].get("failed", 0) == 0
    ready = [run for run in result["runs"] if run["state"] == "completed"]
    assert ready and all(run["summary"]["coverage"]["history_failed"] == 1 for run in ready)
    assert any(run["summary"]["coverage"]["evaluated_symbols"] >= 1 for run in ready)
    assert {row["symbol"] for row in result["progress"]["data_errors"]} == {"BAD"}


def test_data_failure_is_not_a_successful_zero_trade_batch(tmp_path):
    def fail(*_): raise HistoryUnavailable("transport")
    service = ExperimentService(ExperimentStore(tmp_path),enumerate_symbols=lambda *_:["SPY"],loader=fail)
    result = service.submit(request(short_policy="assume-borrow"),background=False)
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
    result=service.submit(request(short_policy="assume-borrow"),background=False)
    assert result["state"] == "cancelled"
    assert result["counts"]["cancelled"] == len(SPECS) and result["counts"]["blocked"] == 449-len(SPECS)
    with pytest.raises(KeyError):
        store.chart(result["id"],"SPY")


def _batch_fingerprint(batch: dict) -> dict:
    runs = [(run["source"], run["state"], run["error"], run["summary"], run["progress"]) for run in batch["runs"]]
    progress = batch["progress"]
    return {"state": batch["state"], "counts": batch["counts"], "runs": runs,
            "completed_symbols": progress.get("completed_symbols"),
            "dataset_sha256": progress.get("dataset_sha256"),
            "quarantined_rows": progress.get("quarantined_rows"),
            "data_errors": progress.get("data_errors")}


def _saved_evidence(store: ExperimentStore, batch_id: str) -> list:
    found = []
    for run in store.runs(batch_id):
        kinds = ["signals", "trades", "positions", "outcomes", "logs"]
        if run["summary"].get("holdout"):
            kinds += [f"holdout-{kind}" for kind in kinds[:]]
        for kind in kinds:
            after = 0
            rows = []
            while True:
                page = store.evidence(batch_id, run["source"], kind, after=after, limit=500)
                rows.extend({key: value for key, value in row.items() if key != "cursor"} for row in page["rows"])
                if page["next"] is None:
                    break
                after = page["next"]
            if rows:
                found.append((run["source"], kind, rows))
    return found


def test_parallel_scan_matches_sequential_results(tmp_path):
    symbols = ["SPY", "BAD", "QQQ"]

    def loader(symbol, *_):
        if symbol == "BAD":
            raise ValueError("Invalid OHLCV on 2020-01-01")
        return bars(220)

    def run(path, workers, key, *, background=False):
        service = ExperimentService(ExperimentStore(path), enumerate_symbols=lambda *_: symbols,
                                    loader=loader, workers=workers)
        rows = bars(220)
        batch = service.submit(request(start_date=rows[20]["date"], end_date=rows[-1]["date"],
                                       holdout_start=rows[120]["date"], short_policy="assume-borrow",
                                       idempotency_key=key), background=background)
        if background:
            deadline = time.time() + 90
            while batch["state"] in {"queued", "loading", "processing"}:
                assert time.time() < deadline, batch.get("error") or batch["state"]
                time.sleep(0.05)
                batch = service.snapshot(batch["id"])
        return service, batch

    sequential, seq = run(tmp_path / "seq", 1, "seq")
    parallel, par = run(tmp_path / "par", 2, "par", background=True)
    assert _batch_fingerprint(seq) == _batch_fingerprint(par)
    assert _saved_evidence(sequential.store, seq["id"]) == _saved_evidence(parallel.store, par["id"])
    assert sequential.store.chart(seq["id"], "SPY") == parallel.store.chart(par["id"], "SPY")
    assert sequential.store.chart(seq["id"], "QQQ") == parallel.store.chart(par["id"], "QQQ")
    with pytest.raises(KeyError):
        sequential.store.chart(seq["id"], "BAD")
    assert seq["progress"]["scan_workers"] == 1
    assert par["progress"]["scan_workers"] == 2
    assert par["progress"]["inflight"] == 0


def test_parallel_cancel_during_load_discards_the_symbol(tmp_path):
    store = ExperimentStore(tmp_path)

    def loader(*_):
        store.cancel(store.listing()[0]["id"])
        return bars(40)

    service = ExperimentService(store, enumerate_symbols=lambda *_: ["SPY", "QQQ"], loader=loader, workers=2)
    result = service.submit(request(short_policy="assume-borrow", idempotency_key="cancel-parallel"), background=False)
    assert result["state"] == "cancelled"
    assert result["counts"]["cancelled"] == len(SPECS) and result["counts"]["blocked"] == 449 - len(SPECS)
    with pytest.raises(KeyError):
        store.chart(result["id"], "SPY")


def test_worker_loads_and_replays_one_symbol(monkeypatch):
    monkeypatch.setattr("core.experiments.history.raw_symbol", lambda *_: bars(80))
    from core.experiments.scan_worker import scan_symbol

    out = scan_symbol({"symbol": "SPY", "load": True, "store_root": "", "batch_id": "",
                       "request": request(short_policy="assume-borrow").model_dump(mode="json"),
                       "sources": [{"id": "candle", "number": 115, "symbols": None}]})
    assert out["error_type"] is None and out["rows"]
    assert out["results"][0]["result"]["symbol"] == "SPY"
