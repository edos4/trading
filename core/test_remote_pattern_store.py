"""Remote pattern registry: integrity, caching and fail-closed writes.

These run with no database at all -- which is the whole point of the store.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from core.pattern_edit_store import EditError
from core.pattern_versions import PatternVersions
from core.remote_pattern_store import (
    RemotePatternStore, clear_bundle_cache, remote_patterns_enabled,
)


@pytest.fixture(autouse=True)
def _fresh_bundle_cache():
    """Bundles are cached process-wide; no test may inherit another's."""
    clear_bundle_cache()
    yield
    clear_bundle_cache()


def _ref(data: bytes) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data), "media_type": "text/plain"}


def _bundle(version_id: str = "v1", files: dict[str, bytes] | None = None) -> dict:
    files = files if files is not None else {"patterns/detector.py": b"VALUE = 1\n"}
    return {
        "payload": {"version_id": version_id,
                    "files": {name: _ref(data) for name, data in files.items()}},
        "files": {name: base64.b64encode(data).decode("ascii")
                  for name, data in files.items()},
        "content_sha256": None,
    }


def _store(monkeypatch, bundle: dict, calls: list | None = None) -> RemotePatternStore:
    import data.pattern_client as client

    calls = calls if calls is not None else []

    def fetch(version_id):
        calls.append(version_id)
        return bundle

    monkeypatch.setattr(client, "fetch_version_bundle", fetch)
    return RemotePatternStore()


def test_bundle_serves_payload_and_verified_bytes(monkeypatch):
    data = b"VALUE = 1\n"
    store = _store(monkeypatch, _bundle(files={"patterns/detector.py": data}))
    payload = store.get("versions", "v1")
    assert payload["version_id"] == "v1"
    assert store.read_blob(payload["files"]["patterns/detector.py"]) == data


def test_a_version_is_fetched_once_per_process(monkeypatch):
    """A backtester builds a store per symbol task; the bundle must not refetch."""
    calls: list = []
    store = _store(monkeypatch, _bundle(), calls)
    store.get("versions", "v1")
    store.get("versions", "v1")
    store.read_blob(store.get("versions", "v1")["files"]["patterns/detector.py"])
    # A different store instance in this process reuses the immutable bundle.
    other = RemotePatternStore()
    assert other.get("versions", "v1")["version_id"] == "v1"
    assert calls == ["v1"]


def test_tampered_file_is_rejected(monkeypatch):
    bundle = _bundle(files={"patterns/detector.py": b"VALUE = 1\n"})
    bundle["files"]["patterns/detector.py"] = base64.b64encode(b"VALUE = 666\n").decode()
    store = _store(monkeypatch, bundle)
    with pytest.raises(EditError, match="integrity"):
        store.get("versions", "v1")


def test_mismatched_version_payload_is_rejected(monkeypatch):
    bundle = _bundle(version_id="v1")
    bundle["payload"]["version_id"] = "something-else"
    store = _store(monkeypatch, bundle)
    with pytest.raises(EditError, match="mismatched"):
        store.get("versions", "v1")


def test_a_file_the_payload_does_not_record_is_rejected(monkeypatch):
    bundle = _bundle(files={})
    bundle["files"]["patterns/smuggled.py"] = base64.b64encode(b"X = 1\n").decode()
    store = _store(monkeypatch, bundle)
    with pytest.raises(EditError, match="unrecorded"):
        store.get("versions", "v1")


def test_only_versions_are_served(monkeypatch):
    store = _store(monkeypatch, _bundle())
    with pytest.raises(EditError, match="versions"):
        store.get("patterns", "p1")


def test_unloaded_and_malformed_refs_are_refused(monkeypatch):
    store = _store(monkeypatch, _bundle())
    missing = {"sha256": "0" * 64, "size_bytes": 3, "media_type": "text/plain"}
    with pytest.raises(EditError, match="not part of"):
        store.read_blob(missing)
    with pytest.raises(EditError, match="artifact reference"):
        store.read_blob({"sha256": "0" * 64})


def test_writes_are_refused_rather_than_emulated():
    store = RemotePatternStore()
    for name in ("transaction", "connect", "blob", "insert_version", "update_pattern"):
        with pytest.raises(EditError, match="remote"):
            getattr(store, name)()


def test_worker_heartbeat_is_skipped_not_written():
    """Worker rows belong to the host that owns the registry."""
    assert RemotePatternStore().save_worker({"worker_id": "w", "pid": 1}) is None


def test_resolve_uses_the_remote_verdict_without_sql(monkeypatch):
    store = RemotePatternStore()
    monkeypatch.setattr(store, "pinned",
                        lambda disabled=(), selected=None: {"p1": "v1"})
    # `transaction` raises on a remote store, so reaching SQL would fail here.
    assert PatternVersions(store).resolve(disabled=("p2",)) == {"p1": "v1"}


def test_remote_mode_needs_a_url_and_not_being_the_serving_host(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "pattern_api_url", "")
    monkeypatch.setattr(settings, "pattern_api_owner", False)
    assert remote_patterns_enabled() is False

    monkeypatch.setattr(settings, "pattern_api_url", "https://33ai.edos.uk")
    assert remote_patterns_enabled() is True

    # The host that serves the API must never proxy to itself.
    monkeypatch.setattr(settings, "pattern_api_owner", True)
    assert remote_patterns_enabled() is False


def test_open_pattern_store_follows_configuration(monkeypatch):
    from config import settings
    from core.pattern_edit_store import EditStore, open_pattern_store

    monkeypatch.setattr(settings, "pattern_api_url", "")
    assert isinstance(open_pattern_store(), EditStore)

    monkeypatch.setattr(settings, "pattern_api_url", "https://33ai.edos.uk")
    assert isinstance(open_pattern_store(), RemotePatternStore)


def test_open_pattern_store_honours_an_explicit_schema(monkeypatch):
    """Workers pass connection config across the process boundary."""
    from config import settings
    from core.pattern_edit_store import EditStore, open_pattern_store

    monkeypatch.setattr(settings, "pattern_api_url", "")
    store = open_pattern_store(root=".", dsn="postgresql:///never_opened",
                               schema="pattern_editor_worker")
    assert isinstance(store, EditStore)
    assert store.schema == "pattern_editor_worker"
