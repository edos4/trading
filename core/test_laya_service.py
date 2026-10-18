"""Laya store + service checks (docs/laya.md section 15)."""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


def _tmp_store():
    from core.laya_store import LayaStore

    tmp = Path(tempfile.mkdtemp(prefix="laya-store-"))
    return LayaStore(tmp / "laya.sqlite3")


class LayaStoreTests(unittest.TestCase):
    def test_idempotency_conflict_and_rerun_link(self) -> None:
        store = _tmp_store()
        job = {"id": "job-1", "idempotency_key": "k-1", "input_hash": "0" * 64,
               "assessment_id": "asm-1", "state": "queued",
               "created_at": "2026-01-01T00:00:00+00:00",
               "updated_at": "2026-01-01T00:00:00+00:00",
               "error_code": None, "error_detail": None, "attempt": 1,
               "cancelled": 0, "previous_attempt_id": "asm-0"}
        store.create_job(job)
        same = store.create_job(dict(job, id="job-2"))
        self.assertEqual(same["id"], "job-1")
        row = {"id": "asm-1", "market": "us", "symbol": "AAPL",
               "task_id": "event-review-v1", "input_mode": "text",
               "decision_at": "2026-01-01T00:00:00+00:00",
               "as_of": "2026-01-01T00:00:00+00:00",
               "input_json": "{}", "input_hash": "0" * 64,
               "coalescing_hash": "1" * 64, "raw_json": None,
               "normalized_json": None, "calibration_status": "uncalibrated",
               "elapsed_ms": 0, "warnings_json": "[]", "truncated": 0,
               "created_at": "2026-01-01T00:00:00+00:00", "rerun_of": "asm-0"}
        store.save_assessment(row)
        self.assertEqual(store.get_assessment("asm-1")["rerun_of"], "asm-0")
        rows, total = store.list_assessments(market="us", limit=10, offset=0)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["id"], "asm-1")

    def test_pagination(self) -> None:
        store = _tmp_store()
        for i in range(3):
            store.save_assessment(
                {"id": f"asm-{i}", "market": "us", "symbol": "AAPL",
                 "task_id": "event-review-v1", "input_mode": "text",
                 "decision_at": "2026-01-01T00:00:00+00:00",
                 "as_of": "2026-01-01T00:00:00+00:00",
                 "input_json": "{}", "input_hash": "0" * 64,
                 "coalescing_hash": f"{i:064d}".replace(" ", "0")[-64:],
                 "raw_json": None, "normalized_json": None,
                 "calibration_status": "uncalibrated", "elapsed_ms": 0,
                 "warnings_json": "[]", "truncated": 0,
                 "created_at": f"2026-01-0{i + 1}T00:00:00+00:00", "rerun_of": None})
        rows, total = store.list_assessments(limit=2, offset=0)
        self.assertEqual(total, 3)
        self.assertEqual(len(rows), 2)


