"""URL generation, causal rule compilation and durable import lifecycle checks."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from ai.providers.experiment_generation import ExperimentGenerator, GeneratedExperiment
from core.experiments import rules
from core.experiments.catalog import catalog
from core.experiments.contracts import BatchRequest
from core.experiments.imports import SourceImportService
from core.experiments.service import ExperimentService
from core.experiments.source_store import SourceStore
from core.experiments.source_urls import canonical_video_url, check_public_fetch
from core.experiments.store import ExperimentStore
from core.test_experiments import bars

URL = "https://www.youtube.com/watch?v=experiment-test-video"


def specification() -> dict:
    return {"version":1, "timeframe":"daily", "execution":"next-session-open",
            "sizing":"independent-fixed-notional", "markets":["us", "ph"], "symbols":[],
            "features":[{"name":"fast", "kind":"sma", "price":"close", "period":2, "lag":0},
                        {"name":"slow", "kind":"sma", "price":"close", "period":3, "lag":0}],
            "variants":[{"name":"two-three", "side":"long",
                         "entry":{"op":"cross_above", "left":"fast", "right":"slow"},
                         "exit":{"op":"cross_below", "left":"fast", "right":"slow"},
                         "evidence":"At 2 seconds the source defines the SMA2/3 cross-up entry and cross-down exit."}],
            "source_evidence":"At 1–3 seconds the source explicitly uses daily equity candles and SMA2/SMA3 closes."}


def generated(**changes) -> GeneratedExperiment:
    writeup = "\n\n".join(f"## {heading}\n\n" +
                          "The source explicitly compares completed daily SMA2 and SMA3 values, "
                          "enters on an upward cross and exits on a downward cross. " * 4
                          for heading in ("What it does", "How to reproduce it", "Exact settings and source evidence",
                                          "Missing rules and limitations", "Evaluation"))
    plan = "\n\n".join(f"## {heading}\n\n" +
                       "Freeze the daily close rules, source evidence and history before causal next-open replay. " * 4
                       for heading in ("Experiment overview", "Rules and inputs", "Proposed integration",
                                       "Data handling", "Results and validation", "Gaps before implementation"))
    return GeneratedExperiment.model_validate({"title":"Daily moving-average experiment", "overview":"A complete daily-equity SMA crossover source example.",
                 "writeup":writeup, "plan":plan, "is_trading":True, "source_complete":True,
                 "source_timeframe":"daily", "missing_rules":[], "unsupported_requirements":[],
                 "contradictions":[], "rule_spec":specification(), "frame_requests":[], **changes})


def preparer(directory, url, cancelled, extra=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "frames").mkdir(exist_ok=True)
    (directory / "video.mp4").write_bytes(b"test-only media; decoder is replaced by the preparation fixture")
    (directory / "video.description").write_text("Daily equities. SMA2/3 close cross-up entry, cross-down exit.")
    (directory / "transcript.txt").write_text("[1–3 s] Daily equity close SMA2/3 crosses.")
    (directory / "frames/sample.jpg").write_bytes(b"test-only image")
    (directory / "frames/contact-sheet.jpg").write_bytes(b"test-only image")
    evidence = {"url":url, "title":"Daily crossover", "duration":10.0,
                "media":{"file":"video.mp4", "bytes":(directory / "video.mp4").stat().st_size,
                         "sha256":hashlib.sha256((directory / "video.mp4").read_bytes()).hexdigest()},
                "description":(directory / "video.description").read_text(),
                "transcript":{"segments":[{"start":1, "end":3, "text":"Daily SMA2/3"}]},
                "frames":[{"file":"frames/sample.jpg", "review_file":"frames/sample.jpg", "seconds":2.0}]}
    (directory / "evidence.json").write_text(json.dumps(evidence))


class Generator:
    def __init__(self, result=None):
        self.result = result or generated()
        self.calls = 0

    def available(self):
        return True

    def generate(self, directory, evidence):
        self.calls += 1
        return self.result, {"requested_model":"test-model", "returned_model":"test-model"}


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/video", "https://youtube.com.evil.test/watch?v=x",
    "https://user:secret@youtube.com/watch?v=x", "https://youtube.com:8080/watch?v=x", "https://youtube.com/playlist?list=x",
    "https://youtube.com", "https://facebook.com/a\nInjected", None, "https://[::1]/video"])
def test_reject_nonvideo_or_nonpublic_submission(url):
    with pytest.raises(ValueError):
        canonical_video_url(url)


def test_canonicalization_preserves_identity_and_deduplicates_tracking():
    assert canonical_video_url(URL + "&si=tracking#t=2") == canonical_video_url(URL)
    assert canonical_video_url("https://m.facebook.com/watch/?v=123&fbclid=tracking") == "https://facebook.com/watch?v=123"


def test_extractor_fetch_rejects_private_ipv4_and_ipv6(monkeypatch):
    for host in ("127.0.0.1", "169.254.169.254", "10.2.3.4", "::1"):
        monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (host, 443))])
        with pytest.raises(ValueError, match="public"):
            check_public_fetch("https://video.example/media.mp4")


def test_rule_values_crosses_and_prefix_invariance():
    close = [5,4,3,2,1,2,3,4,5,4,3,2]
    frame = pd.DataFrame({key:close for key in ("open", "high", "low", "close", "volume")})
    variant = rules.variants(specification(), frame)[0]
    assert np.flatnonzero(variant.entry).tolist() == [6]
    assert np.flatnonzero(variant.exit).tolist() == [10]
    assert variant.evidence(6)["fast"] == 2.5
    assert variant.evidence(6)["slow"] == 2
    altered = frame.copy()
    altered.iloc[8:] = 5000
    prefix = rules.variants(specification(), altered)[0]
    np.testing.assert_array_equal(prefix.entry[:8], variant.entry[:8])
    np.testing.assert_array_equal(prefix.exit[:8], variant.exit[:8])
    truncated = rules.variants(specification(), frame.iloc[:8])[0]
    np.testing.assert_array_equal(truncated.entry, variant.entry[:8])


def test_prior_range_lag_excludes_the_signal_bar():
    spec = specification()
    spec["features"] = [{"name":"prior_high", "kind":"highest", "price":"high", "period":3, "lag":1}]
    spec["variants"][0]["entry"] = {"op":"above", "left":"close", "right":"prior_high"}
    spec["variants"][0]["exit"] = {"op":"below", "left":"close", "right":"prior_high"}
    close = [2,3,4,5,6,7]
    frame = pd.DataFrame({k:close for k in ("open", "high", "low", "close", "volume")})
    variant = rules.variants(spec, frame)[0]
    assert variant.evidence(4)["prior_high"] == 5
    assert variant.entry[4] == 1


@pytest.mark.parametrize("change", ["future", "code", "unknown", "atr", "intraday", "stop", "duplicate"])
def test_rule_compiler_rejects_unsupported_or_executable_rules(change):
    spec = specification()
    if change == "future": spec["features"][0]["lag"] = -1
    elif change == "code": spec["variants"][0]["entry"]["left"] = "__import__('os').system('anything')"
    elif change == "unknown": spec["features"][0]["kind"] = "proprietary"
    elif change == "atr": spec["features"][0].update(kind="atr", price="volume")
    elif change == "intraday": spec["timeframe"] = "5m"
    elif change == "stop": spec["stop_loss"] = .02
    elif change == "duplicate": spec["features"][0]["name"] = "close"
    with pytest.raises(ValidationError):
        rules.RuleSpecification.model_validate(spec)


def test_generated_source_survives_restart_deduplicates_and_runs_only_selected(tmp_path):
    store = ExperimentStore(tmp_path)
    generator = Generator()
    imports = SourceImportService(store, generator=generator, preparer=preparer)
    job = imports.submit(URL, background=False)
    assert job["state"] == "completed" and job["source_id"] == job["id"]
    assert {"writeup.md", "33ai-integration.md", "source.json", "rules.json", "evidence.json"} <= set(job["files"])
    repeated = imports.submit(URL + "&si=another", background=False)
    assert repeated["id"] == job["id"] and generator.calls == 1
    service = ExperimentService(ExperimentStore(tmp_path), enumerate_symbols=lambda *_:["SPY"], loader=lambda *_:bars())
    source = service.source(job["source_id"])
    assert len(service.catalog()) == 450
    assert next(s for s in service.catalog() if s["id"] == source["id"])["availability"] == "ready"
    request = BatchRequest(start_date="2021-06-01", end_date="2021-11-30", source_ids=[source["id"]],
                           position_notional=1000, idempotency_key="generated-one")
    batch = service.submit(request, background=False)
    assert batch["state"] == "completed"
    assert [r["source"] for r in batch["runs"]] == [source["id"]]
    assert batch["runs"][0]["summary"]["trade_count"] > 0
    rows = store.evidence(batch["id"], source["id"], "trades")["rows"]
    assert all(r["entry_date"] > r["signal_date"] for r in rows)
    assert batch["sources"][0]["rule_spec_sha256"] == source["rule_spec_sha256"]
    with pytest.raises(ValueError, match="Unknown selected"):
        service.submit(request.model_copy(update={"source_ids":["missing"], "idempotency_key":"bad-selection"}))


@pytest.mark.parametrize("changes", [
    {"missing_rules":["Exit timing remains unknown"]}, {"unsupported_requirements":["Requires a 5m volume profile"]},
    {"contradictions":["Caption and input disagree"]}, {"source_timeframe":"unknown"},
    {"source_complete":False}, {"is_trading":False}, {"rule_spec":None}])
def test_incomplete_source_generates_documents_without_fake_runnable_rules(tmp_path, changes):
    service = SourceImportService(ExperimentStore(tmp_path), generator=Generator(generated(**changes)), preparer=preparer)
    job = service.submit(URL, background=False)
    assert job["state"] == "completed"
    source = service.jobs.sources()[0]
    assert source["availability"] == "blocked" and source["rule_spec"] is None
    assert source["blocker"] and "Runnable replay is blocked" in source["plan"]


def test_cancellation_prevents_publish_and_retry_fences_old_worker(tmp_path):
    store = ExperimentStore(tmp_path)
    jobs = SourceStore(store)
    job, _ = jobs.create(canonical_video_url(URL))
    attempt = jobs.claim(job["id"])
    jobs.cancel(job["id"])
    jobs.retry(job["id"])
    new_attempt = jobs.claim(job["id"])
    assert new_attempt == attempt + 1
    assert not jobs.heartbeat(job["id"], attempt)
    assert not jobs.update(job["id"], attempt=attempt, state="failed", stage="failed", message="old worker")
    assert not jobs.finish(job["id"], {"id":"wrong"}, attempt=attempt)
    assert jobs.heartbeat(job["id"], new_attempt)
    assert jobs.get(job["id"])["state"] == "preparing"


def test_cancel_during_generation_retains_evidence_without_publishing(tmp_path):
    service = SourceImportService(ExperimentStore(tmp_path), preparer=preparer)
    class Cancelling(Generator):
        def generate(self, directory, evidence):
            service.jobs.cancel(service.jobs.listing()[0]["id"])
            return super().generate(directory, evidence)
    service._generator = Cancelling()
    job = service.submit(URL, background=False)
    assert job["state"] == "cancelled" and "evidence.json" in job["files"]
    assert not service.jobs.sources()
    service._generator = Generator()
    assert service.retry(job["id"], background=False)["state"] == "completed"


def test_additional_frame_pass_is_bounded_and_both_requests_are_recorded(tmp_path):
    prepared = []
    def extra_preparer(directory, url, cancelled, extra=None):
        prepared.append(extra)
        if extra is None:
            preparer(directory, url, cancelled)
        else:
            evidence = json.loads((directory / "evidence.json").read_text())
            evidence["frames"].append({**evidence["frames"][0], "seconds":extra[0]})
            (directory / "evidence.json").write_text(json.dumps(evidence))
    class TwoPass(Generator):
        def generate(self, directory, evidence):
            self.calls += 1
            output = generated(frame_requests=[3.0], source_complete=False, rule_spec=None) if self.calls == 1 else generated()
            return output, {"request_id":str(self.calls)}
    generator = TwoPass()
    imports = SourceImportService(ExperimentStore(tmp_path), generator=generator, preparer=extra_preparer)
    assert imports.submit(URL, background=False)["state"] == "completed"
    assert generator.calls == 2 and prepared == [None, [3.0]]
    source = imports.jobs.sources()[0]
    assert source["availability"] == "ready"
    assert [p["request_id"] for p in source["generation"]["passes"]] == ["1", "2"]


def test_expired_job_requires_explicit_retry(tmp_path):
    jobs = SourceStore(ExperimentStore(tmp_path))
    job, _ = jobs.create(canonical_video_url(URL))
    with jobs.store.connect() as con:
        con.execute("UPDATE source_imports SET lease_until=0 WHERE id=?", (job["id"],))
    assert jobs.get(job["id"])["state"] == "interrupted"
    assert jobs.retry(job["id"])["attempt"] == 2


def test_generated_document_and_rule_tampering_blocks_catalog(tmp_path):
    service = SourceImportService(ExperimentStore(tmp_path), generator=Generator(), preparer=preparer)
    service.submit(URL, background=False)
    source = service.jobs.sources()[0]
    changed = deepcopy(source)
    changed["rule_spec"]["features"][0]["period"] = 99
    assert catalog((changed,))[-1]["availability"] == "blocked"
    changed = {**source, "writeup":"tampered"}
    assert catalog((changed,))[-1]["availability"] == "blocked"
    changed = {**source, "adapter_version":"a-different-compiler"}
    assert catalog((changed,))[-1]["availability"] == "blocked"


def test_artifact_paths_cannot_escape_import_directory(tmp_path):
    service = SourceImportService(ExperimentStore(tmp_path), generator=Generator(), preparer=preparer)
    job = service.submit(URL, background=False)
    private = tmp_path / "outside.txt"
    private.write_text("private")
    (service.jobs.directory(job["id"]) / "escape.txt").symlink_to(private)
    for name in ("../../outside.txt", "escape.txt", "/etc/passwd"):
        with pytest.raises(KeyError):
            service.artifact(job["id"], name)


def test_multimodal_provider_uses_complete_caption_transcript_and_local_images(tmp_path):
    preparer(tmp_path, URL, None)
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    seen = []
    response = SimpleNamespace(stop_reason="end_turn", id="test-call", model="test-model",
        usage=SimpleNamespace(model_dump=lambda:{"input_tokens":100}),
        content=[SimpleNamespace(type="text", text=generated().model_dump_json())])
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **request: seen.append(request) or response))
    result, provenance = ExperimentGenerator(client=client, model="test-model").generate(tmp_path, evidence)
    assert result.rule_spec and provenance["request_id"] == "test-call"
    content = seen[0]["messages"][0]["content"]
    assert any(block["type"] == "image" for block in content)
    assert evidence["description"] in content[0]["text"]
    assert "UNTRUSTED SOURCE" in seen[0]["system"]
    response.stop_reason = "max_tokens"
    with pytest.raises(ValueError, match="complete"):
        ExperimentGenerator(client=client).generate(tmp_path, evidence)


def test_explicit_provider_selection_and_rejecting_text_only_models(monkeypatch):
    from config import settings
    from unittest.mock import patch
    selected = settings.model_copy(update={"experiments_generation_provider":"deepseek",
        "experiments_generation_model":"", "deepseek_api_key":"test-only-key",
        "deepseek_base_url":"https://api.deepseek.com", "deepseek_model":"deepseek-flash"})
    with patch("config.settings", selected):
        generator = ExperimentGenerator()
        assert generator.available() and generator.model == "deepseek-flash"
        assert generator.base_url == "https://api.deepseek.com/anthropic"
        assert not ExperimentGenerator(model="deepseek-v4-pro").available()
        assert "does not process" in ExperimentGenerator(model="deepseek-v4-pro").unavailable_reason()
    selected = selected.model_copy(update={"experiments_generation_provider":"anthropic", "anthropic_api_key":"other-test-key"})
    with patch("config.settings", selected):
        generator = ExperimentGenerator()
        assert generator.available() and generator.base_url == "https://api.anthropic.com"
        assert generator.model == "claude-opus-4-6"


def test_real_sdk_sends_only_the_selected_providers_api_key(tmp_path, monkeypatch):
    import anthropic
    from anthropic import _base_client
    from config import settings
    from unittest.mock import patch
    preparer(tmp_path, URL, None)
    seen = []
    http = getattr(_base_client, "httpx", None) or _base_client.httpx2
    def respond(request):
        seen.append(request)
        return http.Response(200, json={"id":"test-sdk-call", "type":"message", "role":"assistant",
            "model":"deepseek-flash", "content":[{"type":"text", "text":generated().model_dump_json()}],
            "stop_reason":"end_turn", "stop_sequence":None, "usage":{"input_tokens":100,"output_tokens":100}})
    constructor = anthropic.Anthropic
    def create(**kwargs):
        return constructor(**kwargs, http_client=http.Client(transport=http.MockTransport(respond)))
    monkeypatch.setattr(anthropic, "Anthropic", create)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unrelated-test-token")
    selected = settings.model_copy(update={"experiments_generation_provider":"deepseek",
        "experiments_generation_model":"deepseek-flash", "deepseek_api_key":"selected-test-key",
        "deepseek_base_url":"https://api.deepseek.com"})
    with patch("config.settings", selected):
        output, _ = ExperimentGenerator().generate(tmp_path, json.loads((tmp_path / "evidence.json").read_text()))
    assert output.source_complete
    assert seen[0].headers["x-api-key"] == "selected-test-key"
    assert "authorization" not in seen[0].headers


def test_source_video_stays_inside_the_archive_or_import_store(tmp_path):
    service = ExperimentService(ExperimentStore(tmp_path))
    assert service.locate_video({"id": "../video"}) is None
    assert service.locate_video({"id": "a/b"}) is None
    assert service.locate_video({"id": ""}) is None
    assert service.video_url({"id": "missing-folder"}) is None
    folder = tmp_path / "sources" / "url-safe"
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"0123456789")
    found = service.locate_video({"id": "url-safe"})
    assert found == (folder / "video.mp4").resolve()
    assert service.video_url({"id": "url-safe"}) == "/api/experiments/sources/url-safe/video"
    with pytest.raises(KeyError):
        service.source_video("no-such-source")
