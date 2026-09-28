"""Pattern API client: transport mapping, retries and credential failures."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from core.pattern_editor_db import DatabaseUnavailable, EditError


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, params=None, json=None):
        self.calls.append((path, params))
        self.methods = getattr(self, "methods", []) + [(method, path, json)]
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _call(func, responses, **kwargs):
    client = _Client(responses)
    with patch("data.pattern_client._client", return_value=client), \
         patch("data.pattern_client.time.sleep"):
        result = func(**kwargs)
    return result, client


def test_pinned_returns_the_version_set():
    from data.pattern_client import fetch_pinned

    versions, client = _call(fetch_pinned, [_Resp(payload={"versions": {"a": "v1"}})])
    assert versions == {"a": "v1"}
    assert client.calls[0][0] == "/api/patterns/pinned"


def test_disabled_and_selected_are_encoded_as_repeated_params():
    from data.pattern_client import fetch_pinned

    _, client = _call(fetch_pinned, [_Resp(payload={"versions": {}})],
                      disabled=("p1", "p2"), selected={"p3": "v9"})
    params = client.calls[0][1]
    assert params["disabled"] == ["p1", "p2"]
    assert params["selected"] == ["p3:v9"]


def test_transport_failure_becomes_database_unavailable():
    """A flaky API must look like a flaky database so the scanner retries."""
    from data.pattern_client import fetch_version_bundle

    with pytest.raises(DatabaseUnavailable):
        _call(fetch_version_bundle, [httpx.ConnectError("refused")] * 3,
              version_id="v1")


def test_retry_recovers_from_a_connect_timeout():
    from data.pattern_client import fetch_version_bundle

    bundle, client = _call(
        fetch_version_bundle,
        [httpx.ConnectTimeout("timed out"), _Resp(payload={"files": {}})],
        version_id="v1")
    assert bundle == {"files": {}}
    assert len(client.calls) == 2


def test_rejected_credentials_name_the_setting():
    from data.pattern_client import fetch_pinned

    with pytest.raises(EditError) as failure:
        _call(fetch_pinned, [_Resp(status_code=401)])
    assert "PATTERN_API" in str(failure.value)


def test_server_side_outage_is_unavailable_not_a_bad_request():
    from data.pattern_client import fetch_pinned

    with pytest.raises(DatabaseUnavailable):
        _call(fetch_pinned, [_Resp(status_code=503,
                                   payload={"detail": "PostgreSQL unavailable"})])


def test_error_detail_is_surfaced_to_the_caller():
    from data.pattern_client import fetch_pinned

    with pytest.raises(EditError, match="Pattern is unavailable"):
        _call(fetch_pinned, [_Resp(status_code=400,
                                   payload={"detail": "Pattern is unavailable: p1"})])


def test_missing_pinned_set_is_an_error_not_an_empty_result():
    from data.pattern_client import fetch_pinned

    with pytest.raises(EditError):
        _call(fetch_pinned, [_Resp(payload={"unexpected": True})])


def test_upload_run_posts_the_payload_and_returns_the_record():
    from data.pattern_client import upload_run

    recorded, client = _call(upload_run, [_Resp(payload={"run_id": "run-1",
                                                        "state": "completed"})],
                             payload={"run_id": "run-1", "artifacts": {}})
    assert recorded == {"run_id": "run-1", "state": "completed"}
    method, path, body = client.methods[0]
    assert (method, path) == ("POST", "/api/patterns/runs")
    assert body == {"run_id": "run-1", "artifacts": {}}


def test_upload_run_requires_a_recorded_run():
    from data.pattern_client import upload_run

    with pytest.raises(EditError, match="did not record"):
        _call(upload_run, [_Resp(payload={"unexpected": True})], payload={})


def test_fetch_run_reads_the_host_record():
    from data.pattern_client import fetch_run

    payload, client = _call(fetch_run, [_Resp(payload={"run_id": "run-1",
                                                       "state": "completed"})],
                            run_id="run-1")
    assert payload["state"] == "completed"
    assert client.calls[0][0] == "/api/backtest/runs/run-1"


def test_pattern_api_auth_requires_an_explicit_password():
    """The dashboard's own password belongs to the local host, not the server."""
    from config import Settings

    s = Settings.model_construct(
        web_ui_username="admin", web_ui_password="local-secret",
        stocks_history_username="", pattern_api_username="admin",
        pattern_api_password="")
    assert s.pattern_api_auth == ("admin", "")


def test_remote_desktop_editor_uses_existing_web_endpoints(monkeypatch):
    from data import pattern_client
    from ui.patterns_dialog import PatternsDialog

    calls = []
    def call(method, path, params=None, json_body=None):
        calls.append((method, path, params, json_body))
        if path == "/api/patterns":
            return {"patterns": [{"pattern_id": "pattern_003_double_bottom"}]}
        if path.endswith("/versions"):
            return {"versions": [{"version_id": "v1"}]}
        if path.endswith("/presets"):
            return {"presets": []}
        if path.endswith("/edits"):
            return {"job_id": "job1", "state": "queued"}
        return {"run_id": "run1"}
    monkeypatch.setattr(pattern_client, "_call_json", call)
    monkeypatch.setattr("core.remote_pattern_store.remote_patterns_enabled", lambda: True)
    dialog = PatternsDialog.__new__(PatternsDialog)
    dialog._editor = None
    editor = dialog.editor()
    assert isinstance(editor, pattern_client.RemotePatternEditor)
    assert editor.catalog()[0]["pattern_id"] == "pattern_003_double_bottom"
    assert editor.versions_for("pattern_003_double_bottom")[0]["version_id"] == "v1"
    assert editor.presets() == []
    context = {"symbol": "SM", "candles": [{"time": "2026-01-01"}]}
    job = editor.submit_from_values(
        {"start_date": "2026-01-01", "end_date": "2026-02-01"},
        pattern_id="pattern_003_double_bottom", base_version_id="v1",
        instruction="Require a higher second bottom", chart_context=context)
    assert job["id"] == "job1"
    assert calls[-1][3]["chart_context"] == context
    assert calls[-1][3]["idempotency_key"]
    assert editor.backtest_version("v1", {"start_date": "2026-01-01"})["run_id"] == "run1"
    assert calls[-1][1] == "/api/patterns/versions/v1/backtest"
    editor.run_chart("run1", 2)
    assert calls[-1][:3] == ("GET", "/api/patterns/runs/run1/chart", {"detection": 2})
    editor.set_default(pattern_id="p", version_id="v1", expected_generation=2)
    assert calls[-1][1].endswith("/v1/default") and calls[-1][3]["expected_generation"] == 2
    editor.archive(pattern_id="p", version_id="v1", expected_generation=3)
    assert calls[-1][1].endswith("/v1/archive") and calls[-1][3]["idempotency_key"]
