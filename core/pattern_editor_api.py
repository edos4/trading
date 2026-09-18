"""UI-independent Pattern Editor facade for the web tab and desktop dialog.

Every read (catalog, versions, source, diff, reports, presets, runs) and write
(submit an edit, cancel, retry a backtest, set default, archive) goes through the
durable PostgreSQL services. The frontends only render; they never resolve
patterns, validate presets, or decide eligibility themselves.

Edit execution runs on a background thread owned by this facade, so a page
reload or a dialog that is closed and reopened reconnects to the same job by its
durable id. The thread is a fast path only: the job row in PostgreSQL is the
authority for state, progress and results.
"""
from __future__ import annotations

import threading
from datetime import datetime

from core.backtest_service import BacktestService
from core.pattern_edit_service import PatternEditService
from core.pattern_edit_store import EditError, uid
from core.pattern_editor_contracts import ArchiveVersion, DefaultChange, EditRequest
from core.pattern_versions import PatternVersions
from utils.logger import log

TERMINAL_STATES = frozenset(
    {"completed", "failed", "cancelled", "blocked", "interrupted"})


def _text(data: bytes) -> str:
    return bytes(data).decode("utf-8", errors="replace")


def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _jsonable(value):
    """Recursively make PostgreSQL rows/JSON safe for an HTTP response."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return _text(value)
    return _iso(value)


def run_payload(service: BacktestService, run_id: str) -> dict:
    """API-shaped view of one durable run, shared by /backtest and /patterns."""
    status = service.status(run_id)
    payload = {
        "run_id": run_id,
        "state": status["state"],
        "attempt": status["attempt"],
        "retry_of": status.get("retry_of"),
        "completed_units": status["progress"].get("completed_units", 0),
        "total_units": status["progress"].get("total_units"),
        "unit": status["progress"].get("unit", "symbols"),
        "cancel_requested": status.get("cancel_requested", False),
        "error": status.get("error"),
        "result": None,
    }
    if status["state"] == "completed":
        stored = service.result(run_id)
        if stored is not None:
            metrics = stored["result"].metrics
            payload["result"] = {
                "metrics": metrics.model_dump(mode="json"),
                "trades": stored["trades"].get("trades", []),
                "open_positions": stored["open_positions"],
                "equity": stored["equity"],
                "zero_trades": metrics.trade_count == 0,
                "end_policy": stored["inputs"].request.preset.settings.execution.end_policy,
                "mode": stored["inputs"].request.preset.settings.mode,
            }
    return payload


def edit_request_from_values(editor: "PatternEditor", values: dict, *,
                             pattern_id: str, base_version_id: str,
                             instruction: str, preset_id: str | None = None,
                             preset_name: str | None = None,
                             idempotency_key: str | None = None) -> EditRequest:
    """Build one durable edit request, mirroring ``request_from_values``.

    A saved preset is required: either an explicit ``preset_id`` or settings
    that validate and are persisted. Contradictory values raise instead of
    silently substituting a default.
    """
    from core.backtest_params import settings_from_values

    service = editor.backtests
    if preset_id:
        preset = service.get_preset(preset_id)
    else:
        settings = settings_from_values(values)
        name = (preset_name or "").strip() or "edit preset"
        preset = next((p for p in service.list_presets()
                       if p.name == name and p.settings == settings), None)
        if preset is None:
            preset = service.save_preset(name, settings)
    return EditRequest(
        idempotency_key=(idempotency_key or "").strip() or f"edit-{uid()}",
        pattern_id=pattern_id,
        base_version_id=base_version_id,
        instruction=instruction,
        preset=preset,
    )


class PatternEditor:
    """Shared read/write surface for both Pattern Editor frontends."""

    def __init__(self, store=None, *, service: PatternEditService | None = None):
        self.service = service or PatternEditService(store)
        self.store = self.service.store
        self.versions: PatternVersions = self.service.versions
        self.backtests: BacktestService = self.service.backtests
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    # ── catalog / versions ───────────────────────────────────────────────
    def catalog(self) -> list[dict]:
        return [{
            "pattern_id": row["id"],
            "display_name": row["display_name"],
            "enabled": bool(row["enabled"]),
            "published": bool(row["published"]),
            "default_version_id": row["active"],
            "generation": int(row["generation"]),
        } for row in self.versions.catalog()]

    def versions_for(self, pattern_id: str, *,
                     include_archived: bool = False) -> list[dict]:
        return [{
            "version_id": row["payload"]["version_id"],
            "number": int(row["payload"]["version_number"]),
            "parent_version_id": row["payload"].get("parent_version_id"),
            "actor": row["payload"].get("actor"),
            "created_at": row["payload"].get("created_at"),
            "instruction": row["payload"].get("instruction"),
            "explanation": row["payload"].get("explanation"),
            "provenance": row["payload"].get("provenance"),
            "provider": row["payload"].get("provider"),
            "import_batch_id": row["payload"].get("import_batch_id"),
            "edit_job_id": row["payload"].get("edit_job_id"),
            "validation": row["validation"],
            "validation_report_id": row["validation_report_id"],
            "successful_run_id": row["successful_run_id"],
            "archived": row["archived_at"] is not None,
            "archived_at": _iso(row["archived_at"]),
        } for row in self.versions.list_versions(
            pattern_id, include_archived=include_archived)]

    def version_detail(self, version_id: str) -> dict:
        detail = self.versions.detail(version_id)
        version = detail["version"]
        life = detail["lifecycle"]
        return {
            "version_id": version["version_id"],
            "pattern_id": version["pattern_id"],
            "number": int(version["version_number"]),
            "parent_version_id": version.get("parent_version_id"),
            "actor": version.get("actor"),
            "provenance": version.get("provenance"),
            "created_at": version.get("created_at"),
            "source_path": version.get("source_path"),
            "instruction": version.get("instruction"),
            "explanation": version.get("explanation"),
            "provider": version.get("provider"),
            "import_batch_id": version.get("import_batch_id"),
            "edit_job_id": version.get("edit_job_id"),
            "content_sha256": version.get("content_sha256"),
            "metadata": version.get("metadata"),
            "files": sorted(version.get("files", {})),
            "lifecycle": {
                "version_id": life["version"],
                "generation": int(life["generation"]),
                "validation": life["validation"],
                "validation_report_id": life["validation_report_id"],
                "baseline_report_id": life["baseline_report_id"],
                "successful_run_id": life["successful_run_id"],
                "archived_at": _iso(life["archived_at"]),
            },
            "reports": detail["reports"],
            "backtests": [{
                "run_id": row.get("run_id"),
                "state": row["state"],
                "metrics": (row["result"] or {}).get("metrics"),
            } for row in detail["backtests"]],
        }

    def source(self, version_id: str) -> dict:
        files = self.versions.source(version_id)
        version = self.store.get("versions", version_id)
        source_path = version["source_path"]
        return {
            "source_path": source_path,
            "documentation_path": str(
                __import__("pathlib").Path(source_path).with_suffix(".md")),
            "files": {name: _text(data) for name, data in files.items()},
        }

    def diff(self, version_id: str, base_version_id: str | None = None) -> dict:
        return {"files": self.versions.diff(version_id, base_version_id)}

    # ── presets ──────────────────────────────────────────────────────────
    def presets(self) -> list[dict]:
        return [p.model_dump(mode="json") for p in self.backtests.list_presets()]

    def save_preset(self, name: str, settings, *, preset_id=None,
                    expected_generation=None) -> dict:
        return self.backtests.save_preset(
            name, settings, preset_id=preset_id,
            expected_generation=expected_generation).model_dump(mode="json")

    # ── edit jobs ────────────────────────────────────────────────────────
    def submit_edit(self, request: EditRequest) -> dict:
        job = self.service.submit(request)
        if job["state"] == "queued":
            self._start(job["id"])
        return job

    def retry_backtest(self, job_id: str) -> dict:
        """Re-run only the automatic backtests; no provider call, no new version."""
        job = self.service.retry_backtest(job_id)
        if job["state"] == "queued":
            self._start(job["id"])
        return job

    def job_status(self, job_id: str) -> dict:
        return self.service.status(job_id)

    def job_detail(self, job_id: str) -> dict:
        detail = self.service.detail(job_id)
        runs = detail.get("runs") or None
        if runs:
            detail["runs"] = {
                "candidate": runs.get("candidate"),
                "base": runs.get("base"),
                "version_id": runs.get("version_id"),
            }
        return _jsonable(detail)

    def cancel_job(self, job_id: str) -> dict:
        return self.service.cancel(job_id)

    def execute_job(self, job_id: str) -> None:
        """Blocking execution; callers off the UI thread use this directly."""
        self.service.execute(job_id)

    def _start(self, job_id: str) -> None:
        with self._lock:
            existing = self._threads.get(job_id)
            if existing is not None and existing.is_alive():
                return
            thread = threading.Thread(target=self._run, args=(job_id,), daemon=True,
                                      name=f"pattern-edit-{job_id[:8]}")
            self._threads[job_id] = thread
        thread.start()

    def _run(self, job_id: str) -> None:
        try:
            self.service.execute(job_id)
        except EditError as exc:
            log.warning(f"Pattern editor | job {job_id} did not start: {exc}")
        except Exception:  # noqa: BLE001 - the durable row carries the outcome
            log.exception(f"Pattern editor | job {job_id} failed")
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    # ── runs ─────────────────────────────────────────────────────────────
    def run_payload(self, run_id: str) -> dict:
        return run_payload(self.backtests, run_id)

    # ── lifecycle ────────────────────────────────────────────────────────
    def set_default(self, *, pattern_id: str, version_id: str,
                    expected_generation: int, idempotency_key: str | None = None) -> dict:
        change = DefaultChange(
            pattern_id=pattern_id, version_id=version_id,
            expected_generation=int(expected_generation),
            idempotency_key=idempotency_key or f"default-{uid()}")
        return _jsonable(self.versions.set_default(change))

    def archive(self, *, pattern_id: str, version_id: str,
                replacement_default_version_id: str | None = None,
                expected_generation: int, idempotency_key: str | None = None) -> dict:
        change = ArchiveVersion(
            pattern_id=pattern_id, version_id=version_id,
            replacement_default_version_id=replacement_default_version_id,
            expected_generation=int(expected_generation),
            idempotency_key=idempotency_key or f"archive-{uid()}")
        return _jsonable(self.versions.archive(change))
