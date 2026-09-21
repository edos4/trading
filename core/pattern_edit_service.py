"""Durable AI pattern-edit coordinator.

Submission pins the immutable base version, the frozen dataset and the saved
preset before any provider call. Generation, validation and the automatic
candidate/base backtests run as one durable job with explicit states, so a
failure at any stage stays visible, retryable where safe, and never changes a
default. A generated version is persisted immutably *before* validation; only a
successful validation can make it usable, and only the user can make it default.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from ai.providers.deepseek import (
    DeepSeekProvider, EditGenerationRequest, ProviderError, ProviderUnavailable,
)
from core.backtest_jobs import BacktestJobStore
from core.backtest_service import BacktestService
from core.pattern_edit_store import (
    Conflict, EditError, canonical, digest, open_pattern_store, uid,
)
from core.pattern_edit_validation import Validator
from core.pattern_editor_contracts import (
    BacktestRequest, DomainError, EditRequest, ErrorCode, JobState, VersionSelection,
)
from core.pattern_versions import PatternVersions, runtime_manifest


class EditCancelled(EditError):
    pass


class SandboxBlocked(EditError):
    """The candidate sandbox is unavailable; the job is blocked, not failed."""


class BacktestFailed(EditError):
    """An automatic candidate/base backtest did not complete; the job failed."""

    def __init__(self, message: str, *, run_id: str | None = None):
        super().__init__(message)
        self.run_id = run_id


class PatternEditService:
    def __init__(self, store=None, *, provider=None, runner=None,
                 jobs=None, versions=None, backtests=None, validator_factory=None,
                 dataset_root=None):
        self.store = store or open_pattern_store()
        self.versions = versions or PatternVersions(self.store)
        self.jobs = jobs or BacktestJobStore(self.store)
        self._provider = provider
        self._runner = runner
        self._validator_factory = validator_factory
        self.backtests = backtests or BacktestService(self.store, dataset_root=dataset_root)

    # ── provider / validator ─────────────────────────────────────────────
    def provider(self):
        if self._provider is None:
            self._provider = DeepSeekProvider()
        return self._provider

    def validator(self) -> Validator:
        if self._validator_factory is not None:
            return self._validator_factory(self.store)
        if self._runner is not None:
            return Validator(self.store, runner=self._runner)
        return Validator(self.store)

    # ── submission ───────────────────────────────────────────────────────
    def submit(self, request: EditRequest, *, actor: str = "user") -> dict:
        request = request if isinstance(request, EditRequest) else EditRequest.model_validate(request)
        base = self._base_version(request.pattern_id, request.base_version_id)
        settings = self.backtests.validate_settings(request.preset.settings)
        symbols = self._resolve_symbols(settings)
        dataset = self.backtests.dataset_ref(settings, symbols)
        payload = {
            "request": request.model_dump(mode="json"),
            "actor": actor,
            "dataset": dataset,
            "progress": {},
        }
        job = self.jobs.create(
            kind="edit",
            request_sha256=digest(canonical(request.model_dump(mode="json"))),
            idempotency_key=request.idempotency_key,
            payload=payload,
            preset_id=request.preset.preset_id,
            base_version_id=base["version_id"],
        )
        return self._view(job)

    def retry_backtest(self, job_id: str) -> dict:
        """Retry only the automatic backtests, without another provider call."""
        with self.store.transaction() as con:
            prior = con.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE",
                                (job_id,)).fetchone()
            if prior is None:
                raise EditError("Edit job not found")
            if prior["kind"] != "edit":
                raise EditError("Not an edit job")
            if prior["state"] not in ("completed", "failed", "blocked",
                                      "interrupted", "cancelled"):
                raise EditError("Only a finished edit job can be retried")
            payload_prior = prior["payload"] or {}
            # A version is recorded in the payload as soon as it is persisted,
            # so a job that failed during validation/backtesting still has one.
            generated = (payload_prior.get("run_ids") or {}).get("version_id") \
                or payload_prior.get("version_id") \
                or prior["generated_version"]
            if not generated:
                raise EditError("This job has no validated version to re-backtest")
            payload = dict(prior["payload"])
            payload["backtest_only"] = True
            payload["version_id"] = generated
            payload["progress"] = {}
            new_id = uid()
            con.execute(
                "INSERT INTO jobs(id,kind,pattern,base_version,preset,generated_version,"
                "idempotency_key,request_sha256,payload,state,attempt,retry_of) "
                "VALUES(%s,'edit',%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s)",
                (new_id, prior["pattern"], prior["base_version"], prior["preset"], generated,
                 f"{prior['idempotency_key']}:retry:{prior['attempt'] + 1}",
                 prior["request_sha256"], Jsonb(payload), prior["attempt"] + 1, prior["id"]))
            return self._view(con.execute("SELECT * FROM jobs WHERE id=%s", (new_id,)).fetchone())

    # ── execution ────────────────────────────────────────────────────────
    def execute(self, job_id: str, *, worker_id: str | None = None,
                cancel_event: threading.Event | None = None) -> dict:
        worker_id = worker_id or f"edit-{uid()}"
        with self.store.transaction() as con:
            self.jobs.register_worker(worker_id, con)
            row = con.execute(
                "UPDATE jobs SET owner=%s, lease_token=%s, "
                "lease_expires_at=clock_timestamp() + interval '10 minutes', "
                "state='generating', updated_at=clock_timestamp() "
                "WHERE id=%s AND state='queued' RETURNING *", (worker_id, uid(), job_id)
            ).fetchone()
        if row is None:
            raise EditError("Edit job is not queued; it may already be running")
        return self._run_claimed(row, worker_id, cancel_event)

    def run_once(self, worker_id: str | None = None) -> dict | None:
        worker_id = worker_id or f"edit-runner-{uid()}"
        self.jobs.expire_leases()
        row = self.jobs.claim(worker_id, kinds=("edit",))
        if row is None:
            return None
        return self._run_claimed(row, worker_id, None)

    def _run_claimed(self, row: dict, owner: str,
                     cancel_event: threading.Event | None) -> dict:
        job_id = row["id"]
        payload = row["payload"]
        request = EditRequest.model_validate(payload["request"])
        event = cancel_event or threading.Event()
        try:
            if payload.get("backtest_only"):
                version_id = payload.get("version_id")
            else:
                version_id = self._generate(job_id, owner, request, payload, event)
            self._validate(job_id, owner, request, version_id, payload, event)
            runs = self._backtest(job_id, owner, request, payload, version_id, event)
            self.jobs.touch(job_id, owner, state="completed",
                            progress={"completed_units": 4, "total_units": 4,
                                      "unit": "stages", "version_id": version_id},
                            error=None)
            with self.store.transaction() as con:
                con.execute("UPDATE jobs SET generated_version=%s WHERE id=%s",
                            (version_id, job_id))
            return self._view(self.jobs.find(job_id)) | {"runs": runs}
        except EditCancelled:
            return self.jobs.finish(job_id, owner, JobState.CANCELLED.value,
                                    error=_error("execution-failed", "Edit cancelled", True))
        except ProviderUnavailable as exc:
            return self.jobs.finish(job_id, owner, JobState.BLOCKED.value,
                                    error=_error("provider-failed", str(exc), False))
        except ProviderError as exc:
            return self.jobs.finish(job_id, owner, JobState.FAILED.value,
                                    error=_error("provider-failed", str(exc), exc.retryable))
        except SandboxBlocked as exc:
            return self.jobs.finish(job_id, owner, JobState.BLOCKED.value,
                                    error=_error("sandbox-unavailable", str(exc), True))
        except BacktestFailed as exc:
            return self.jobs.finish(job_id, owner, JobState.FAILED.value,
                                    error=_error("execution-failed", str(exc), True))
        except EditError as exc:
            return self.jobs.finish(job_id, owner, JobState.FAILED.value,
                                    error=_error("validation-failed", str(exc), False))
        except Exception:  # noqa: BLE001 - never leak stderr/DSNs to callers
            from utils.logger import log
            log.exception(f"PatternEditService | job {job_id} failed")
            return self.jobs.finish(job_id, owner, JobState.FAILED.value,
                                    error=_error("execution-failed",
                                                 "Edit job failed; see server logs", True))

    # ── stages ───────────────────────────────────────────────────────────
    def _generate(self, job_id: str, owner: str, request: EditRequest,
                  payload: dict, event: threading.Event) -> str:
        base = self._base_version(request.pattern_id, request.base_version_id)
        self._check_cancel(job_id, event)
        if not self.provider().available():
            raise ProviderUnavailable("DeepSeek is not configured; generation is blocked")
        source_path = base["source_path"]
        documentation_path = str(Path(source_path).with_suffix(".md"))
        generation = self.provider().generate(EditGenerationRequest(
            instruction=request.instruction,
            pattern_id=request.pattern_id,
            source_path=source_path,
            documentation_path=documentation_path,
            source=self.store.read_blob(base["files"][source_path]).decode("utf-8"),
            documentation=self._read_documentation(base, documentation_path),
            interface=self._interface(base),
        ))
        self._check_cancel(job_id, event)
        self.jobs.touch(job_id, owner, state="validating",
                        progress={"completed_units": 1, "total_units": 4, "unit": "stages"})
        files = dict(base["files"])
        files[source_path] = self.store.blob(generation.source.encode("utf-8"), "text/plain")
        files[documentation_path] = self.store.blob(
            generation.documentation.encode("utf-8"), "text/plain")
        version = dict(
            base, version_id=uid(), parent_version_id=base["version_id"],
            provenance="generated", import_batch_id=None, files=files,
            content_sha256=digest(canonical(files)), actor="ai-edit",
            # Record the runtime this version was actually generated under; the
            # base's fingerprint may predate it and would fail the compatibility
            # check when the trusted runtime has moved on.
            runtime=runtime_manifest(self.store.root),
            instruction=request.instruction, explanation=generation.explanation,
            edit_job_id=job_id,
            provider={
                "provider": "deepseek",
                "requested_model": generation.requested_model,
                "returned_model": generation.returned_model,
                "request_id": generation.request_id,
            },
        )
        version.pop("validation_report_id", None)
        version.pop("successful_run_id", None)
        inserted = self.store.insert_version(version)
        version_id = inserted["version_id"]
        # Record the generated version on the job as soon as it exists, so a
        # later validation/backtest failure still points at its evidence.
        with self.store.transaction() as con:
            con.execute("UPDATE jobs SET payload = payload || %s WHERE id=%s",
                        (Jsonb({"version_id": version_id}), job_id))
        return version_id

    def _validate(self, job_id: str, owner: str, request: EditRequest,
                  version_id: str, payload: dict, event: threading.Event) -> None:
        self._check_cancel(job_id, event)
        version = self.store.get("versions", version_id)
        revision = {
            "revision_id": uid(),
            "session_id": None,
            "files": version["files"],
            "candidate_sha256": version["content_sha256"],
            "explanation": version.get("explanation"),
            "unresolved_questions": None,
        }
        dataset = self._validation_dataset(payload["dataset"], request)
        report = self.validator().validate(version, revision, dataset, [])
        state = "passed" if report.get("ready") else (
            "blocked" if any(c["name"] == "sandbox" and c["outcome"] == "unavailable"
                             for c in report["checks"]) else "failed")
        report["version_id"] = version_id
        report["status"] = state
        # Automatic edits have no interactive revision; the report is bound to
        # its immutable version instead.
        report["revision_id"] = None
        with self.store.transaction() as con:
            self.store.insert_report(report, con)
            con.execute("UPDATE version_lifecycle SET validation=%s, validation_report_id=%s "
                        "WHERE version=%s", (state, report["report_id"], version_id))
        if state == "blocked":
            raise SandboxBlocked("Candidate sandbox is unavailable; validation is blocked")
        if state == "failed":
            raise EditError("Candidate failed validation; see its report")
        self.jobs.touch(job_id, owner, state="backtesting",
                        progress={"completed_units": 2, "total_units": 4,
                                  "unit": "stages", "version_id": version_id})

    def _backtest(self, job_id: str, owner: str, request: EditRequest,
                  payload: dict, version_id: str, event: threading.Event) -> dict:
        self._check_cancel(job_id, event)
        runs: dict = {"version_id": version_id}

        def run(slot: str, selected_version_id: str) -> None:
            try:
                runs[slot] = self._run_or_reuse(
                    job_id, request, selected_version_id, owner, event)["id"]
            except BacktestFailed as exc:
                # Keep the failed run's evidence linked to the edit job too.
                if exc.run_id:
                    runs[slot] = exc.run_id
                raise
            finally:
                self._record_runs(job_id, runs)

        run("candidate", version_id)
        run("base", request.base_version_id)
        return runs

    def _record_runs(self, job_id: str, runs: dict) -> None:
        with self.store.transaction() as con:
            con.execute("UPDATE jobs SET payload = payload || %s WHERE id=%s",
                        (Jsonb({"run_ids": runs}), job_id))

    def _run_or_reuse(self, job_id: str, request: EditRequest, version_id: str,
                      owner: str, event: threading.Event) -> dict:
        run_request = _probe_request(request, (), version_id=version_id)
        plan = self.backtests.prepare(run_request)
        inputs = self.backtests.freeze_inputs(run_request, plan["symbols"])
        existing = self.backtests.find_reusable_run(inputs.inputs_sha256)
        if existing is not None:
            return self.backtests.status(existing) | {"reused": True}
        job = self.backtests.submit(run_request)
        self.backtests.execute(job["id"], cancel_event=event)
        status = self.backtests.status(job["id"])
        # The durable backtest adapter records a failed/interrupted run without
        # raising; a non-completed automatic run must fail the edit job so the
        # version stays recoverable via "Backtest again" instead of being
        # reported as a completed edit.
        if status["state"] != "completed":
            message = (status.get("error") or {}).get("message") or status["state"]
            raise BacktestFailed(
                f"Automatic backtest {status['state']}: {message}", run_id=job["id"])
        return status | {"reused": False}

    # ── helpers ──────────────────────────────────────────────────────────
    def _base_version(self, pattern_id: str, version_id: str) -> dict:
        version = self.store.get("versions", version_id)
        if version["pattern_id"] != pattern_id:
            raise EditError("Base version must belong to the selected pattern")
        metadata = version.get("metadata") or {}
        if metadata.get("skipped", True):
            raise EditError("Skipped or unvalidated versions cannot be edited")
        for name, ref in version["files"].items():
            self.store.read_blob(ref)
        return version

    def _read_documentation(self, version: dict, path: str) -> str:
        ref = version["files"].get(path)
        if ref is None:
            raise EditError("Base version has no paired documentation")
        return self.store.read_blob(ref).decode("utf-8")

    def _interface(self, version: dict) -> dict:
        metadata = version.get("metadata") or {}
        return {
            "class_name": metadata.get("class_name"),
            "name": metadata.get("name"),
            "timeframes": metadata.get("timeframes"),
            "source_path": version["source_path"],
        }

    def _resolve_symbols(self, settings) -> list[str]:
        from core.backtest_params import resolve_symbols

        return resolve_symbols(settings)

    def _validation_dataset(self, dataset_ref: dict, request: EditRequest) -> dict:
        from core.market import get_market

        rows = json.loads(self.store.read_blob(dataset_ref))
        symbol = sorted(rows)[0]
        settings = request.preset.settings
        candles = [
            {"timestamp": row[0], "open": row[1], "high": row[2], "low": row[3],
             "close": row[4], "volume": row[5]}
            for row in rows[symbol]
        ]
        return {
            "symbol": symbol,
            "timeframe": settings.timeframe,
            "market": settings.market,
            "session_timezone": get_market(settings.market).session_tz,
            "candles": candles,
            "operation": "signals",
            "lookback": 30,
        }

    def _check_cancel(self, job_id: str, event: threading.Event) -> None:
        if event.is_set() or self.jobs.cancel_requested(job_id):
            raise EditCancelled("Edit cancelled")

    # ── views ────────────────────────────────────────────────────────────
    def _view(self, row: dict) -> dict:
        payload = row.get("payload") or {}
        return {
            "id": row["id"], "state": row["state"], "attempt": row["attempt"],
            "retry_of": row.get("retry_of"), "base_version_id": row.get("base_version"),
            "generated_version_id": row.get("generated_version")
            or payload.get("version_id"),
            "progress": payload.get("progress", {}),
            "error": payload.get("error"),
            "cancel_requested": bool(payload.get("cancel_requested")),
        }

    def status(self, job_id: str) -> dict:
        return self._view(self.jobs.find(job_id))

    def cancel(self, job_id: str, *, actor: str = "user") -> dict:
        return self._view(self.jobs.request_cancel(job_id, actor=actor))

    def detail(self, job_id: str) -> dict:
        row = self.jobs.find(job_id)
        payload = row.get("payload") or {}
        version_id = row.get("generated_version") or payload.get("version_id")
        detail = self._view(row) | {"report": None, "diff": None, "runs": payload.get("run_ids"),
                                    "explanation": None}
        if version_id:
            version = self.store.get("versions", version_id)
            detail["explanation"] = version.get("explanation")
            detail["diff"] = self.versions.diff(version_id)
            lifecycle = None
            with self.store.connect() as con:
                lifecycle = con.execute(
                    "SELECT validation, validation_report_id FROM version_lifecycle "
                    "WHERE version=%s", (version_id,)).fetchone()
            if lifecycle and lifecycle["validation_report_id"]:
                detail["report"] = self.store.get("reports", lifecycle["validation_report_id"])
        return detail


def _probe_request(request: EditRequest, symbols=None, *, version_id: str | None = None):
    """A BacktestRequest for the edit's preset, optionally pinned to one version."""
    base_id = version_id or request.base_version_id
    return BacktestRequest(
        idempotency_key=f"edit-probe-{uid()}",
        versions=(VersionSelection(pattern_id=request.pattern_id, version_id=base_id),),
        preset=request.preset,
    )


def _error(code: str, message: str, retryable: bool) -> dict:
    return DomainError(code=ErrorCode(code), message=message,
                       retryable=retryable).model_dump(mode="json")
