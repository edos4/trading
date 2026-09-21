"""Durable backtest runs for the web UI.

Thin adapter over ``core.backtest_service``: the web process only submits a
durable job and reads its status/result; execution happens on a background
thread of this process (the job record is the authority, so a page reload can
reconnect to the same run by ID). PostgreSQL errors are surfaced, never
converted into a file or in-memory fallback.
"""
from __future__ import annotations

import threading

from core.backtest_service import BacktestService
from core.pattern_edit_store import EditError, uid
from core.pattern_editor_api import run_payload
from core.remote_pattern_store import remote_patterns_enabled
from utils.logger import log


class BacktestRuns:
    def __init__(self, service: BacktestService | None = None):
        self._service = service
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        # Runs executing here for recording on the owning host, by run id.
        self._remote: dict[str, dict] = {}

    # ── service ──────────────────────────────────────────────────────────
    def service(self) -> BacktestService:
        if self._service is None:
            self._service = BacktestService()
        return self._service

    def catalog(self) -> list[dict]:
        rows = self.service().versions.catalog()
        return [{
            "pattern_id": row["id"],
            "display_name": row["display_name"],
            "enabled": bool(row["enabled"]),
            "default_version_id": row["active"],
            "generation": int(row["generation"]),
        } for row in rows]

    def versions(self, pattern_id: str, *, include_archived: bool = False) -> list[dict]:
        return [{
            "version_id": row["payload"]["version_id"],
            "number": int(row["payload"]["version_number"]),
            "actor": row["payload"].get("actor"),
            "created_at": row["payload"].get("created_at"),
            "validation": row["validation"],
            "archived": row["archived_at"] is not None,
        } for row in self.service().versions.list_versions(
            pattern_id, include_archived=include_archived)]

    def presets(self) -> list[dict]:
        return [p.model_dump(mode="json") for p in self.service().list_presets()]

    def save_preset(self, name: str, settings, *, preset_id=None,
                    expected_generation=None) -> dict:
        return self.service().save_preset(
            name, settings, preset_id=preset_id,
            expected_generation=expected_generation).model_dump(mode="json")

    # ── runs ─────────────────────────────────────────────────────────────
    def submit(self, request) -> dict:
        if remote_patterns_enabled():
            return self._submit_remote(request)
        job = self.service().submit(request)
        if job["state"] == "queued":
            self._start(job["id"])
        return job

    def _submit_remote(self, request) -> dict:
        """Execute here, then record the run on the host that owns the registry.

        The identifier is ours: the host stores it with the uploaded evidence,
        so polling continues against the authoritative record.
        """
        run_id = uid()
        thread = threading.Thread(target=self._run_remote, args=(run_id, request),
                                  daemon=True, name=f"backtest-{run_id[:8]}")
        with self._lock:
            self._remote[run_id] = {"state": "queued", "progress": {}, "event": threading.Event()}
        thread.start()
        return {"id": run_id, "state": "queued", "attempt": 1, "retry_of": None,
                "progress": {}, "error": None, "cancel_requested": False}

    def _run_remote(self, run_id: str, request) -> None:
        from data.pattern_client import upload_run

        with self._lock:
            entry = self._remote.get(run_id) or {}
            entry["state"] = "backtesting"
            self._remote[run_id] = entry
        try:
            payload = self.service().run_locally(
                request, run_id, cancel_event=entry.get("event"))
            upload_run(payload)
        except EditError as exc:
            self._fail_remote(run_id, str(exc))
            return
        except Exception:  # noqa: BLE001 - never leak internals to the UI
            log.exception(f"Web backtest | local run {run_id} failed")
            self._fail_remote(run_id, "Backtest failed; see server logs")
            return
        with self._lock:
            self._remote.pop(run_id, None)

    def _fail_remote(self, run_id: str, message: str) -> None:
        from core.pattern_editor_contracts import DomainError, ErrorCode

        with self._lock:
            if run_id in self._remote:
                self._remote[run_id] = {
                    "state": "failed", "progress": {},
                    "error": DomainError(code=ErrorCode.EXECUTION_FAILED,
                                         message=message).model_dump(mode="json"),
                }

    def _start(self, run_id: str) -> None:
        with self._lock:
            existing = self._threads.get(run_id)
            if existing is not None and existing.is_alive():
                return
            thread = threading.Thread(target=self._run, args=(run_id,), daemon=True,
                                      name=f"backtest-{run_id[:8]}")
            self._threads[run_id] = thread
        thread.start()

    def _run(self, run_id: str) -> None:
        try:
            self.service().execute(run_id)
        except EditError as exc:
            log.warning(f"Web backtest | run {run_id} did not start: {exc}")
        except Exception:  # noqa: BLE001 - never leak internals to the UI
            log.exception(f"Web backtest | run {run_id} failed")
        finally:
            with self._lock:
                self._threads.pop(run_id, None)

    def status(self, run_id: str) -> dict:
        return self.service().status(run_id)

    def progress(self, run_id: str) -> dict:
        return self.service().progress(run_id).model_dump(mode="json")

    def result(self, run_id: str) -> dict | None:
        return self.service().result(run_id)

    def cancel(self, run_id: str) -> dict:
        if remote_patterns_enabled():
            return self._cancel_remote(run_id)
        return self.service().cancel(run_id)

    def _cancel_remote(self, run_id: str) -> dict:
        with self._lock:
            entry = self._remote.get(run_id)
        if entry is None:
            # Already uploaded: the host holds a completed, immutable record.
            raise EditError("This run is already recorded and cannot be cancelled")
        entry["event"].set()
        return {"id": run_id, "state": "cancelled", "attempt": 1, "retry_of": None,
                "progress": dict(entry.get("progress", {})), "error": None,
                "cancel_requested": True}

    def retry(self, run_id: str) -> dict:
        if remote_patterns_enabled():
            raise EditError("A run recorded on the registry host cannot be retried from here")
        job = self.service().retry(run_id)
        self._start(job["id"])
        return job

    # ── API-shaped payloads ──────────────────────────────────────────────
    def run_payload(self, run_id: str) -> dict:
        remote = self._remote_payload(run_id)
        if remote is not None:
            return remote
        if remote_patterns_enabled():
            # Recorded on the host that owns the registry; its payload is the
            # authority, and `run_payload` there has the same shape.
            from data.pattern_client import fetch_run

            return fetch_run(run_id)
        # One implementation, shared with the Patterns tab.
        return run_payload(self.service(), run_id)

    def _remote_payload(self, run_id: str) -> dict | None:
        """In-flight local state for a run that is recorded elsewhere."""
        with self._lock:
            entry = self._remote.get(run_id)
            if entry is None:
                return None
            state = {"state": entry["state"], "progress": dict(entry.get("progress", {})),
                     "error": entry.get("error")}
        return {
            "run_id": run_id,
            "state": state["state"],
            "attempt": 1,
            "retry_of": None,
            "completed_units": state["progress"].get("completed_units", 0),
            "total_units": state["progress"].get("total_units"),
            "unit": state["progress"].get("unit", "symbols"),
            "cancel_requested": False,
            "error": state["error"],
            "result": None,
        }


backtest_runs = BacktestRuns()
