"""P09 Patterns tab: the web API drives the durable edit coordinator.

The real FastAPI app is exercised with a TestClient against a disposable
PostgreSQL schema and a fixture dataset. The provider and the candidate sandbox
are deterministic doubles (the real provider needs credentials and the real
sandbox is unavailable here); validation, jobs, versions and backtests are real.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BARCA = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "barcache"
PATTERN = "pattern_003_double_bottom"
SYMBOL = "CDNS"


def rows():
    from data.barcache import load as load_barcache

    candles = load_barcache("us", SYMBOL, root=BARCA)
    assert candles
    return [[c.timestamp.isoformat(), float(c.open), float(c.high), float(c.low),
             float(c.close), float(c.volume or 0.0)] for c in candles]


def _date(index):
    return rows()[index][0][:10]


def settings_values(sessions=0, end=None):
    values = {
        "mode": "historical-stream", "market": "us", "symbols": [SYMBOL],
        "start_date": _date(30), "end_date": end or _date(-1),
        "warmup_bars": 30, "initial_capital": 100000.0, "position_notional": 10000.0,
        "txn_cost_pct": 0.001, "slippage_pct": 0.0, "end_policy": "keep-open",
    }
    if sessions:
        values.pop("end_date")
        values["session_count"] = sessions
    return values


@pytest.fixture
def patterns_web(published_pattern_catalog, edit_doubles, sandbox_double):
    from core.backtest_service import BacktestService
    from core.pattern_edit_service import PatternEditService
    from core.pattern_editor_api import PatternEditor
    from web import patterns as patterns_module

    FakeProvider, Runner = edit_doubles
    store = published_pattern_catalog
    provider = FakeProvider()
    service = PatternEditService(
        store, provider=provider, runner=Runner(store.root),
        backtests=BacktestService(store, dataset_root=BARCA), dataset_root=BARCA)
    editor = PatternEditor(service=service)
    with patch.object(patterns_module.pattern_edits, "_editor", editor):
        patches = [patch("web.auth.settings"), patch("web.app.settings")]
        mocks = [p.start() for p in patches]
        for mock in mocks:
            mock.web_ui_password = "correct-horse"
            mock.web_ui_username = "admin"
            mock.web_ui_secret_key = "test-secret-key"
            mock.web_ui_https = False
            mock.web_ui_session_hours = 12
            mock.enable_kronos = True
            mock.kronos_gate_enabled = True
            mock.volume_gate_enabled = False
            mock.papertrade_stream_start_date = ""
        try:
            from web.app import create_app

            client = TestClient(create_app())
            login = client.post("/login", data={
                "username": "admin", "password": "correct-horse", "next": "/"},
                follow_redirects=False)
            assert login.status_code == 303
            client.cookies.update(login.cookies)
            yield client, service, editor, provider, store
        finally:
            for item in patches:
                item.stop()


def base_version(store) -> str:
    from core.pattern_versions import PatternVersions

    return next(p["active"] for p in PatternVersions(store).catalog()
                if p["id"] == PATTERN)


def await_job(client, job_id, tries=240):
    payload = None
    for _ in range(tries):
        payload = client.get(f"/api/patterns/edits/{job_id}").json()
        if payload["state"] in ("completed", "failed", "cancelled", "blocked", "interrupted"):
            return payload
        time.sleep(0.25)
    raise AssertionError(f"edit job did not finish: {payload}")


def submit(client, store, **overrides):
    body = {
        "pattern_id": PATTERN, "base_version_id": base_version(store),
        "instruction": "Require two closes above the neckline before entry.",
        "preset_name": "patterns web", "settings": settings_values(sessions=40),
    }
    body.update(overrides)
    return client.post("/api/patterns/edits", json=body)


def test_chart_edit_context_candidate_and_dated_detection_charts(patterns_web, monkeypatch):
    client, service, editor, provider, store = patterns_web
    captured = []
    generate = provider.generate
    def record(request):
        captured.append(request)
        return generate(request)
    monkeypatch.setattr(provider, "generate", record)
    tape = rows()
    context = {
        "symbol": SYMBOL, "pattern": PATTERN, "market": "us",
        "pattern_version_id": base_version(store), "timeframe": "1d",
        "candles": [dict(zip(("time", "open", "high", "low", "close"),
                             [r[0][:10], *r[1:5]])) for r in tape[:70]],
        "volume": [{"time": r[0][:10], "value": r[5]} for r in tape[:70]],
    }
    context["manual_corrections"] = [{"id": "drawn", "kind": "pattern", "label": "correct peaks", "points": [
        {"time": context["candles"][i]["time"], "value": 100.0 + i, "pane": "price"} for i in (1, 5, 10)]}]
    response = submit(client, store, chart_context=context, settings=settings_values(sessions=1))
    assert response.status_code == 200, response.text
    detail = await_job(client, response.json()["job_id"])
    assert detail["state"] == "completed", json.dumps({"error": detail["error"], "report": detail["report"]})
    from ai.providers.deepseek import _user_prompt
    prompt = _user_prompt(captured[0])
    assert SYMBOL in prompt and PATTERN in prompt and captured[0].source in prompt
    assert json.loads(captured[0].context[0][1])["candles"] == context["candles"]
    assert json.loads(captured[0].context[0][1])["manual_corrections"] == context["manual_corrections"]
    stored = service.jobs.find(response.json()["job_id"])
    assert stored["payload"]["request"]["chart_context"]["manual_corrections"] == context["manual_corrections"]
    candidate = detail["generated_version_id"]
    assert candidate != base_version(store)  # no automatic publication
    monkeypatch.setattr(service.backtests, "_load_frozen_rows",
                        lambda settings, symbols: {s: tape for s in symbols})
    values = {**settings_values(), "symbols": [SYMBOL, "OTHER"],
              "start_date": "2025-12-29", "end_date": "2025-12-29", "warmup_bars": 200}
    run = client.post(f"/api/patterns/versions/{candidate}/backtest", json=values)
    assert run.status_code == 200, run.text
    run_id = run.json()["run_id"]
    for _ in range(240):
        result = client.get(f"/api/patterns/runs/{run_id}").json()
        if result["state"] in ("completed", "failed"):
            break
        time.sleep(0.25)
    assert result["state"] == "completed", result
    saved = client.get(f"/api/patterns/versions/{candidate}").json()
    assert saved["backtests"][0]["run_id"] == run_id
    detections = result["result"]["detections"]
    assert {d["symbol"] for d in detections} == {SYMBOL, "OTHER"}
    assert all(d["pattern_version_id"] == candidate for d in detections)
    chart = client.get(f"/api/patterns/runs/{run_id}/chart?detection=0")
    assert chart.status_code == 200, chart.text
    payload = chart.json()
    assert payload["pattern_version_id"] == candidate and payload["markers"]
    assert payload["candles"][-1]["time"] <= detections[0]["session"][:10]
    assert client.get(f"/api/patterns/runs/{run_id}/chart?detection=-1").status_code == 400
    assert client.post(f"/api/patterns/versions/{candidate}/backtest",
                       json={**values, "end_date": "1900-01-01"}).status_code == 400


# ── navigation and auth ──────────────────────────────────────────────────
def test_patterns_endpoints_require_auth():
    patches = [patch("web.auth.settings"), patch("web.app.settings")]
    mocks = [p.start() for p in patches]
    for mock in mocks:
        mock.web_ui_password = "correct-horse"
        mock.web_ui_username = "admin"
        mock.web_ui_secret_key = "k"
        mock.web_ui_https = False
        mock.web_ui_session_hours = 12
    try:
        from web.app import create_app

        client = TestClient(create_app())
        for path in ("/patterns", "/api/patterns",
                     f"/api/patterns/{PATTERN}/versions",
                     "/api/patterns/versions/x", "/api/patterns/edits/x",
                     "/api/patterns/provider"):
            assert client.get(path).status_code == 401, path
    finally:
        for item in patches:
            item.stop()


def test_patterns_page_renders_beside_kronos(patterns_web):
    client, *_ = patterns_web
    page = client.get("/patterns")
    assert page.status_code == 200
    assert "pat-submit" in page.text and "pat-instruction" in page.text
    nav = page.text.split("</nav>")[0]
    assert nav.index('href="/kronos"') < nav.index('href="/patterns"')


# ── reads ────────────────────────────────────────────────────────────────
def test_patterns_catalog_versions_source_and_diff(patterns_web):
    client, _service, editor, _provider, store = patterns_web
    catalog = client.get("/api/patterns").json()["patterns"]
    pattern = next(p for p in catalog if p["pattern_id"] == PATTERN)
    assert pattern["default_version_id"] == base_version(store)
    assert pattern["published"] is True

    versions = client.get(f"/api/patterns/{PATTERN}/versions").json()["versions"]
    assert versions[0]["version_id"] == pattern["default_version_id"]
    assert versions[0]["validation"] == "passed"
    assert versions[0]["archived"] is False

    version_id = versions[0]["version_id"]
    detail = client.get(f"/api/patterns/versions/{version_id}").json()
    assert detail["pattern_id"] == PATTERN
    assert detail["lifecycle"]["validation"] == "passed"
    assert detail["files"] and detail["reports"]

    source = client.get(f"/api/patterns/versions/{version_id}/source").json()
    assert "class " in source["files"][source["source_path"]]
    assert source["files"][source["documentation_path"]]

    # the file-import baseline has no parent, so its diff is the whole file
    diff = client.get(f"/api/patterns/versions/{version_id}/diff").json()
    assert source["source_path"] in diff["files"]


def test_patterns_provider_balance_endpoint(patterns_web):
    client, _service, _editor, _provider, _store = patterns_web
    resp = client.get("/api/patterns/provider")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["available"] is True
    assert data["configured"] is True
    assert data["error"] is None
    assert data["model"]
    balance = data["balance"]
    assert balance["is_available"] is True
    assert balance["infos"][0]["currency"] == "USD"
    assert balance["infos"][0]["total_balance"] == "42.00"


def test_patterns_provider_balance_reports_unavailable(patterns_web):
    from ai.providers.deepseek import ProviderUnavailable

    client, _service, _editor, provider, _store = patterns_web
    provider.raises = ProviderUnavailable("DeepSeek credentials are not configured")
    resp = client.get("/api/patterns/provider")
    assert resp.status_code == 200, resp.text  # informational, never a 5xx
    data = resp.json()
    assert data["available"] is False
    assert data["configured"] is False
    assert data["balance"] is None
    assert "not configured" in data["error"]


def test_patterns_provider_balance_call_failure_is_distinct(patterns_web):
    """A configured provider whose balance call fails reads as unavailable."""
    from ai.providers.deepseek import ProviderError

    client, _service, _editor, provider, _store = patterns_web
    provider.raises = ProviderError("DeepSeek transport error: ConnectError",
                                    retryable=True)
    data = client.get("/api/patterns/provider").json()
    assert data["available"] is False and data["configured"] is True
    assert data["retryable"] is True
    assert "transport error" in data["error"]


# ── the full submit → generate → validate → backtest pipeline ────────────
def test_patterns_submit_pipeline_and_reload_reconnect(patterns_web):
    client, service, _editor, provider, store = patterns_web
    created = submit(client, store, idempotency_key="patterns-key-1")
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]

    # a duplicate Submit maps to the same durable job
    duplicate = submit(client, store, idempotency_key="patterns-key-1")
    assert duplicate.json()["job_id"] == job_id

    detail = await_job(client, job_id)
    assert detail["state"] == "completed", detail
    assert provider.calls == 1
    version_id = detail["generated_version_id"]
    assert version_id and version_id != base_version(store)
    assert detail["explanation"] == "Tightened the entry rule."
    assert detail["diff"]["patterns/003_double_bottom.py"].count("+# AI edit") == 1
    assert detail["report"]["status"] == "passed"

    runs = detail["runs"]
    assert runs["candidate"] and runs["base"]
    candidate = client.get(f"/api/patterns/runs/{runs['candidate']}").json()
    base = client.get(f"/api/patterns/runs/{runs['base']}").json()
    assert candidate["state"] == "completed" and base["state"] == "completed"
    assert candidate["result"]["mode"] == "historical-stream"
    assert candidate["result"]["metrics"]["trade_count"] >= 0

    # a reload reconnects to the durable job and the persisted version
    reloaded = client.get(f"/api/patterns/edits/{job_id}").json()
    assert reloaded["state"] == "completed"
    assert reloaded["runs"] == runs
    versions = client.get(f"/api/patterns/{PATTERN}/versions").json()["versions"]
    assert version_id in {v["version_id"] for v in versions}
    # the default never moved on its own
    catalog = client.get("/api/patterns").json()["patterns"]
    assert next(p for p in catalog if p["pattern_id"] == PATTERN)["default_version_id"] \
        == base_version(store)


def test_patterns_failed_validation_preserves_version_and_default(patterns_web):
    client, _service, _editor, provider, store = patterns_web
    # a syntactically invalid candidate fails validation but is preserved
    provider.source_suffix = "\ndef broken(:\n"
    created = submit(client, store)
    job_id = created.json()["job_id"]
    detail = await_job(client, job_id)
    assert detail["state"] == "failed"
    assert detail["error"]["code"] == "validation-failed"
    assert detail["generated_version_id"]
    assert detail["report"]["status"] == "failed"
    assert any(c["name"] == "syntax" and c["outcome"] == "failed"
               for c in detail["report"]["checks"])
    assert next(p for p in client.get("/api/patterns").json()["patterns"]
                if p["pattern_id"] == PATTERN)["default_version_id"] == base_version(store)


def test_patterns_backtest_again_reuses_version_without_provider_call(patterns_web):
    client, _service, _editor, provider, store = patterns_web
    job_id = submit(client, store).json()["job_id"]
    assert await_job(client, job_id)["state"] == "completed"
    calls = provider.calls

    retry = client.post(f"/api/patterns/edits/{job_id}/retry")
    assert retry.status_code == 200, retry.text
    retry_id = retry.json()["job_id"]
    assert retry_id != job_id
    assert await_job(client, retry_id)["state"] == "completed"
    assert provider.calls == calls  # no second model call, no second version


class _UnavailableRunner:
    """Stands in for the real (unconfigured) candidate sandbox."""

    def available(self):
        from core.pattern_edit_worker import SandboxUnavailable

        raise SandboxUnavailable("Candidate sandbox is unavailable")

    def run(self, *_a, **_k):
        from core.pattern_edit_worker import SandboxUnavailable

        raise SandboxUnavailable("Candidate sandbox is unavailable")


def test_patterns_unavailable_sandbox_blocks_and_retries_without_model(patterns_web):
    client, service, _editor, provider, store = patterns_web
    service._runner = _UnavailableRunner()
    base = base_version(store)
    job_id = submit(client, store).json()["job_id"]
    detail = await_job(client, job_id)
    assert detail["state"] == "blocked"
    assert detail["error"]["code"] == "sandbox-unavailable"
    assert detail["error"]["retryable"] is True
    assert detail["report"]["status"] == "blocked"
    assert any(c["outcome"] == "unavailable" for c in detail["report"]["checks"])
    assert detail["generated_version_id"]  # the generated version is preserved
    calls = provider.calls

    retry = client.post(f"/api/patterns/edits/{job_id}/retry").json()
    assert await_job(client, retry["job_id"])["state"] == "blocked"
    assert provider.calls == calls  # a blocked job retries without a new model call
    assert next(p for p in client.get("/api/patterns").json()["patterns"]
                if p["pattern_id"] == PATTERN)["default_version_id"] == base


def test_patterns_expired_job_is_interrupted_and_visible(patterns_web, monkeypatch):
    client, service, editor, _provider, store = patterns_web
    monkeypatch.setattr(editor, "_start", lambda job_id: None)
    job_id = submit(client, store).json()["job_id"]
    # a runner died holding the lease; recovery marks the job interrupted
    service.jobs.register_worker("dead-runner")
    with service.store.transaction() as con:
        con.execute("UPDATE jobs SET state='generating', owner='dead-runner', "
                    "lease_token='x', lease_expires_at=clock_timestamp() - interval '1 hour' "
                    "WHERE id=%s", (job_id,))
    assert service.jobs.expire_leases() >= 1
    detail = client.get(f"/api/patterns/edits/{job_id}").json()
    assert detail["state"] == "interrupted"
    assert detail["error"]["retryable"] is True


def test_patterns_cancellation_is_durable(patterns_web, monkeypatch):
    client, _service, editor, _provider, store = patterns_web
    monkeypatch.setattr(editor, "_start", lambda job_id: None)
    job_id = submit(client, store).json()["job_id"]
    assert client.get(f"/api/patterns/edits/{job_id}").json()["state"] == "queued"
    cancelled = client.post(f"/api/patterns/edits/{job_id}/cancel").json()
    assert cancelled["state"] == "cancelled"
    assert client.get(f"/api/patterns/edits/{job_id}").json()["state"] == "cancelled"


# ── lifecycle actions ────────────────────────────────────────────────────
def test_patterns_default_and_archive_with_replacement(patterns_web):
    client, _service, _editor, _provider, store = patterns_web
    base = base_version(store)
    job_id = submit(client, store).json()["job_id"]
    detail = await_job(client, job_id)
    generated = detail["generated_version_id"]

    catalog = client.get("/api/patterns").json()["patterns"]
    generation = next(p for p in catalog if p["pattern_id"] == PATTERN)["generation"]

    # stale generation is a conflict, not a silent overwrite
    stale = client.post(f"/api/patterns/versions/{generated}/default",
                        json={"expected_generation": generation + 5})
    assert stale.status_code == 409

    promoted = client.post(f"/api/patterns/versions/{generated}/default",
                           json={"expected_generation": generation})
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["pattern"]["default_version_id"] == generated

    # archiving the current default requires a replacement
    generation = promoted.json()["pattern"]["generation"]
    missing = client.post(f"/api/patterns/versions/{generated}/archive",
                          json={"expected_generation": generation})
    assert missing.status_code == 400
    replaced = client.post(f"/api/patterns/versions/{generated}/archive",
                           json={"expected_generation": generation,
                                 "replacement_default_version_id": base})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["pattern"]["default_version_id"] == base
    versions = client.get(
        f"/api/patterns/{PATTERN}/versions?include_archived=true").json()["versions"]
    archived = next(v for v in versions if v["version_id"] == generated)
    assert archived["archived"] is True
    # an archived version cannot become the default again
    generation = replaced.json()["pattern"]["generation"]
    assert client.post(f"/api/patterns/versions/{generated}/default",
                       json={"expected_generation": generation}).status_code == 400


# ── failures and error mapping ───────────────────────────────────────────
def test_patterns_malformed_requests_and_missing_records(patterns_web):
    client, _service, _editor, _provider, store = patterns_web
    assert submit(client, store, instruction="").status_code == 400
    assert submit(client, store, base_version_id="does-not-exist").status_code == 400
    # an offline (or empty) preset cannot drive an automatic edit
    assert submit(client, store, preset_name="x",
                  settings={"mode": "offline", "symbols": [SYMBOL]}).status_code == 400
    assert client.get("/api/patterns/versions/does-not-exist").status_code == 400
    assert client.get("/api/patterns/edits/does-not-exist").status_code == 404
    assert client.get("/api/patterns/runs/does-not-exist").status_code == 404


def test_patterns_database_unavailable_is_503(patterns_web):
    client, _service, editor, _provider, _store = patterns_web
    from core.pattern_editor_db import DatabaseUnavailable
    from web import patterns as patterns_module

    def unavailable(_self):
        raise DatabaseUnavailable("PostgreSQL unavailable; check editor connection")

    with patch.object(patterns_module.PatternEditor, "catalog", unavailable):
        resp = client.get("/api/patterns")
    assert resp.status_code == 503
    assert "unavailable" in resp.json()["detail"].lower()


# ── execution material for a caller with no database ─────────────────────
def _decode(bundle, name):
    data = base64.b64decode(bundle["files"][name])
    ref = bundle["payload"]["files"][name]
    assert len(data) == ref["size_bytes"], name
    assert hashlib.sha256(data).hexdigest() == ref["sha256"], name
    return data


def test_pinned_matches_the_eligible_catalog(patterns_web):
    client, _service, editor, _provider, store = patterns_web
    resp = client.get("/api/patterns/pinned")
    assert resp.status_code == 200
    pinned = resp.json()["versions"]
    expected = {p["id"]: p["active"] for p in editor.versions.catalog()
                if p["enabled"] and p["active"]}
    assert pinned == expected
    assert PATTERN in pinned


def test_pinned_honours_a_disabled_pattern(patterns_web):
    client, _service, _editor, _provider, _store = patterns_web
    resp = client.get("/api/patterns/pinned", params={"disabled": PATTERN})
    assert resp.status_code == 200
    assert PATTERN not in resp.json()["versions"]


def test_bundle_carries_every_executable_file_byte_exact(patterns_web):
    client, _service, editor, _provider, store = patterns_web
    resp = client.get("/api/patterns/bundle")
    assert resp.status_code == 200
    body = resp.json()
    pinned = body["versions"]
    assert pinned
    assert set(body["bundles"]) == set(pinned.values())
    for pattern_id, version_id in pinned.items():
        bundle = body["bundles"][version_id]
        assert bundle["payload"]["version_id"] == version_id
        assert bundle["payload"]["pattern_id"] == pattern_id
        # every file the loader execs is present, not just the detector source
        assert set(bundle["files"]) == set(bundle["payload"]["files"])
        for name in bundle["files"]:
            _decode(bundle, name)


def test_version_bundle_excludes_nothing_and_fails_closed(patterns_web):
    client, _service, _editor, _provider, store = patterns_web
    version_id = base_version(store)
    resp = client.get(f"/api/patterns/versions/{version_id}/bundle")
    assert resp.status_code == 200
    bundle = resp.json()
    assert bundle["payload"]["version_id"] == version_id
    assert set(bundle["files"]) == set(bundle["payload"]["files"])
    assert bundle["payload"]["source_path"] in bundle["files"]
    # unknown material is an error, never an empty bundle
    assert client.get("/api/patterns/versions/does-not-exist/bundle").status_code == 400


def upload_request(backtests):
    """A request built the way a client with no registry database builds one."""
    from core.backtest_service import request_from_values

    return request_from_values(backtests, settings_values(sessions=40),
                               preset_name="uploaded run", persist=False)


def test_run_upload_records_usable_lifecycle_evidence(patterns_web):
    """A run executed elsewhere must read back and satisfy the eligibility query."""
    from core.pattern_edit_store import uid

    client, service, _editor, _provider, store = patterns_web
    request = upload_request(service.backtests)
    run_id = uid()
    payload = service.backtests.run_locally(request, run_id)

    resp = client.post("/api/patterns/runs", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"run_id": run_id, "state": "completed"}

    # The host's own read path must be able to serve it, artifacts included.
    detail = client.get(f"/api/backtest/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["state"] == "completed"
    assert body["result"]["metrics"]["currency"] in ("USD", "PHP")

    pattern_id = request.versions[0].pattern_id
    version_id = request.versions[0].version_id
    with store.transaction() as con:
        pointer = con.execute("SELECT successful_run_id FROM version_lifecycle "
                              "WHERE version=%s", (version_id,)).fetchone()
        # The exact join the default-eligibility check performs.
        evidence = con.execute(
            "SELECT 1 FROM backtest_runs b JOIN jobs j ON j.id=b.job "
            "JOIN run_versions v ON v.run=b.id JOIN results r ON r.run=b.id "
            "WHERE b.id=%s AND v.version=%s AND j.state='completed'",
            (pointer["successful_run_id"], version_id)).fetchone()
    assert pointer["successful_run_id"] == run_id
    assert evidence is not None


def test_run_upload_is_idempotent_and_validated(patterns_web):
    from core.pattern_edit_store import uid

    client, service, _editor, _provider, store = patterns_web
    payload = service.backtests.run_locally(upload_request(service.backtests), uid())

    first = client.post("/api/patterns/runs", json=payload)
    assert first.status_code == 200
    # A retried upload resolves to the run already recorded.
    again = client.post("/api/patterns/runs", json=payload)
    assert again.status_code == 200
    assert again.json() == first.json()

    # Artifacts that do not hash to their reference are refused.
    tampered = dict(payload, artifacts=dict(payload["artifacts"],
                                            trades=base64.b64encode(b"[]").decode()))
    bad = client.post("/api/patterns/runs", json=tampered)
    assert bad.status_code == 400
    assert "content reference" in bad.json()["detail"]

    # A result that violates the contract never reaches the registry.
    malformed = json.loads(json.dumps(payload))
    malformed["result"]["metrics"]["win_rate"] = 5.0
    assert client.post("/api/patterns/runs", json=malformed).status_code == 400

    # A missing artifact is rejected rather than stored half-complete.
    missing = dict(payload, artifacts={k: v for k, v in payload["artifacts"].items()
                                       if k != "logs"})
    assert client.post("/api/patterns/runs", json=missing).status_code == 400


def test_bundle_is_sufficient_to_load_a_pattern_without_a_database(patterns_web):
    """`discover()` must work from the bundle alone -- it execs the closure."""
    client, _service, _editor, _provider, store = patterns_web
    version_id = base_version(store)
    bundle = client.get(f"/api/patterns/versions/{version_id}/bundle").json()

    class BundleStore:
        """Store double backed only by the HTTP bundle, with no database."""

        root = store.root

        def __init__(self, bundle):
            self.bundle = bundle

        def get(self, table, identity, con=None):
            assert table == "versions"
            return self.bundle["payload"]

        def read_blob(self, ref, con=None):
            for name in self.bundle["files"]:
                data = _decode(self.bundle, name)
                if hashlib.sha256(data).hexdigest() == ref["sha256"]:
                    return data
            raise AssertionError(f"bundle is missing blob {ref['sha256']}")

    from core.pattern_loader import VersionPattern

    pattern = VersionPattern(version_id, BundleStore(bundle))
    assert pattern.name == PATTERN
    assert pattern.timeframes
