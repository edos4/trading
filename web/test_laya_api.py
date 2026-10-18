"""HTTP auth + workflow checks for the Laya tab."""
from __future__ import annotations

import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class WebLayaApiTests(unittest.TestCase):
    TIMEOUT_SECONDS = 120

    def _on_timeout(self, signum, frame) -> None:
        raise TimeoutError(f"Laya API test exceeded {self.TIMEOUT_SECONDS}s")

    def setUp(self) -> None:
        signal.signal(signal.SIGALRM, self._on_timeout)
        signal.alarm(self.TIMEOUT_SECONDS)
        try:
            self.patches = [
                patch("web.auth.settings"),
                patch("web.app.settings"),
            ]
            self.auth_s = self.patches[0].start()
            self.app_s = self.patches[1].start()
            for s in (self.auth_s, self.app_s):
                s.web_ui_password = "correct-horse"
                s.web_ui_username = "admin"
                s.web_ui_secret_key = "test-secret-key"
                s.web_ui_https = False
                s.web_ui_session_hours = 12
                s.volume_gate_enabled = False

            # Isolated Laya service: enabled + fake inference + tmp store.
            import config
            import core.laya_service as svc
            import core.laya_store as st

            self._tmp = Path(tempfile.mkdtemp(prefix="laya-web-"))
            self._prev_enabled = config.settings.laya_enabled
            self._prev_store = config.settings.laya_store_path
            config.settings.laya_enabled = True
            config.settings.laya_store_path = str(self._tmp / "laya.sqlite3")
            st._store = None
            svc.reset_service()
            import os

            os.environ["LAYA_FAKE"] = "1"
            svc.reset_service()

            from web.app import create_app

            from fastapi.testclient import TestClient

            self.client = TestClient(create_app())
            self.client.post("/login", data={
                "username": "admin", "password": "correct-horse", "next": "/"})
        except BaseException:
            signal.alarm(0)
            raise

    def tearDown(self) -> None:
        try:
            import config
            import core.laya_service as svc
            import core.laya_store as st
            import os

            os.environ.pop("LAYA_FAKE", None)
            config.settings.laya_enabled = self._prev_enabled
            config.settings.laya_store_path = self._prev_store
            st._store = None
            svc.reset_service()
            for p in self.patches:
                p.stop()
        finally:
            signal.alarm(0)

    def test_page_renders_and_nav(self) -> None:
        r = self.client.get("/laya")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Laya", r.text)

    def test_unauthorized_api(self) -> None:
        from fastapi.testclient import TestClient
        import web.app as appmod

        anon = TestClient(appmod.create_app())
        r = anon.get("/api/laya/status")
        self.assertEqual(r.status_code, 401)

    def test_status_tasks_no_side_effects(self) -> None:
        s = self.client.get("/api/laya/status")
        self.assertEqual(s.status_code, 200)
        t = self.client.get("/api/laya/tasks")
        self.assertEqual(t.status_code, 200)
        ids = [task["task_id"] for task in t.json()["tasks"]]
        self.assertIn("event-review-v1", ids)
        self.assertIn("candidate-review-v1", ids)

    def test_text_assessment_roundtrip(self) -> None:
        payload = {
            "market": "us", "symbol": "AAPL", "exchange": "NASDAQ",
            "task_id": "event-review-v1",
            "source_text": "EXAMPLE Corp priced a secondary offering of shares.",
            "source_title": "Offering", "publisher": "test",
            "idempotency_key": "web-test-1",
            "assessment_id": "asm-web-test-1",
        }
        r = self.client.post("/api/laya/assessments", json=payload)
        self.assertEqual(r.status_code, 202, r.text)
        job_id = r.json()["job_id"]
        assessment_id = r.json()["assessment_id"]
        # Duplicate key + same input returns the existing job.
        r2 = self.client.post("/api/laya/assessments", json=payload)
        self.assertEqual(r2.status_code, 202)
        self.assertEqual(r2.json()["job_id"], job_id)
        # Poll to terminal.
        terminal = None
        for _ in range(100):
            job = self.client.get(f"/api/laya/jobs/{job_id}").json()
            if job["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
                terminal = job
                break
            time.sleep(0.05)
        self.assertIsNotNone(terminal)
        self.assertEqual(terminal["state"], "succeeded")
        detail = self.client.get(f"/api/laya/assessments/{assessment_id}")
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(body["symbol"], "AAPL")
        self.assertTrue(body["result"]["answers"])
        # Every assessment identifies market/evidence/model/calibration.
        self.assertIn("market", body["input"])
        self.assertIn("content_hash", body["input"])
        self.assertIn("question_schema_id", body["input"])
        # Export is versioned JSON.
        exp = self.client.get(f"/api/laya/assessments/{assessment_id}/export")
        self.assertEqual(exp.status_code, 200)
        self.assertEqual(exp.json()["export_version"], "laya-export-v1")
        # History lists it; reload path uses the saved record.
        hist = self.client.get("/api/laya/assessments?market=us&symbol=AAPL")
        self.assertEqual(hist.status_code, 200)
        self.assertGreaterEqual(hist.json()["total"], 1)

    def test_schema_error_is_422(self) -> None:
        r = self.client.post("/api/laya/assessments", json={
            "market": "us", "symbol": "AAPL", "task_id": "event-review-v1",
            "source_text": "   ", "idempotency_key": "web-bad-1",
        })
        self.assertEqual(r.status_code, 422)

    def test_unknown_ids_are_404(self) -> None:
        self.assertEqual(
            self.client.get("/api/laya/jobs/nope").status_code, 404)
        self.assertEqual(
            self.client.get("/api/laya/assessments/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
