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
