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
from core.pattern_edit_store import EditError
from core.pattern_editor_api import run_payload
from utils.logger import log


class BacktestRuns:
    def __init__(self, service: BacktestService | None = None):
        self._service = service
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

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
        job = self.service().submit(request)
        if job["state"] == "queued":
            self._start(job["id"])
        return job

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
        return self.service().cancel(run_id)

    def retry(self, run_id: str) -> dict:
        job = self.service().retry(run_id)
        self._start(job["id"])
        return job

    # ── API-shaped payloads ──────────────────────────────────────────────
    def run_payload(self, run_id: str) -> dict:
        # One implementation, shared with the Patterns tab.
        return run_payload(self.service(), run_id)


backtest_runs = BacktestRuns()