class LayaServiceTests(unittest.TestCase):
    def _service(self):
        from core.laya_service import LayaService

        return LayaService(store=_tmp_store(), max_pending=4, timeout_s=10.0, fake=True)

    def _assessment(self, aid: str):
        from core.laya_context import build_text_input

        assessment, _ = build_text_input(
            assessment_id=aid, market="us", symbol="AAPL", exchange="NASDAQ",
            source_text="EXAMPLE Corp priced an offering of shares.",
            publication_time="2026-01-10T12:00:00+00:00",
            decision_at="2026-01-11T12:00:00+00:00")
        return assessment

    def test_submit_idempotent_and_recoverable(self) -> None:
        service = self._service()
        assessment = self._assessment("asm-svc-1")
        job, created = service.submit(assessment, idempotency_key="idem-1")
        self.assertTrue(created)
        for _ in range(100):
            current = service.store.get_job(job["id"])
            if current and current["state"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        current = service.store.get_job(job["id"])
        self.assertEqual(current["state"], "succeeded")
        job2, created2 = service.submit(assessment, idempotency_key="idem-1")
        self.assertFalse(created2)
        self.assertEqual(job2["id"], job["id"])
        # Reload after "restart": same saved result without calling the model.
        row = service.store.get_assessment("asm-svc-1")
        self.assertTrue(row["normalized_json"])

    def test_idempotency_conflict(self) -> None:
        from core.laya_service import ConflictError

        service = self._service()
        service.submit(self._assessment("asm-a"), idempotency_key="idem-x")
        with self.assertRaises(ConflictError):
            service.submit(self._assessment("asm-b"), idempotency_key="idem-x")

    def test_cancel_terminal(self) -> None:
        from core.laya_service import LayaService

        store = _tmp_store()

        def _never(_payload):
            time.sleep(5)
            return {"ok": True, "answers": []}

        service = LayaService(store=store, max_pending=4, timeout_s=10.0,
                              worker_factory=_never)
        assessment = self._assessment("asm-cancel-1")
        job, _ = service.submit(assessment, idempotency_key="idem-cancel")
        cancelled = service.cancel(job["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        again = service.cancel(job["id"])
        self.assertEqual(again["state"], "cancelled")

    def test_queue_saturation(self) -> None:
        from core.laya_service import LayaService, QueueFullError

        store = _tmp_store()

        def _slow(_payload):
            time.sleep(2)
            return {"ok": False, "error_code": "inference-failed",
                    "error_detail": "slow", "elapsed_ms": 0}

        service = LayaService(store=store, max_pending=1, timeout_s=10.0,
                              worker_factory=_slow)
        service.submit(self._assessment("asm-q-1"), idempotency_key="q-1")
        time.sleep(0.2)
        with self.assertRaises(QueueFullError):
            service.submit(self._assessment("asm-q-2"), idempotency_key="q-2")

    def test_status_no_side_effects(self) -> None:
        service = self._service()
        status = service.status()
        self.assertIn("state", status)

    def test_seed_policy_migrates_on_version_change(self) -> None:
        store = _tmp_store()
        store.seed_policy("pol-x", 1, {"threshold": "a"}, "2026-01-01T00:00:00+00:00")
        # Same version never clobbers an operator-edited row.
        store.seed_policy("pol-x", 1, {"threshold": "b"}, "2026-01-02T00:00:00+00:00")
        row = store.get_policy("pol-x")
        self.assertEqual(row["version"], 1)
        import json as _json

        self.assertEqual(_json.loads(row["payload_json"]), {"threshold": "a"})
        # A version bump migrates the thresholds.
        store.seed_policy("pol-x", 2, {"threshold": "c"}, "2026-01-03T00:00:00+00:00")
        row = store.get_policy("pol-x")
        self.assertEqual(row["version"], 2)
        self.assertEqual(_json.loads(row["payload_json"]), {"threshold": "c"})

    def _completed_assessment(self, service, aid: str = "asm-eval-1") -> str:
        assessment = self._assessment(aid)
        job, _ = service.submit(assessment, idempotency_key=f"idem-{aid}")
        for _ in range(100):
            current = service.store.get_job(job["id"])
            if current and current["state"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(service.store.get_job(job["id"])["state"], "succeeded")
        return aid

    def test_evaluate_matching_task_records_decision(self) -> None:
        from core.laya_contracts import LayaPolicy

        service = self._service()
        aid = self._completed_assessment(service)
        policy = LayaPolicy(policy_id="pol-evt", task_id="event-review-v1",
                            model_id="pinned", markets=("us", "ph"))
        service.store.seed_policy(policy.policy_id, policy.version,
                                  policy.model_dump(mode="json"),
                                  "2026-01-01T00:00:00+00:00")
        decision = service.evaluate(aid, "pol-evt", mode="research", book_id="tab")
        # Benign event answers (default fake: earnings/positive/no-risk) carry
        # no danger signal, so the veto-only gate allows with reasons.
        self.assertEqual(decision["outcome"], "allow")
        self.assertEqual(decision["mode"], "research")
        saved = service.store.decisions_for(aid)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["outcome"], "allow")

    def test_evaluate_task_mismatch_refuses(self) -> None:
        from core.laya_contracts import LayaPolicy

        service = self._service()
        aid = self._completed_assessment(service, "asm-eval-2")
        policy = LayaPolicy(policy_id="pol-cand", task_id="candidate-review-v1",
                            model_id="pinned", markets=("us", "ph"))
        service.store.seed_policy(policy.policy_id, policy.version,
                                  policy.model_dump(mode="json"),
                                  "2026-01-01T00:00:00+00:00")
        with self.assertRaises(ValueError):
            service.evaluate(aid, "pol-cand", mode="filter")
        self.assertEqual(service.store.decisions_for(aid), [])


if __name__ == "__main__":
    unittest.main()
