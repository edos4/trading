"""P05 shared backtest service: presets, durable jobs, offline adapter.

Runs against a disposable PostgreSQL schema (see conftest). The offline engine
is executed for real with pinned database versions, so these tests exercise the
same adapter the web and desktop frontends will use in P07.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from core.backtest_params import settings_from_values
from core.backtest_service import BacktestService
from core.pattern_edit_store import Conflict, EditError, uid
from core.pattern_editor_contracts import (
    BacktestPreset, BacktestRequest, VersionSelection,
)

PATTERN = "pattern_002_double_top"
BARCA = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "barcache"


def service_for(store) -> BacktestService:
    return BacktestService(store, dataset_root=BARCA)


def values(**overrides):
    base = {
        "mode": "offline", "market": "us", "symbols": ["TXN"],
        "txn_cost_pct": "0.001", "position_notional": "10000",
    }
    base.update(overrides)
    return base


def version_id(service: BacktestService) -> str:
    return next(p["active"] for p in service.versions.catalog() if p["id"] == PATTERN)


def request_for(service: BacktestService, preset: BacktestPreset | None = None,
                *, key: str | None = None, **overrides) -> BacktestRequest:
    if preset is None:
        preset = service.save_preset(uid(), settings_from_values(values(**overrides)))
    return BacktestRequest(
        idempotency_key=key or uid(),
        versions=(VersionSelection(pattern_id=PATTERN, version_id=version_id(service)),),
        preset=preset,
    )


def test_preset_is_frozen_and_generation_guarded(published_pattern_catalog):
    service = service_for(published_pattern_catalog)
    settings = settings_from_values(values())
    preset = service.save_preset("first", settings)
    assert preset.generation == 0
    assert preset.settings == settings
    assert [p.name for p in service.list_presets()] == ["first"]
    assert service.get_preset(preset.preset_id) == preset

    updated = service.save_preset("first (v2)", settings, preset_id=preset.preset_id,
                                  expected_generation=0)
    assert updated.generation == 1
    assert [p.name for p in service.list_presets()] == ["first (v2)"]
    with pytest.raises(Conflict):
        service.save_preset("stale", settings, preset_id=preset.preset_id,
                            expected_generation=0)
    with pytest.raises(EditError, match="name"):
        service.save_preset("  ", settings)


def test_contradictory_stream_settings_are_rejected():
    with pytest.raises(ValueError, match="start date"):
        settings_from_values({"mode": "historical-stream", "symbols": ["TXN"]})
    with pytest.raises(ValueError, match="not both"):
        settings_from_values({"mode": "historical-stream", "symbols": ["TXN"],
                              "start_date": "2026-01-02", "end_date": "2026-02-02",
                              "session_count": 5})
    with pytest.raises(ValueError, match="end date or a session count"):
        settings_from_values({"mode": "historical-stream", "symbols": ["TXN"],
                              "start_date": "2026-01-02"})
    with pytest.raises(ValueError, match="timeframe"):
        settings_from_values({"mode": "offline", "symbols": ["TXN"], "timeframe": "1W"})
    with pytest.raises(ValueError):
        settings_from_values({"mode": "offline"})


def test_offline_run_persists_inputs_and_result(published_pattern_catalog):
    store = published_pattern_catalog
    service = service_for(store)
    request = request_for(service)
    job = service.submit(request)
    assert job["state"] == "queued"
    assert service.submit(request)["id"] == job["id"]  # duplicate Submit is idempotent

    service.execute(job["id"])
    assert service.status(job["id"])["state"] == "completed"
    assert service.progress(job["id"]).state.value == "completed"

    payload = service.result(job["id"])
    assert payload is not None
    metrics = payload["result"].metrics
    assert metrics.trade_count >= 1
    assert metrics.net_pnl == metrics.realized_pnl
    assert metrics.open_position_count == 0
    assert metrics.currency == "USD"
    assert payload["trades"]["trades"]
    assert payload["trades"]["trades"][0]["pattern_version_id"] == request.versions[0].version_id
    assert payload["equity"]

    # the successful run becomes default-eligibility evidence
    with store.connect() as con:
        life = con.execute("SELECT successful_run_id FROM version_lifecycle WHERE version=%s",
                           (request.versions[0].version_id,)).fetchone()
    assert life["successful_run_id"] == job["id"]
    assert service.runs_for_version(request.versions[0].version_id)[0]["state"] == "completed"
    assert service.find_reusable_run(payload["inputs"].inputs_sha256) == job["id"]

    # an identical request with a fresh key resolves to the same frozen inputs
    twin = request_for(service, preset=request.preset)
    plan = service.prepare(twin)
    assert service.freeze_inputs(twin, plan["symbols"]).inputs_sha256 == \
        payload["inputs"].inputs_sha256
    assert service.find_reusable_run(payload["inputs"].inputs_sha256) == job["id"]


def test_duplicate_key_with_different_request_is_rejected(published_pattern_catalog):
    service = service_for(published_pattern_catalog)
    first = request_for(service, key="same-key")
    service.submit(first)
    other = request_for(service, key="same-key", position_notional="25000")
    with pytest.raises(EditError, match="Idempotency"):
        service.submit(other)


def test_frozen_rows_dedupe_a_repeated_session(published_pattern_catalog, tmp_path):
    """Vendor history can return two rows for one session; the replay needs one.

    A midnight-stamped artefact plus the real session bar for the same day made
    the causal replay reject the frozen dataset ("timestamps must be unique").
    """
    import json

    (tmp_path / "us").mkdir()
    bars = [
        {"t": 1267419600, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0},  # 00:00 New York
        {"t": 1267453800, "o": 2.0, "h": 2.0, "l": 2.0, "c": 2.0, "v": 2.0},  # 09:30 New York
    ]
    (tmp_path / "us" / "CDNS.json").write_text(json.dumps(
        {"symbol": "CDNS", "timeframe": "1d", "bars": bars}), encoding="utf-8")

    service = BacktestService(published_pattern_catalog, dataset_root=tmp_path)
    settings = settings_from_values({
        "mode": "historical-stream", "market": "us", "symbols": ["CDNS"],
        "start_date": "2010-03-01", "end_date": "2010-03-02", "warmup_bars": 0,
    })
    rows = service._load_frozen_rows(settings, ["CDNS"])["CDNS"]
    assert len(rows) == 1
    assert rows[0][4] == 2.0  # keeps the real session bar, not the artefact


def test_concurrent_jobs_are_independent(published_pattern_catalog):
    store = published_pattern_catalog
    service = service_for(store)
    a = request_for(service, position_notional="10000")
    b = request_for(service, position_notional="25000")
    ja, jb = service.submit(a), service.submit(b)
    assert ja["id"] != jb["id"]

    errors: list[BaseException] = []

    def run(job_id: str) -> None:
        try:
            service.execute(job_id, worker_id=f"worker-{job_id}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(ja["id"],)),
               threading.Thread(target=run, args=(jb["id"],))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
    assert not errors
    assert service.status(ja["id"])["state"] == "completed"
    assert service.status(jb["id"])["state"] == "completed"

    ra = service.result(ja["id"])["inputs"]
    rb = service.result(jb["id"])["inputs"]
    assert ra.inputs_sha256 != rb.inputs_sha256  # different effective settings
    assert ra.request.idempotency_key != rb.request.idempotency_key


def test_cancel_queued_and_cancelled_run_stays_terminal(published_pattern_catalog):
    service = service_for(published_pattern_catalog)
    request = request_for(service)
    job = service.submit(request)
    assert service.cancel(job["id"])["state"] == "cancelled"
    assert service.status(job["id"])["state"] == "cancelled"
    with pytest.raises(EditError, match="not queued"):
        service.execute(job["id"])


def test_cancel_race_during_execution(published_pattern_catalog):
    service = service_for(published_pattern_catalog)
    request = request_for(service)
    job = service.submit(request)
    event = threading.Event()
    event.set()
    result = service.execute(job["id"], cancel_event=event)
    assert result["state"] == "cancelled"
    assert service.result(job["id"]) is None


def test_stale_lease_is_interrupted_not_rerun(published_pattern_catalog):
    service = service_for(published_pattern_catalog)
    request = request_for(service)
    job = service.submit(request)
    claimed = service.jobs.claim("crashed-worker", lease_seconds=-5)
    assert claimed["id"] == job["id"]
    assert claimed["owner"] == "crashed-worker"
    assert service.jobs.expire_leases() == 1
    status = service.status(job["id"])
    assert status["state"] == "interrupted"
    assert status["error"]["retryable"] is True


def test_retry_is_traceable_and_does_not_duplicate_results(published_pattern_catalog):
    service = service_for(published_pattern_catalog)
    request = request_for(service)
    job = service.submit(request)
    service.execute(job["id"])
    original = service.result(job["id"])["inputs"].inputs_sha256

    retry = service.retry(job["id"])
    assert retry["retry_of"] == job["id"]
    assert retry["attempt"] == 2
    assert retry["state"] == "queued"

    # the completed attempt is untouched until the retry actually runs
    assert service.result(job["id"])["inputs"].inputs_sha256 == original
    service.execute(retry["id"])
    assert service.status(retry["id"])["state"] == "completed"
    assert service.result(retry["id"]) is not None
    assert service.status(job["id"])["state"] == "completed"

    # only finished jobs can be retried
    queued = service.submit(request_for(service))
    with pytest.raises(EditError, match="finished"):
        service.retry(queued["id"])
