"""HTTP client for the remote pattern registry (GET /api/patterns).

A client with no local editor database reads pattern versions from the VPS
API. The transport conventions mirror ``data/history_client.py`` so a scan does
not open a TLS session per pattern.

Failures are raised as editor errors rather than returned as ``None``: a
registry read is never optional. Transport failures become
``DatabaseUnavailable`` so the scanner's existing retry treats a flaky API
exactly like a flaky database.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import quote

from config import settings
from core.pattern_editor_db import DatabaseUnavailable, EditError
from utils.logger import log

# Same budgets as the history client: connect includes TLS.
_CONNECT_TIMEOUT = 20.0
_READ_TIMEOUT = 30.0
_MAX_INFLIGHT = 4
_RETRIES = 3
_cached_client = None
_cached_key: tuple[str, str, str] | None = None
_client_lock = threading.Lock()
_request_sema = threading.BoundedSemaphore(_MAX_INFLIGHT)


def _base_url() -> str:
    return (settings.pattern_api_url or "").strip().rstrip("/")


def pattern_api_configured() -> bool:
    """True when the registry lives behind the API instead of local PostgreSQL."""
    return bool(_base_url())


def pattern_api_origin() -> str:
    """Base URL of the registry API, without a trailing slash."""
    return _base_url()


def _timeout():
    import httpx

    return httpx.Timeout(
        connect=_CONNECT_TIMEOUT,
        read=_READ_TIMEOUT,
        write=_CONNECT_TIMEOUT,
        pool=_CONNECT_TIMEOUT,
    )


def _reset_client() -> None:
    global _cached_client, _cached_key
    if _cached_client is not None:
        try:
            _cached_client.close()
        except Exception:
            pass
    _cached_client = None
    _cached_key = None


def _client():
    global _cached_client, _cached_key
    import httpx

    user, password = settings.pattern_api_auth
    if user and not password:
        log.warning(
            "Pattern API | no PATTERN_API_PASSWORD set — /api/patterns requires "
            "the serving host's WEB_UI_PASSWORD"
        )
    key = (_base_url(), user, password)
    if _cached_client is not None and _cached_key == key:
        return _cached_client
    _reset_client()
    _cached_client = httpx.Client(
        base_url=key[0],
        auth=(user, password) if user or password else None,
        timeout=_timeout(),
        headers={"Accept": "application/json", "Accept-Encoding": "gzip"},
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    )
    _cached_key = key
    return _cached_client


def _is_transport_error(exc: BaseException) -> bool:
    import httpx

    return isinstance(exc, (
        httpx.ConnectTimeout,
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.RemoteProtocolError,
    ))


def _request(method: str, path: str, params: dict[str, Any] | None = None,
             json_body: dict | None = None):
    """One request with bounded retries; transport exhaustion surfaces as an outage.

    Writes are retried like reads because the registry resolves a repeated
    idempotency key to the record it already holds.
    """
    last_exc: BaseException | None = None
    for attempt in range(_RETRIES):
        _request_sema.acquire()
        try:
            with _client_lock:
                client = _client()
            return client.request(method, path, params=params or {}, json=json_body)
        except Exception as exc:
            last_exc = exc
            if not (_is_transport_error(exc) and attempt + 1 < _RETRIES):
                raise
        finally:
            _request_sema.release()
        # Back off after releasing the slot: sleeping while holding it would
        # stall the other in-flight readers during a timeout storm.
        time.sleep(0.4 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _detail(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        return str(body.get("detail") or "")
    return ""


def _call_json(method: str, path: str, params: dict[str, Any] | None = None,
               json_body: dict | None = None):
    try:
        resp = _request(method, path, params, json_body)
    except Exception as exc:
        if _is_transport_error(exc):
            log.warning(
                f"Pattern API | {path} failed: {type(exc).__name__}: {exc}"
            )
            raise DatabaseUnavailable(
                "Pattern registry API unreachable; check PATTERN_API_URL and the server"
            ) from None
        log.exception(f"Pattern API | {path} failed")
        raise
    if resp.status_code == 200:
        return resp.json()
    detail = _detail(resp)
    if resp.status_code in (401, 403):
        raise EditError(
            "Pattern API rejected the credentials; set PATTERN_API_USERNAME / "
            "PATTERN_API_PASSWORD to the serving host's WEB_UI credentials"
        )
    if resp.status_code == 503:
        raise DatabaseUnavailable(detail or "Pattern registry unavailable")
    raise EditError(detail or f"Pattern API {path} failed ({resp.status_code})")


def _pinned_params(disabled: tuple[str, ...], selected: dict[str, str] | None):
    params: dict[str, Any] = {}
    if disabled:
        params["disabled"] = list(disabled)
    if selected:
        params["selected"] = [f"{pattern}:{version}"
                              for pattern, version in sorted(selected.items())]
    return params or None


def fetch_pinned(disabled: tuple[str, ...] = (),
                 selected: dict[str, str] | None = None) -> dict[str, str]:
    """The eligibility-checked version set, decided by the serving host."""
    data = _call_json("GET", "/api/patterns/pinned", _pinned_params(disabled, selected))
    versions = data.get("versions") if isinstance(data, dict) else None
    if not isinstance(versions, dict):
        raise EditError("Pattern API returned no pinned version set")
    return versions


def fetch_bundle(disabled: tuple[str, ...] = (),
                 selected: dict[str, str] | None = None) -> dict:
    """Pinned set plus every pinned version's payload and file bytes (base64)."""
    data = _call_json("GET", "/api/patterns/bundle", _pinned_params(disabled, selected))
    if not isinstance(data, dict) or not isinstance(data.get("bundles"), dict):
        raise EditError("Pattern API returned no pattern bundle")
    return data


