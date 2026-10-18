"""Laya application service (docs/laya.md section 8).

Owns the bounded inference queue, job transitions, cancellation, idempotent
submission, coalesced identical work, and local persistence. The inference
worker returns results to this service instead of writing independently.

Job states: queued/loading/running/succeeded/failed/cancelled/interrupted.
Cancellation is terminal: a late result cannot resurrect a cancelled job.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.laya_context import coalescing_hash, input_hash, semantic_hash
from core.laya_contracts import (
    LayaAnswer, LayaAssessmentInput, LayaAssessmentResult, LayaPolicy,
)

TERMINAL_STATES = ("succeeded", "failed", "cancelled", "interrupted")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class LayaService:
    def __init__(
        self,
        store=None,
        *,
        max_pending: int = 8,
        timeout_s: float = 60.0,
        fake: bool = False,
        worker_factory: Callable[[dict], dict] | None = None,
    ) -> None:
        from core.laya_store import get_store

        self.store = store or get_store()
        self.max_pending = max(1, int(max_pending))
        self.timeout_s = max(1.0, float(timeout_s))
        self.fake = fake
        self._worker_factory = worker_factory
        self._pending: queue.Queue[str] = queue.Queue(maxsize=self.max_pending)
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()
        self._seed_policies()

    # ── capability ────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        from config import settings

        enabled = bool(getattr(settings, "laya_enabled", False))
        if not enabled:
            return {"state": "disabled", "enabled": False, "reason": "LAYA_ENABLED is false"}
        worker_python = (getattr(settings, "laya_worker_python", "") or "").strip()
        model_path = (getattr(settings, "laya_model_path", "") or "").strip()
        if self.fake or self._worker_factory is not None:
            return {"state": "ready", "enabled": True, "mode": "fake",
                    "model": {"model_id": "fake", "revision": "fake"},
                    "queue": {"pending": self._pending.qsize(), "max": self.max_pending}}
        if not worker_python or not Path(worker_python).exists():
            return {"state": "missing-dependency", "enabled": True,
                    "reason": "LAYA_WORKER_PYTHON is not configured"}
        if not model_path or not Path(model_path).exists():
            return {"state": "missing-model", "enabled": True,
                    "reason": "LAYA_MODEL_PATH is not provisioned"}
        return {"state": "ready", "enabled": True, "mode": "worker",
                "model": {"model_id": model_path, "revision": "pinned"},
                "queue": {"pending": self._pending.qsize(), "max": self.max_pending}}

    def tasks(self) -> list[dict[str, Any]]:
        from core.laya_contracts import TASK_PRESETS

        out = []
        for task_id, preset in TASK_PRESETS.items():
            out.append({
                "task_id": task_id,
                "input_mode": preset["input_mode"],
                "description": preset["description"],
                "questions": [q.model_dump(mode="json") for q in preset["questions"]],
            })
        return out

    # ── submission ────────────────────────────────────────────────────────
    def submit(
        self,
        assessment: LayaAssessmentInput,
        *,
        idempotency_key: str,
        rerun_of: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency_key is required (max 200 chars)")
        digest = input_hash(assessment)
        coalesced = coalescing_hash(assessment)
        existing = self.store.get_job_by_key(idempotency_key)
        if existing is not None:
            # Idempotency compares semantic input (excluding wall-clock-only
            # observed/ingested timestamps); the same key with genuinely
            # different input is a conflict.
            try:
                prior_row = self.store.get_assessment(existing.get("assessment_id") or "")
                prior_semantic = None
                if prior_row is not None:
                    prior = LayaAssessmentInput.model_validate(
                        json.loads(prior_row["input_json"]))
                    prior_semantic = semantic_hash(prior)
            except Exception:
                prior_semantic = None
            if prior_semantic is not None and prior_semantic != semantic_hash(assessment):
                raise ConflictError("idempotency key already used with different input")
            return existing, False
        # Coalesce identical work: same evidence+cutoff+schema+model.
        hit = self.store.find_by_coalescing(coalesced)
        job = {
            "id": _uid("job"),
            "idempotency_key": idempotency_key,
            "input_hash": digest,
            "assessment_id": assessment.assessment_id,
            "state": "queued",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "error_code": None,
            "error_detail": None,
            "attempt": 1,
            "cancelled": 0,
            "previous_attempt_id": rerun_of,
        }
        created = self.store.create_job(job)
        if created.get("id") != job["id"]:
            return created, False
        self.store.save_assessment({
            "id": assessment.assessment_id,
            "market": assessment.market,
            "symbol": assessment.symbol,
            "task_id": assessment.question_schema_id,
            "input_mode": "text" if assessment.documents else "candidate",
            "decision_at": assessment.decision_at.isoformat(),
            "as_of": assessment.as_of.isoformat(),
            "input_json": json.dumps(assessment.model_dump(mode="json")),
            "input_hash": digest,
            "coalescing_hash": coalesced,
            "raw_json": None,
            "normalized_json": None,
            "calibration_status": "uncalibrated",
            "elapsed_ms": 0,
            "warnings_json": "[]",
            "truncated": 0,
            "created_at": _now_iso(),
            "rerun_of": rerun_of,
        })
        if hit is not None and hit.get("normalized_json"):
            # Identical work already computed: link without re-running the model.
            self.store.save_assessment({
                "id": assessment.assessment_id,
                "market": assessment.market,
                "symbol": assessment.symbol,
                "task_id": assessment.question_schema_id,
                "input_mode": "text" if assessment.documents else "candidate",
                "decision_at": assessment.decision_at.isoformat(),
                "as_of": assessment.as_of.isoformat(),
                "input_json": json.dumps(assessment.model_dump(mode="json")),
                "input_hash": digest,
                "coalescing_hash": coalesced,
                "raw_json": hit.get("raw_json"),
                "normalized_json": hit.get("normalized_json"),
                "calibration_status": hit.get("calibration_status") or "uncalibrated",
                "elapsed_ms": int(hit.get("elapsed_ms") or 0),
                "warnings_json": hit.get("warnings_json") or "[]",
                "truncated": int(hit.get("truncated") or 0),
                "created_at": _now_iso(),
                "rerun_of": hit.get("id"),
            })
            self.store.update_job(job["id"], state="succeeded", updated_at=_now_iso())
            return self.store.get_job(job["id"]) or job, True
        try:
            self._pending.put_nowait(job["id"])
        except queue.Full:
            self.store.update_job(job["id"], state="failed", updated_at=_now_iso(),
                                  error_code="queue-full",
                                  error_detail="Worker queue saturated; retry later")
            raise QueueFullError("Laya worker queue is saturated")
        worker = threading.Thread(target=self._run_job, args=(job["id"],),
                                  name=f"laya-{job['id']}", daemon=True)
        with self._guard:
            self._threads[job["id"]] = worker
        worker.start()
        return self.store.get_job(job["id"]) or job, True

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self.store.get_job(job_id)
        if job is None:
            return None
        if job.get("state") in TERMINAL_STATES:
            return job  # idempotent
        self.store.update_job(job_id, cancelled=1, state="cancelled",
                              updated_at=_now_iso(),
                              error_code="cancelled",
                              error_detail="Cancelled by operator")
        return self.store.get_job(job_id)

    # ── execution ─────────────────────────────────────────────────────────
    def _run_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None:
            return
        if int(job.get("cancelled") or 0):
            return
        self.store.update_job(job_id, state="loading", updated_at=_now_iso())
        row = self.store.get_assessment(job["assessment_id"])
        if row is None:
            self.store.update_job(job_id, state="failed", updated_at=_now_iso(),
                                  error_code="missing-input",
                                  error_detail="Assessment input was not persisted")
            return
        try:
            assessment = LayaAssessmentInput.model_validate(json.loads(row["input_json"]))
        except Exception as exc:
            self.store.update_job(job_id, state="failed", updated_at=_now_iso(),
                                  error_code="invalid-request",
                                  error_detail=str(exc)[:300])
            return
        job_now = self.store.get_job(job_id)
        if job_now is None or int(job_now.get("cancelled") or 0):
            return
        self.store.update_job(job_id, state="running", updated_at=_now_iso())
        try:
            response = self._invoke_worker(assessment)
        except TimeoutError:
            self._mark_interrupted_or_failed(job_id, "timeout", "Worker deadline exceeded")
            return
        except Exception as exc:  # noqa: BLE001 - sanitized stable error code
            self._mark_interrupted_or_failed(job_id, "inference-failed", str(exc)[:300])
            return
        # Cancellation is terminal: a late result cannot resurrect the job.
        job_now = self.store.get_job(job_id)
        if job_now is None or int(job_now.get("cancelled") or 0) or job_now.get("state") == "cancelled":
            return
        if not response.get("ok"):
            self.store.update_job(job_id, state="failed", updated_at=_now_iso(),
                                  error_code=str(response.get("error_code") or "inference-failed"),
                                  error_detail=str(response.get("error_detail") or "")[:300])
            return
        try:
            answers = tuple(LayaAnswer.model_validate(a) for a in response.get("answers") or [])
            result = LayaAssessmentResult(
                assessment_id=assessment.assessment_id,
                input_hash=input_hash(assessment),
                answers=answers,
                raw=response.get("raw") if isinstance(response.get("raw"), dict) else {"task_id": assessment.question_schema_id},
                raw_version=str(response.get("raw_version") or "laya-app-v1"),
                calibration_status="uncalibrated",
                elapsed_ms=int(response.get("elapsed_ms") or 0),
                warnings=tuple(str(w) for w in (response.get("warnings") or [])),
                truncated=bool(response.get("truncated")),
                options_collapsed=bool(response.get("options_collapsed")),
            )
        except Exception as exc:
            self.store.update_job(job_id, state="failed", updated_at=_now_iso(),
                                  error_code="invalid-response",
                                  error_detail=str(exc)[:300])
            return
        self.store.save_assessment({
            "id": assessment.assessment_id,
            "market": assessment.market,
            "symbol": assessment.symbol,
            "task_id": assessment.question_schema_id,
            "input_mode": "text" if assessment.documents else "candidate",
            "decision_at": assessment.decision_at.isoformat(),
            "as_of": assessment.as_of.isoformat(),
            "input_json": row["input_json"],
            "input_hash": input_hash(assessment),
            "coalescing_hash": coalescing_hash(assessment),
            "raw_json": json.dumps(result.raw) if result.raw is not None else None,
            "normalized_json": json.dumps(result.model_dump(mode="json")),
            "calibration_status": result.calibration_status,
            "elapsed_ms": result.elapsed_ms,
            "warnings_json": json.dumps(list(result.warnings)),
            "truncated": int(result.truncated),
            "created_at": row["created_at"],
            "rerun_of": row.get("rerun_of"),
        })
        self.store.update_job(job_id, state="succeeded", updated_at=_now_iso(),
                              error_code=None, error_detail=None)
        try:
            self._pending.get_nowait()
        except queue.Empty:
            pass

    def _mark_interrupted_or_failed(self, job_id: str, code: str, detail: str) -> None:
        job = self.store.get_job(job_id)
        if job is None or int(job.get("cancelled") or 0) or job.get("state") == "cancelled":
            return
        self.store.update_job(job_id, state="failed", updated_at=_now_iso(),
                              error_code=code, error_detail=detail[:300])
        try:
            self._pending.get_nowait()
        except queue.Empty:
            pass

    def _invoke_worker(self, assessment: LayaAssessmentInput) -> dict[str, Any]:
        payload = {
            "request_id": assessment.assessment_id,
            "task_id": assessment.question_schema_id,
            "context_text": assessment.context_text,
            "questions": [q.model_dump(mode="json") for q in assessment.questions],
            "timeout_s": self.timeout_s,
        }
        if self._worker_factory is not None:
            return self._worker_factory(payload)
        if self.fake:
            from ai.laya_worker import handle_request

            return handle_request(payload, backend=None, fake=True,
                                  raw_version="laya-app-v1-fake")
        from config import settings

        worker_python = (getattr(settings, "laya_worker_python", "") or "").strip()
        model_path = (getattr(settings, "laya_model_path", "") or "").strip()
        device = (getattr(settings, "laya_device", "") or "cpu").strip() or "cpu"
        if not worker_python or not Path(worker_python).exists():
            return {"request_id": payload["request_id"], "ok": False,
                    "error_code": "unavailable", "error_detail": "Worker unavailable",
                    "elapsed_ms": 0}
        if not model_path:
            return {"request_id": payload["request_id"], "ok": False,
                    "error_code": "missing-model", "error_detail": "No model provisioned",
                    "elapsed_ms": 0}
        # Fixed module arguments; never shell. Bounded deadline; supervisor may
        # terminate and replace the worker after a hard timeout.
        proc = subprocess.Popen(
            [worker_python, "-m", "ai.laya_worker",
             "--model-path", model_path, "--device", device],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, cwd=str(Path(__file__).resolve().parent.parent),
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None
            # Consume the ready line first (no model download on status paths).
            ready_line = proc.stdout.readline()
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
            deadline = time.monotonic() + self.timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    raise TimeoutError("Laya worker deadline exceeded")
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("Worker exited without responding")
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(response, dict) and response.get("request_id") == payload["request_id"]:
                    return response
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

    # ── policies ──────────────────────────────────────────────────────────
    def _seed_policies(self) -> None:
        seeds = (
            LayaPolicy(
                policy_id="laya-candidate-v1-default",
                version=1,
                task_id="candidate-review-v1",
                model_id="pinned",
                calibration_id="uncalibrated",
                markets=("us", "ph"),
                min_answer_confidence=0.0,
                require_alignment=("supports",),
                veto_on_material_conflict_yes_prob=0.5,
                min_evidence_score="weak",
            ),
            LayaPolicy(
                policy_id="laya-event-v1-default",
                version=1,
                task_id="event-review-v1",
                model_id="pinned",
                calibration_id="uncalibrated",
                markets=("us",),  # EDGAR evidence is US-only; PH books abstain
                veto_on_material_risk_yes_prob=0.6,
                veto_financing_min_confidence=0.35,
            ),
        )
        for seed in seeds:
            self.store.seed_policy(seed.policy_id, seed.version,
                                   seed.model_dump(mode="json"), _now_iso())

    def evaluate(
        self, assessment_id: str, policy_id: str, *,
        mode: str, candidate_id: str | None = None,
        book_id: str | None = None, run_id: str | None = None,
    ) -> dict[str, Any]:
        from core.laya_policy import evaluate_policy

        row = self.store.get_assessment(assessment_id)
        if row is None or not row.get("normalized_json"):
            raise ValueError("Assessment is not complete")
        result = LayaAssessmentResult.model_validate(json.loads(row["normalized_json"]))
        prow = self.store.get_policy(policy_id)
        if prow is None:
            raise ValueError("Unknown policy")
        policy = LayaPolicy.model_validate(json.loads(prow["payload_json"]))
        if policy.task_id != row["task_id"]:
            # A candidate policy on event answers (or vice versa) must never
            # silently produce a verdict: refuse explicitly. Callers convert
            # this to an abstain with the assessment linked.
            raise ValueError(
                f"Policy task {policy.task_id!r} does not match assessment task "
                f"{row['task_id']!r}")
        decision = evaluate_policy(
            result=result, policy=policy, mode=mode, market=row["market"],
            candidate_id=candidate_id, book_id=book_id, run_id=run_id,
        )
        self.store.save_decision({
            "assessment_id": decision.assessment_id,
            "policy_id": decision.policy_id,
            "candidate_id": decision.candidate_id,
            "book_id": decision.book_id,
            "run_id": decision.run_id,
            "mode": decision.mode,
            "outcome": decision.outcome,
            "reasons_json": json.dumps(list(decision.reasons)),
            "decision_hash": decision.decision_hash,
            "evaluated_at": decision.evaluated_at.isoformat(),
        })
        return decision.model_dump(mode="json")


class ConflictError(ValueError):
    pass


class QueueFullError(RuntimeError):
    pass


_service: LayaService | None = None


def get_service() -> LayaService:
    global _service
    if _service is None:
        from config import settings

        fake = os.environ.get("LAYA_FAKE", "") in ("1", "true", "yes")
        _service = LayaService(
            max_pending=int(getattr(settings, "laya_max_pending", 8) or 8),
            timeout_s=float(getattr(settings, "laya_timeout_seconds", 60.0) or 60.0),
            fake=fake,
        )
    return _service


def reset_service() -> None:
    global _service
    _service = None
