"""P06 isolated historical-stream replay: causality, isolation, end policy.

The replay drives the same ``_open_trade`` / ``_check_exit`` / ``_close_trade``
functions the paper account uses, but grows its candle store causally (exactly
like the live scanner's ``copy_candles`` window) so future bars cannot influence
earlier signals. ``pattern_003_double_bottom`` is the fixture detector here: it
is the enabled pattern whose detector actually triggers on a causal window,
which is what paper trading sees.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from core.backtest_params import settings_from_values
from core.backtest_service import BacktestService
from core.pattern_edit_store import EditError, uid
from core.pattern_editor_contracts import BacktestRequest, VersionSelection
from core.pattern_loader import discover
from core.pattern_versions import PatternVersions
from core.stream_backtest import ReplayCancelled, StreamReplay

BARCA = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "barcache"
PATTERN = "pattern_003_double_bottom"
SYMBOL = "CDNS"


def _rows(symbol: str = SYMBOL) -> list[list]:
    from data.barcache import load as load_barcache

    candles = load_barcache("us", symbol, root=BARCA)
    assert candles
    return [[c.timestamp.isoformat(), float(c.open), float(c.high), float(c.low),
             float(c.close), float(c.volume or 0.0)] for c in candles]


def _date(rows: list[list], index: int) -> str:
    return datetime.fromisoformat(rows[index][0]).date().isoformat()


def _patterns(store, names=(PATTERN,)):
    pinned = {p["id"]: p["active"] for p in PatternVersions(store).catalog()
              if p["id"] in names}
    return discover(version_set=pinned, store=store)


def _stream_settings(rows: list[list], *, start=30, end=-1, warmup=30,
                     end_policy="keep-open", **overrides):
    values = {
        "mode": "historical-stream", "market": "us", "symbols": [SYMBOL],
        "start_date": _date(rows, start), "end_date": _date(rows, end),
        "warmup_bars": str(warmup), "initial_capital": "100000",
        "position_notional": "10000", "txn_cost_pct": "0.001",
        "slippage_pct": "0", "end_policy": end_policy,
    }
    values.update(overrides)
    return settings_from_values(values)


def _run(store, rows, *, symbol=SYMBOL, patterns=None, start=30, end=-1,
         warmup=30, end_policy="keep-open", **overrides):
    replay = StreamReplay(
        dataset={symbol: rows},
        patterns=patterns or _patterns(store),
        settings=_stream_settings(rows, start=start, end=end, warmup=warmup,
                                  end_policy=end_policy, **overrides),
        session_tz="America/New_York",
    )
    return replay.run()


def _core(artifacts: dict) -> list[tuple]:
    return [(t["sym"], t["pattern"], t["entryDate"], t["entryPrice"], t["shares"],
             t["exitReason"], round(t["pnlUSD"], 6))
            for t in artifacts["trades"]["trades"]]


def _stable(value):
    """Drop freshly assigned provenance IDs so two identical replays compare equal."""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items()
                if k not in ("trade_id", "signal_id")}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def test_replay_is_deterministic_and_produces_trades(published_pattern_catalog):
    store = published_pattern_catalog
    rows = _rows()
    first = _run(store, rows)
    second = _run(store, copy.deepcopy(rows))
    assert json.dumps(_stable(first), sort_keys=True) == \
        json.dumps(_stable(second), sort_keys=True)
    assert first["metrics"]["trade_count"] >= 1
    assert first["trades"]["meta"]["mode"] == "historical-stream"
    assert first["trades"]["meta"]["sessions"] == len(rows)


def test_future_bar_mutation_cannot_change_earlier_fills(published_pattern_catalog):
    store = published_pattern_catalog
    rows = _rows()
    baseline = _run(store, rows)
    trades = baseline["trades"]["trades"]
    assert trades, "fixture must produce a trade so the check is meaningful"
    cutoff = trades[0]["entryDate"]

    mutated = copy.deepcopy(rows)
    for row in mutated:
        if datetime.fromisoformat(row[0]).date().isoformat() > cutoff:
            row[1] = row[3] * 0.5
            row[2] = row[3] * 0.5
            row[4] = row[3] * 0.5
    after = _run(store, mutated)
    assert _core({"trades": {"trades": [t for t in after["trades"]["trades"]
                                        if t["entryDate"] <= cutoff]}}) == \
        _core({"trades": {"trades": [t for t in trades if t["entryDate"] <= cutoff]}})


def test_warmup_sessions_never_trade(published_pattern_catalog):
    store = published_pattern_catalog
    rows = _rows()
    baseline = _run(store, rows)
    assert baseline["metrics"]["trade_count"] >= 2

    start = _date(rows, 140)
    late = _run(store, rows, start=140, warmup=140)
    assert late["metrics"]["trade_count"] >= 1
    assert all(t["entryDate"] >= start for t in late["trades"]["trades"])
    early = {_core({"trades": {"trades": [t]}})[0] for t in baseline["trades"]["trades"]
             if t["entryDate"] < start}
    assert early  # the baseline really does trade before the late start
    assert not early & {_core({"trades": {"trades": [t]}})[0]
                        for t in late["trades"]["trades"]}


def test_end_policy_keep_open_versus_force_close(published_pattern_catalog):
    store = published_pattern_catalog
    rows = _rows()
    keep = _run(store, rows, end=134, end_policy="keep-open")
    assert keep["metrics"]["open_count"] >= 1
    assert keep["metrics"]["unrealized_pnl"] == pytest.approx(
        sum(p["unrealized_usd"] for p in keep["open_positions"]))

    forced = _run(store, rows, end=134, end_policy="force-close")
    assert forced["open_positions"] == []
    assert forced["metrics"]["open_count"] == 0
    assert forced["metrics"]["unrealized_pnl"] == 0.0
    assert "force_close" in {t["exitReason"] for t in forced["trades"]["trades"]}
    assert forced["metrics"]["trade_count"] == keep["metrics"]["trade_count"] + len(
        keep["open_positions"])


def test_replays_are_isolated_from_each_other_and_from_paper(published_pattern_catalog):
    from core.paper_trader import PaperAccount

    store = published_pattern_catalog
    paper = PaperAccount(initial_capital=100_000.0, market="us",
                         slippage_pct=0.0, txn_cost_pct=0.0)
    paper.assume_session_open = True
    before = paper.snapshot_metrics()["equity"]
    rows = _rows()
    expected = {"keep-open": _run(store, rows, end=134, end_policy="keep-open"),
                "force-close": _run(store, rows, end=134, end_policy="force-close")}
    assert expected["keep-open"]["metrics"]["open_count"] > 0
    assert expected["force-close"]["metrics"]["open_count"] == 0

    results: dict[str, dict] = {}
    errors: list[BaseException] = []

    def replay(end_policy: str) -> None:
        try:
            results[end_policy] = _run(store, rows, end=134, end_policy=end_policy)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=replay, args=("keep-open",)),
               threading.Thread(target=replay, args=("force-close",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    assert not errors
    assert set(results) == {"keep-open", "force-close"}
    assert paper.snapshot_metrics()["equity"] == before
    assert paper.open_count() == 0
    # No interference: each concurrent replay equals its sequential twin.
    for policy in ("keep-open", "force-close"):
        assert _stable(results[policy]) == _stable(expected[policy])
    assert results["keep-open"]["metrics"]["open_count"] > \
        results["force-close"]["metrics"]["open_count"]


def test_replay_cancellation_and_bounds(published_pattern_catalog):
    store = published_pattern_catalog
    rows = _rows()
    cancel = threading.Event()
    replay = StreamReplay(
        dataset={SYMBOL: rows}, patterns=_patterns(store),
        settings=_stream_settings(rows), session_tz="America/New_York", cancel=cancel,
        progress=lambda done, total: cancel.set() if done >= 2 else None)
    with pytest.raises(ReplayCancelled):
        replay.run()

    with pytest.raises(EditError, match="after the frozen history ends|no sessions"):
        _run(store, rows, start=0, end=0, start_date="2099-01-01", end_date="2099-02-01")
    with pytest.raises(EditError, match="No frozen daily history"):
        StreamReplay(dataset={}, patterns=_patterns(store),
                     settings=_stream_settings(rows), session_tz="America/New_York")


def test_service_runs_stream_backtest_durably(published_pattern_catalog):
    store = published_pattern_catalog
    service = BacktestService(store, dataset_root=BARCA)
    rows = _rows()
    preset = service.save_preset("stream", _stream_settings(rows))
    version_id = next(p["active"] for p in service.versions.catalog() if p["id"] == PATTERN)
    request = BacktestRequest(
        idempotency_key=uid(),
        versions=(VersionSelection(pattern_id=PATTERN, version_id=version_id),),
        preset=preset,
    )
    job = service.submit(request)
    service.execute(job["id"])
    assert service.status(job["id"])["state"] == "completed"
    payload = service.result(job["id"])
    assert payload is not None
    assert payload["result"].metrics.trade_count >= 1
    assert payload["result"].metrics.currency == "USD"
    assert payload["trades"]["meta"]["mode"] == "historical-stream"
    assert payload["equity"] and payload["equity"][0]["date"]
    assert service.progress(job["id"]).unit == "sessions"
    assert payload["inputs"].request.preset.settings.mode == "historical-stream"
    # frozen inputs are the reusable identity
    assert service.find_reusable_run(payload["inputs"].inputs_sha256) == job["id"]
    assert service.runs_for_version(version_id)[0]["state"] == "completed"


def test_stream_run_can_be_cancelled_and_survives_as_a_record(published_pattern_catalog):
    store = published_pattern_catalog
    service = BacktestService(store, dataset_root=BARCA)
    rows = _rows()
    preset = service.save_preset("stream", _stream_settings(rows))
    version_id = next(p["active"] for p in service.versions.catalog() if p["id"] == PATTERN)
    request = BacktestRequest(
        idempotency_key=uid(),
        versions=(VersionSelection(pattern_id=PATTERN, version_id=version_id),),
        preset=preset)
    job = service.submit(request)
    event = threading.Event()
    event.set()
    result = service.execute(job["id"], cancel_event=event)
    assert result["state"] == "cancelled"
    assert service.result(job["id"]) is None
    with store.connect() as con:
        run = con.execute("SELECT id FROM backtest_runs WHERE id=%s", (job["id"],)).fetchone()
    assert run is not None  # the frozen inputs record survives a cancelled run


def test_representative_replay_throughput(published_pattern_catalog):
    """Measured throughput for the sandbox/batching decision in P06-08."""
    store = published_pattern_catalog
    rows = _rows()
    started = time.monotonic()
    artifacts = _run(store, rows)
    elapsed = max(time.monotonic() - started, 1e-9)
    sessions = artifacts["trades"]["meta"]["sessions"]
    print(f"\nreplay throughput: {sessions} sessions in {elapsed:.2f}s "
          f"({sessions / elapsed:.1f} sessions/s, symbol={SYMBOL}, "
          f"patterns={PATTERN})")
    assert sessions > 0