def fetch_version_bundle(version_id: str) -> dict:
    """Execution material for one version, for refreshing a single pattern."""
    data = _call_json("GET", f"/api/patterns/versions/{quote(version_id, safe='')}/bundle")
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise EditError("Pattern API returned no version bundle")
    return data


def upload_run(payload: dict) -> dict:
    """Record a run executed here on the host that owns the registry.

    Safe to retry: the payload carries an idempotency key, and the host
    resolves a repeated key to the run it already recorded.
    """
    data = _call_json("POST", "/api/patterns/runs", json_body=payload)
    if not isinstance(data, dict) or not data.get("run_id"):
        raise EditError("Pattern API did not record the run")
    return data


def fetch_run(run_id: str) -> dict:
    """Status and result of a run recorded on the owning host."""
    return _call_json("GET", f"/api/backtest/runs/{quote(run_id, safe='')}")


class RemotePatternEditor:
    """Desktop editor calls the same endpoints as the web Patterns page."""

    def catalog(self):
        return _call_json("GET", "/api/patterns")["patterns"]

    def versions_for(self, pattern_id, *, include_archived=False):
        return _call_json("GET", f"/api/patterns/{quote(pattern_id, safe='')}/versions",
                          {"include_archived": str(include_archived).lower()})["versions"]

    def version_detail(self, version_id):
        return _call_json("GET", f"/api/patterns/versions/{quote(version_id, safe='')}")

    def source(self, version_id):
        return _call_json("GET", f"/api/patterns/versions/{quote(version_id, safe='')}/source")

    def diff(self, version_id):
        return _call_json("GET", f"/api/patterns/versions/{quote(version_id, safe='')}/diff")

    def presets(self):
        return _call_json("GET", "/api/backtest/presets")["presets"]

    def balance(self):
        return _call_json("GET", "/api/patterns/provider")

    def submit_from_values(self, values, **kwargs):
        from core.pattern_edit_store import uid

        body = {**kwargs, "settings": values,
                "idempotency_key": kwargs.get("idempotency_key") or f"edit-{uid()}"}
        job = _call_json("POST", "/api/patterns/edits", json_body=body)
        return {**job, "id": job["job_id"]}

    def job_detail(self, job_id):
        return _call_json("GET", f"/api/patterns/edits/{quote(job_id, safe='')}")

    def cancel_job(self, job_id):
        return _call_json("POST", f"/api/patterns/edits/{quote(job_id, safe='')}/cancel")

    def backtest_version(self, version_id, values):
        from core.pattern_edit_store import uid

        return _call_json("POST", f"/api/patterns/versions/{quote(version_id, safe='')}/backtest",
                          json_body={**values, "idempotency_key": f"backtest-{uid()}"})

    def run_payload(self, run_id):
        return _call_json("GET", f"/api/patterns/runs/{quote(run_id, safe='')}")

    def run_chart(self, run_id, detection):
        return _call_json("GET", f"/api/patterns/runs/{quote(run_id, safe='')}/chart",
                          {"detection": detection})

    def set_default(self, *, pattern_id, version_id, **values):
        from core.pattern_edit_store import uid

        return _call_json("POST", f"/api/patterns/versions/{quote(version_id, safe='')}/default",
                          json_body={**values, "idempotency_key": f"default-{uid()}"})

    def archive(self, *, pattern_id, version_id, **values):
        from core.pattern_edit_store import uid

        return _call_json("POST", f"/api/patterns/versions/{quote(version_id, safe='')}/archive",
                          json_body={**values, "idempotency_key": f"archive-{uid()}"})
