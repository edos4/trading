"""Read-only pattern registry backed by the remote editor API.

Implements only the read surface the loader, scanner, backtester and validator
use, so those run without a local editor database. Versions and their blobs are
immutable, so a fetched bundle is cached for the life of the process; the pinned
set is decided by the serving host on every call.

Anything that would write raises. A client has no editor database to write to,
and silently degrading would hide the mistake.
"""
from __future__ import annotations

import base64
import threading
from pathlib import Path

from core.pattern_edit_store import ROOT, EditError, digest
from utils.logger import log


#: Version bundles are immutable, so one fetch serves every store instance in
#: the process -- including a backtester that builds a store per symbol task.
_bundle_cache: dict[str, dict] = {}
_bundle_lock = threading.Lock()


def _cached_bundle(version_id: str) -> dict | None:
    with _bundle_lock:
        return _bundle_cache.get(version_id)


def _cache_bundle(version_id: str, entry: dict) -> None:
    with _bundle_lock:
        _bundle_cache[version_id] = entry


def clear_bundle_cache() -> None:
    """Drop fetched bundles (tests, and any explicit registry refresh)."""
    with _bundle_lock:
        _bundle_cache.clear()


def remote_patterns_enabled() -> bool:
    """True when the registry lives behind the API instead of local PostgreSQL.

    An empty ``PATTERN_API_URL`` keeps the local database everywhere, and the
    host that serves the API sets ``PATTERN_API_OWNER`` so it never proxies to
    itself.
    """
    from config import settings

    if settings.pattern_api_owner:
        return False
    return bool((settings.pattern_api_url or "").strip())


class RemotePatternStore:
    """Store double that satisfies the read calls made by pattern execution."""

    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()

    # ── reads ────────────────────────────────────────────────────────────
    def pinned(self, disabled=(), selected=None):
        """Version set pinned by the serving host, with its eligibility verdict."""
        from data.pattern_client import fetch_pinned

        return fetch_pinned(tuple(disabled), selected)

    def get(self, table, identity, con=None):
        if table != "versions":
            raise EditError("The remote pattern registry only serves versions")
        return self._load(identity)["payload"]

    def read_blob(self, ref, con=None):
        if not isinstance(ref, dict) or set(ref) != {"sha256", "size_bytes", "media_type"}:
            raise EditError("Invalid database artifact reference")
        from core.pattern_editor_contracts import ContentRef

        try:
            ContentRef.model_validate(ref)
        except ValueError:
            raise EditError("Invalid database artifact reference") from None
        with _bundle_lock:
            loaded = list(_bundle_cache.values())
        for bundle in loaded:
            data = bundle["files"].get(ref["sha256"])
            if data is not None:
                if len(data) != ref["size_bytes"]:
                    raise EditError("Artifact hash/size mismatch")
                return data
        # Nothing loaded carries those bytes, so the read cannot be satisfied
        # from the API surface this client is given.
        raise EditError("Required artifact is not part of a loaded pattern version")

    def save_worker(self, payload):
        """Worker heartbeats belong to the host that owns the registry."""
        log.debug(
            "Pattern registry is remote | worker acknowledgement skipped "
            f"for pid={payload.get('pid')}"
        )

    # ── writes: refused, never emulated ──────────────────────────────────
    def _read_only(self, *args, **kwargs):
        raise EditError(
            "The pattern registry is remote and read-only; "
            "run this operation on the host that owns the database"
        )

    connect = _read_only
    transaction = _read_only
    blob = _read_only
    list = _read_only
    active_set = _read_only
    insert_version = _read_only
    insert_report = _read_only
    insert_revision = _read_only
    create_pattern = _read_only
    lock_pattern = _read_only
    update_pattern = _read_only
    save_session = _read_only

    # ── internals ────────────────────────────────────────────────────────
    def _load(self, version_id: str) -> dict:
        cached = _cached_bundle(version_id)
        if cached is not None:
            return cached
        from data.pattern_client import fetch_version_bundle

        bundle = fetch_version_bundle(version_id)
        payload = bundle.get("payload")
        if not isinstance(payload, dict) or payload.get("version_id") != version_id:
            raise EditError("Pattern API returned a mismatched version payload")
        files = {}
        for name, encoded in bundle.get("files", {}).items():
            ref = payload.get("files", {}).get(name)
            if not isinstance(ref, dict):
                raise EditError("Pattern API returned an unrecorded file: " + name)
            try:
                data = base64.b64decode(encoded, validate=True)
            except Exception:
                raise EditError("Pattern API returned an undecodable file: " + name) from None
            if len(data) != ref["size_bytes"] or digest(data) != ref["sha256"]:
                raise EditError("Pattern API artifact failed its integrity check: " + name)
            files[ref["sha256"]] = data
        entry = {"payload": payload, "files": files}
        _cache_bundle(version_id, entry)
        return entry
