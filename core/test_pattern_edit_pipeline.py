"""P08 DeepSeek adapter and the durable edit/validate/backtest pipeline.

The provider is exercised for real over ``httpx.MockTransport`` (no network).
The pipeline runs against PostgreSQL with a deterministic provider double and a
sandbox double that executes the *same* trusted evaluator in a subprocess; the
real sandbox is unavailable in this environment, and the blocked path is
asserted separately so an unavailable sandbox can never silently fall back.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from ai.providers.deepseek import (
    DeepSeekProvider, EditGenerationRequest, ProviderError, ProviderUnavailable,
)
from core.backtest_service import BacktestService
from core.pattern_edit_service import PatternEditService
from core.pattern_edit_store import EditError, uid
from core.pattern_editor_contracts import BacktestPreset, EditRequest
from core.pattern_versions import PatternVersions

BARCA = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "barcache"
PATTERN = "pattern_003_double_bottom"
SYMBOL = "CDNS"


# ── provider unit tests (real adapter, mocked transport) ─────────────────
def _provider(handler, **kwargs) -> DeepSeekProvider:
    return DeepSeekProvider(
        api_key="test-key", base_url="https://api.example.test",
        model="deepseek-flash", max_retries=kwargs.pop("max_retries", 0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None, **kwargs)


def _generation_request() -> EditGenerationRequest:
    return EditGenerationRequest(
        instruction="Require two closes above the neckline before entry.",
        pattern_id=PATTERN, source_path="patterns/003_double_bottom.py",
        documentation_path="patterns/003_double_bottom.md",
        source="class X: pass", documentation="# X",
        interface={"class_name": "DoubleBottomPattern", "name": PATTERN,
                   "timeframes": ["1d"]})


def _ok_body(**overrides):
    content = {"source": "class X: pass\n", "documentation": "# X\n",
               "explanation": "Tightened the entry."}
    content.update(overrides)
    return {"id": "req-123", "model": "deepseek-flash-2026-09-01",
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"total_tokens": 12}}


def test_provider_parses_structured_response_and_records_models():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    generation = _provider(handler).generate(_generation_request())
    assert generation.source.startswith("class X")
    assert generation.explanation == "Tightened the entry."
    assert generation.requested_model == "deepseek-flash"
    assert generation.returned_model == "deepseek-flash-2026-09-01"
    assert generation.request_id == "req-123"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "deepseek-flash"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    # only the pattern pair is sent, never credentials or other files
    assert "test-key" not in json.dumps(seen["body"]["messages"])


def test_provider_retries_transient_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json=_ok_body())

    generation = _provider(handler, max_retries=2).generate(_generation_request())
    assert calls["n"] == 2
    assert generation.source


def test_provider_failures_are_bounded_and_classified():
    def status(code):
        return lambda request: httpx.Response(code, json={"error": "nope"})

    with pytest.raises(ProviderError) as perf:
        _provider(status(400)).generate(_generation_request())
    assert perf.value.retryable is False

    with pytest.raises(ProviderError) as transient:
        _provider(status(503), max_retries=1).generate(_generation_request())
    assert transient.value.retryable is True

    def malformed(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    with pytest.raises(ProviderError, match="JSON object"):
        _provider(malformed).generate(_generation_request())

    def missing(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"source": "x", "documentation": ""})}}]})

    with pytest.raises(ProviderError, match="documentation"):
        _provider(missing).generate(_generation_request())

    def extra(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"source": "x", "documentation": "y", "explanation": "z", "extra": 1})}}]})

    with pytest.raises(ProviderError, match="unexpected keys"):
        _provider(extra).generate(_generation_request())

    def oversized(request):
        return httpx.Response(200, content=b"x" * 64,
                              headers={"content-type": "application/json"})

    with pytest.raises(ProviderError, match="size limit"):
        _provider(oversized, max_output_bytes=16).generate(_generation_request())


def test_provider_reports_missing_configuration():
    provider = DeepSeekProvider(api_key="", base_url="https://api.example.test")
    assert provider.available() is False
    with pytest.raises(ProviderUnavailable):
        provider.generate(_generation_request())


# ── account balance ──────────────────────────────────────────────────────
def _balance_body(**overrides):
    body = {"is_available": True, "balance_infos": [
        {"currency": "USD", "total_balance": "1.89",
         "granted_balance": "0.00", "topped_up_balance": "1.89"}]}
    body.update(overrides)
    return body


def test_provider_parses_balance_and_caches_briefly():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_balance_body())

    provider = _provider(handler)
    snapshot = provider.balance()
    assert seen["method"] == "GET" and seen["auth"] == "Bearer test-key"
    assert snapshot.is_available is True
    assert snapshot.infos[0].currency == "USD"
    assert snapshot.infos[0].total_balance == "1.89"
    assert snapshot.infos[0].topped_up_balance == "1.89"

    calls = {"n": 0}

    def counting(request):
        calls["n"] += 1
        return httpx.Response(200, json=_balance_body())

    provider = _provider(counting)
    provider.balance()
    provider.balance()
    assert calls["n"] == 1  # cached within the TTL
    provider.balance(ttl=0)
    assert calls["n"] == 2


def test_provider_balance_failures_are_classified():
    def status(code):
        return lambda request: httpx.Response(code, json={"error": "nope"})

    with pytest.raises(ProviderError) as excinfo:
        _provider(status(402)).balance()
    assert excinfo.value.retryable is False

    with pytest.raises(ProviderError) as transient:
        _provider(status(503), max_retries=1).balance()
    assert transient.value.retryable is True

    def malformed(request):
        return httpx.Response(200, json={"is_available": True})

    with pytest.raises(ProviderError, match="balance information"):
        _provider(malformed).balance()

    def missing_amount(request):
        return httpx.Response(200, json=_balance_body(balance_infos=[
            {"currency": "USD"}]))

    with pytest.raises(ProviderError, match="malformed balance"):
        _provider(missing_amount).balance()


def test_provider_balance_requires_configuration():
    provider = DeepSeekProvider(api_key="", base_url="https://api.example.test")
    with pytest.raises(ProviderUnavailable):
        provider.balance()


def test_provider_timeout_is_retryable_and_bounded():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectTimeout("connect timed out")

    with pytest.raises(ProviderError) as excinfo:
        _provider(handler, max_retries=1).generate(_generation_request())
    assert excinfo.value.retryable is True
    assert calls["n"] == 2  # one initial attempt plus one bounded retry


def test_provider_bounds_concurrent_calls():
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def handler(request):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.03)
        with lock:
            state["active"] -= 1
        return httpx.Response(200, json=_ok_body())

    provider = _provider(handler, max_concurrency=2)
    failures = []

    def worker():
        try:
            provider.generate(_generation_request())
        except Exception as exc:  # noqa: BLE001 - reported to the assertion below
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures
    assert 1 <= state["peak"] <= 2  # the provider never exceeds its slot count


# ── pipeline harness ─────────────────────────────────────────────────────
class FakeProvider:
    """Deterministic provider double; counts calls to prove retry reuse."""

    def __init__(self, *, source_suffix="\n# AI edit\n", raises=None):
        self.calls = 0
        self.raises = raises
        self.source_suffix = source_suffix

    def available(self):
        return self.raises is not ProviderUnavailable

    def generate(self, request):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return _Generation(
            source=request.source + self.source_suffix,
            documentation=request.documentation + "\n\nAI edit: tightened entry.\n",
            explanation="Tightened the entry rule.",
            requested_model="deepseek-flash", returned_model="deepseek-flash",
            request_id=f"req-{self.calls}")


def _Generation(**kwargs):
    from ai.providers.deepseek import EditGeneration

    return EditGeneration(**kwargs)


class InProcessRunner:
    """Sandbox double: runs the same trusted evaluator in a plain subprocess."""

    def __init__(self, root):
        self.root = Path(root)

    def available(self):
        return self.root, sys.executable

    def run(self, files, request, cancel=None):
        request = dict(request)
        request["test_paths"] = [
            str(self.root / p.removeprefix("/input/"))
            for p in request.get("test_paths", [])
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "input"
            root.mkdir(parents=True)
            for name, data in files.items():
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            driver = (
                "import contextlib, importlib.util, io, json, sys;"
                "spec=importlib.util.spec_from_file_location('ev', sys.argv[1]);"
                "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
                "buf=io.StringIO();ctx=contextlib.redirect_stdout(buf);"
                "ctx.__enter__();res=m.evaluate(json.loads(sys.argv[2]));"
                "ctx.__exit__(None,None,None);"
                "print(json.dumps(res, default=str, allow_nan=False))"
            )
            script = root / "core/pattern_edit_evaluator.py"
            out = subprocess.run(
                [sys.executable, "-B", "-c", driver, str(script), json.dumps(request)],
                cwd=str(root), capture_output=True, text=True, timeout=120)
            if out.returncode:
                tail = (out.stderr or "").strip().splitlines()[-1:] or [""]
                raise EditError(f"candidate worker failed (exit {out.returncode}): {tail[0]}")
            return json.loads(out.stdout)


def _stream_preset(service, rows):
    from core.backtest_params import settings_from_values

    settings = settings_from_values({
        "mode": "historical-stream", "market": "us", "symbols": [SYMBOL],
        "start_date": rows[30][0][:10], "end_date": rows[-1][0][:10],
        "warmup_bars": 30, "initial_capital": 100000, "position_notional": 10000,
        "txn_cost_pct": 0.001, "slippage_pct": 0.0, "end_policy": "keep-open",
    })
    return service.save_preset("ai edit preset", settings)


def _short_preset(service, rows, sessions=40):
    """Same effective settings as the smoke preset but a much shorter window."""
    from core.backtest_params import settings_from_values

    settings = settings_from_values({
        "mode": "historical-stream", "market": "us", "symbols": [SYMBOL],
        "start_date": rows[30][0][:10], "end_date": rows[30 + sessions][0][:10],
        "warmup_bars": 30, "initial_capital": 100000, "position_notional": 10000,
        "txn_cost_pct": 0.001, "slippage_pct": 0.0, "end_policy": "keep-open",
    })
    return service.save_preset("ai edit short preset", settings)


def _rows():
    from data.barcache import load as load_barcache

    candles = load_barcache("us", SYMBOL, root=BARCA)
    return [[c.timestamp.isoformat(), float(c.open), float(c.high), float(c.low),
             float(c.close), float(c.volume or 0.0)] for c in candles]


def _service(store, provider, runner=None, validator_factory=None):
    backtests = BacktestService(store, dataset_root=BARCA)
    return PatternEditService(store, provider=provider, runner=runner,
                              backtests=backtests, dataset_root=BARCA,
                              validator_factory=validator_factory)


@pytest.fixture
def sandbox_double(monkeypatch):
    """Route candidate execution through the in-process evaluator double.

    The real CandidateRunner is unavailable in this environment; production
    code keeps failing closed and this only substitutes the test double.
    """
    from core.pattern_edit_validation import Validator as RealValidator

    def factory(store=None, runner=None):
        return RealValidator(store, runner=InProcessRunner(store.root))

    monkeypatch.setattr("core.pattern_edit_validation.Validator", factory)
    return factory


def _request(service, base_version_id, **overrides):
    preset = overrides.pop("preset", None) or _stream_preset(service.backtests, _rows())
    values = {"idempotency_key": uid(), "pattern_id": PATTERN,
              "base_version_id": base_version_id,
              "instruction": "Require two closes above the neckline.",
              "preset": preset}
    values.update(overrides)
    return EditRequest(**values)


def _base_version(store) -> str:
    return next(p["active"] for p in PatternVersions(store).catalog()
                if p["id"] == PATTERN)


def test_edit_pipeline_end_to_end_with_sandbox_double(published_pattern_catalog, sandbox_double, monkeypatch):
    store = published_pattern_catalog
    provider = FakeProvider()
    service = _service(store, provider, runner=InProcessRunner(store.root))
    base = _base_version(store)
    tape = _rows()
    context = {"symbol": SYMBOL, "pattern": PATTERN, "pattern_version_id": base, "market": "us",
               "candles": [dict(zip(("time", "open", "high", "low", "close"), [r[0][:10], *r[1:5]]))
                           for r in tape[:10]],
               "manual_corrections": [{"id": "v", "kind": "vert", "time": tape[3][0][:10], "label": "breakout"}]}
    request = _request(service, base, chart_context=context)
    generated = []
    original_generate = provider.generate
    def generate(value):
        generated.append(value)
        return original_generate(value)
    monkeypatch.setattr(provider, "generate", generate)

    job = service.submit(request)
    assert job["state"] == "queued"
    saved = service.jobs.find(job["id"])["payload"]["request"]
    assert EditRequest.model_validate(saved).chart_context == request.chart_context
    assert service.submit(request)["id"] == job["id"]  # duplicate Submit is idempotent

    service.execute(job["id"])
    status = service.status(job["id"])
    assert status["state"] == "completed", status
    assert json.loads(generated[0].context[0][1])["manual_corrections"] == context["manual_corrections"]
    detail = service.detail(job["id"])
    version_id = status["generated_version_id"]
    assert version_id and version_id != base
    assert detail["explanation"] == "Tightened the entry rule."
    assert detail["diff"]["patterns/003_double_bottom.py"].count("+# AI edit") == 1
    assert detail["report"]["status"] == "passed"
    assert detail["runs"]["candidate"] and detail["runs"]["base"]

    # exactly one immutable version was created, with the base as its parent
    versions = PatternVersions(store).list_versions(PATTERN)
    assert [v["payload"]["version_number"] for v in versions] == [1, 2]
    assert store.get("versions", version_id)["parent_version_id"] == base
    # the candidate ran on the frozen preset and is comparable
    candidate = BacktestService(store, dataset_root=BARCA).result(detail["runs"]["candidate"])
    base_run = BacktestService(store, dataset_root=BARCA).result(detail["runs"]["base"])
    assert candidate["inputs"].request.preset == base_run["inputs"].request.preset
    # and the default never moved
    assert PatternVersions(store).catalog()[0]["active"] is not None
    assert _base_version(store) == base


def test_edit_pipeline_reuses_identical_backtests(published_pattern_catalog, sandbox_double):
    store = published_pattern_catalog
    provider = FakeProvider()
    service = _service(store, provider, runner=InProcessRunner(store.root))
    base = _base_version(store)

    first = service.submit(_request(service, base))
    service.execute(first["id"])
    assert service.status(first["id"])["state"] == "completed"

    second = service.submit(_request(service, base))
    service.execute(second["id"])
    assert service.status(second["id"])["state"] == "completed"
    detail = service.detail(second["id"])
    # identical generated source -> the same frozen inputs -> reused results
    assert service.backtests.status(detail["runs"]["base"]).get("state") == "completed"
    assert provider.calls == 2


def test_edit_pipeline_malformed_provider_response_creates_no_version(published_pattern_catalog):
    store = published_pattern_catalog
    provider = FakeProvider(raises=ProviderError("DeepSeek did not return a JSON object"))
    service = _service(store, provider, runner=InProcessRunner(store.root))
    base = _base_version(store)
    job = service.submit(_request(service, base))
    service.execute(job["id"])
    status = service.status(job["id"])
    assert status["state"] == "failed"
    assert status["error"]["code"] == "provider-failed"
    assert len(PatternVersions(store).list_versions(PATTERN)) == 1
    assert status["generated_version_id"] is None


def test_edit_pipeline_unavailable_provider_is_blocked(published_pattern_catalog):
    store = published_pattern_catalog
    provider = FakeProvider(raises=ProviderUnavailable("DeepSeek is not configured"))
    service = _service(store, provider, runner=InProcessRunner(store.root))
    job = service.submit(_request(service, _base_version(store)))
    service.execute(job["id"])
    status = service.status(job["id"])
    assert status["state"] == "blocked"
    assert status["error"]["code"] == "provider-failed"


def test_edit_pipeline_validation_failure_preserves_the_version(published_pattern_catalog):
    store = published_pattern_catalog
    provider = FakeProvider(source_suffix="\ndef broken(:\n")
    service = _service(store, provider, runner=InProcessRunner(store.root))
    base = _base_version(store)
    job = service.submit(_request(service, base))
    service.execute(job["id"])
    status = service.status(job["id"])
    assert status["state"] == "failed"
    assert status["error"]["code"] == "validation-failed"
    # the failed version is preserved with its diagnostics
    version_id = status["generated_version_id"]
    assert version_id
    report = service.detail(job["id"])["report"]
    assert report["status"] == "failed"
    assert any(c["name"] == "syntax" and c["outcome"] == "failed" for c in report["checks"])
    assert len(PatternVersions(store).list_versions(PATTERN)) == 2
    assert _base_version(store) == base  # defaults unchanged


def test_edit_pipeline_unavailable_sandbox_blocks_without_fallback(published_pattern_catalog):
    store = published_pattern_catalog
    provider = FakeProvider()
    service = _service(store, provider)  # real CandidateRunner: unavailable here
    job = service.submit(_request(service, _base_version(store)))
    service.execute(job["id"])
    status = service.status(job["id"])
    assert status["state"] == "blocked"
    assert status["error"]["code"] == "sandbox-unavailable"
    report = service.detail(job["id"])["report"]
    assert report is not None and report["status"] == "blocked"
    assert any(c["outcome"] == "unavailable" for c in report["checks"])
    assert len(PatternVersions(store).list_versions(PATTERN)) == 2


def test_edit_pipeline_incomplete_backtest_fails_the_job(published_pattern_catalog, sandbox_double, monkeypatch):
    """A backtest that ends failed/interrupted must not report a completed edit."""
    store = published_pattern_catalog
    provider = FakeProvider()
    service = _service(store, provider, runner=InProcessRunner(store.root))
    base = _base_version(store)
    preset = _short_preset(service.backtests, _rows())

    # The durable adapter records a non-completed run without raising.
    real_execute = service.backtests.execute
    monkeypatch.setattr(service.backtests, "execute", lambda job_id, **kwargs: None)
    job = service.submit(_request(service, base, preset=preset))
    service.execute(job["id"])
    status = service.status(job["id"])
    assert status["state"] == "failed"
    assert status["error"]["code"] == "execution-failed"
    assert status["error"]["retryable"] is True
    assert status["generated_version_id"]
    assert len(PatternVersions(store).list_versions(PATTERN)) == 2
    assert _base_version(store) == base

    # "Backtest again" recovers with no new model call or version.
    monkeypatch.setattr(service.backtests, "execute", real_execute)
    provider_calls = provider.calls
    retry = service.retry_backtest(job["id"])
    service.execute(retry["id"])
    assert service.status(retry["id"])["state"] == "completed"
    assert provider.calls == provider_calls
    assert len(PatternVersions(store).list_versions(PATTERN)) == 2


def test_edit_pipeline_cancellation_and_backtest_only_retry(published_pattern_catalog, sandbox_double):
    store = published_pattern_catalog
    provider = FakeProvider()
    service = _service(store, provider, runner=InProcessRunner(store.root))
    base = _base_version(store)
    preset = _short_preset(service.backtests, _rows(), sessions=2)

    cancelled_job = service.submit(_request(service, base, preset=preset))
    event = threading.Event()
    event.set()
    service.execute(cancelled_job["id"], cancel_event=event)
    assert service.status(cancelled_job["id"])["state"] == "cancelled"

    job = service.submit(_request(service, base, preset=preset))
    service.execute(job["id"])
    assert service.status(job["id"])["state"] == "completed"
    calls_before = provider.calls

    retry = service.retry_backtest(job["id"])
    assert retry["retry_of"] == job["id"]
    assert retry["attempt"] == 2
    assert retry["generated_version_id"] == service.status(job["id"])["generated_version_id"]
    service.cancel(retry["id"])
    retry = service.retry_backtest(retry["id"])
    assert retry["attempt"] == 3
    assert not retry["cancel_requested"]
    assert retry["error"] is None
    assert service.detail(retry["id"])["runs"] is None
    service.execute(retry["id"])
    assert service.status(retry["id"])["state"] == "completed"
    # no further provider call: the stored version was re-backtested
    assert provider.calls == calls_before
    assert len(PatternVersions(store).list_versions(PATTERN)) == 2


def test_edit_pipeline_rejects_ineligible_base_and_offline_preset(published_pattern_catalog):
    store = published_pattern_catalog
    service = _service(store, FakeProvider(), runner=InProcessRunner(store.root))
    base = _base_version(store)

    with pytest.raises(EditError, match="belong to the selected pattern"):
        service.submit(_request(service, base, pattern_id="pattern_002_double_top"))

    from core.backtest_params import settings_from_values

    offline = service.backtests.save_preset(
        "offline", settings_from_values({"mode": "offline", "symbols": [SYMBOL]}))
    with pytest.raises(ValueError, match="historical-stream"):
        EditRequest(idempotency_key=uid(), pattern_id=PATTERN,
                    base_version_id=base, instruction="x", preset=offline)


def test_edit_pipeline_pins_base_across_default_change(published_pattern_catalog, sandbox_double):
    """A default change while work is pinned never redirects the edit or its runs."""
    from core.pattern_editor_contracts import DefaultChange

    store = published_pattern_catalog
    versions = PatternVersions(store)
    service = _service(store, FakeProvider(), runner=InProcessRunner(store.root))
    base = _base_version(store)
    preset = _short_preset(service.backtests, _rows())

    first = service.submit(_request(service, base, preset=preset))
    service.execute(first["id"])
    assert service.status(first["id"])["state"] == "completed"
    generated = service.status(first["id"])["generated_version_id"]
    assert generated and generated != base

    # Another actor promotes the new version to default mid-flight.
    pattern = next(p for p in versions.catalog() if p["id"] == PATTERN)
    versions.set_default(DefaultChange(pattern_id=PATTERN, version_id=generated,
                                       expected_generation=pattern["generation"],
                                       idempotency_key=uid()))
    assert _base_version(store) == generated

    # The second edit still targets the explicitly selected base version.
    second = service.submit(_request(service, base, preset=preset))
    service.execute(second["id"])
    status = service.status(second["id"])
    assert status["state"] == "completed", status
    detail = service.detail(second["id"])
    assert store.get("versions", status["generated_version_id"])["parent_version_id"] == base
    base_run = BacktestService(store, dataset_root=BARCA).result(detail["runs"]["base"])
    assert base_run["inputs"].request.versions[0].version_id == base
    assert _base_version(store) == generated  # the default was never auto-promoted


def test_edit_pipeline_missing_data_fails_before_generation(published_pattern_catalog, tmp_path):
    store = published_pattern_catalog
    provider = FakeProvider()
    service = PatternEditService(
        store, provider=provider, runner=InProcessRunner(store.root),
        backtests=BacktestService(store, dataset_root=tmp_path,
                                  fetch_missing_history=False),
        dataset_root=tmp_path)

    with pytest.raises(EditError, match="frozen daily history"):
        service.submit(_request(service, _base_version(store)))
    # nothing was generated, versioned, or queued
    assert provider.calls == 0
    assert len(PatternVersions(store).list_versions(PATTERN)) == 1


def test_edit_pipeline_zero_trades_is_a_completed_run(published_pattern_catalog, sandbox_double):
    """A candidate that never triggers is a success, not a failure."""
    store = published_pattern_catalog
    versions = PatternVersions(store)
    pattern = "pattern_002_double_top"
    base = next(p["active"] for p in versions.catalog() if p["id"] == pattern)
    service = _service(store, FakeProvider(), runner=InProcessRunner(store.root))
    preset = _short_preset(service.backtests, _rows())

    job = service.submit(_request(service, base, pattern_id=pattern, preset=preset))
    service.execute(job["id"])
    status = service.status(job["id"])
    assert status["state"] == "completed", status
    result = BacktestService(store, dataset_root=BARCA).result(service.detail(job["id"])["runs"]["candidate"])
    assert result["result"].metrics.trade_count == 0
    assert result["trades"]["trades"] == []


def test_edit_pipeline_backtest_failure_preserves_version_and_retries(
        published_pattern_catalog, sandbox_double, monkeypatch):
    store = published_pattern_catalog
    provider = FakeProvider()
    service = _service(store, provider, runner=InProcessRunner(store.root))
    base = _base_version(store)
    preset = _short_preset(service.backtests, _rows())

    calls = {"n": 0}
    real_execute = service.backtests.execute

    def flaky(job_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EditError("simulated execution failure")
        return real_execute(job_id, **kwargs)

    monkeypatch.setattr(service.backtests, "execute", flaky)
    job = service.submit(_request(service, base, preset=preset))
    service.execute(job["id"])
    status = service.status(job["id"])
    assert status["state"] == "failed"
    assert status["generated_version_id"]
    assert len(PatternVersions(store).list_versions(PATTERN)) == 2

    # Retrying the backtest uses the persisted version and never calls the model.
    provider_calls = provider.calls
    retry = service.retry_backtest(job["id"])
    service.execute(retry["id"])
    assert service.status(retry["id"])["state"] == "completed"
    assert provider.calls == provider_calls
    assert len(PatternVersions(store).list_versions(PATTERN)) == 2
