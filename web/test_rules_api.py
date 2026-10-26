"""HTTP tests for the Builder tab and /api/rules."""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

DSN = os.environ.get("RULES_TEST_DATABASE_URL")
needs_db = unittest.skipUnless(
    DSN, "set RULES_TEST_DATABASE_URL to a disposable database")


def payload(name="indicator1", attachment=None):
    body = {
        "schema_version": 1,
        "name": name,
        "timeframe": "1d",
        "direction": "long",
        "root": {"kind": "group", "op": "AND", "children": [{
            "kind": "condition",
            "left": {"source": "indicator", "id": "close"},
            "operator": "gt",
            "right": {"source": "value", "value": 1},
        }]},
        "exit": {"on_false": True, "stop": {"kind": "percent", "percent": 5},
                 "target": {"kind": "r_multiple", "r": 2}},
    }
    if attachment is not None:
        body["attachments"] = {"patternsv2": attachment}
    return body


def candles(n=140):
    from data.tv_client import OHLCVCandle

    out = []
    for index in range(n):
        stamp = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
        value = 100.0 + index * 0.2
        out.append(OHLCVCandle(open=value, high=value + 1, low=value - 1,
                               close=value, volume=1_000_000.0, timestamp=stamp))
    return out


class WebRulesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patches = [patch("web.auth.settings"), patch("web.app.settings")]
        self.auth_s = self.patches[0].start()
        self.app_s = self.patches[1].start()
        for settings in (self.auth_s, self.app_s):
            settings.web_ui_password = "correct-horse"
            settings.web_ui_username = "admin"
            settings.web_ui_users = ""
            settings.web_ui_secret_key = "test-secret-key"
            settings.web_ui_https = False
            settings.web_ui_session_hours = 12
            settings.volume_gate_enabled = False
            settings.tasks_enabled = False

        self.schema = None
        import config

        self._prev_url = config.settings.rules_database_url
        self._prev_schema = config.settings.rules_schema
        if DSN:
            from core.rules_db import migrate

            self.schema = "rules_test_" + uuid.uuid4().hex
            migrate(DSN, self.schema)
            config.settings.rules_database_url = DSN
            config.settings.rules_schema = self.schema
            import core.rules_store as rs

            rs._store = None

        from web.app import create_app

        self.client = TestClient(create_app())
        self.client.post("/login", data={
            "username": "admin", "password": "correct-horse", "next": "/"})

    def tearDown(self) -> None:
        try:
            import config
            import core.rules_store as rs

            rs._store = None
            config.settings.rules_database_url = self._prev_url
            config.settings.rules_schema = self._prev_schema
            if self.schema:
                from psycopg import sql

                from core.rules_db import connection

                with connection(DSN, self.schema) as con:
                    con.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(self.schema)))
        finally:
            for patch_obj in self.patches:
                patch_obj.stop()

    # ── no database required ───────────────────────────────────────────────
    def test_page_renders_and_nav_link(self) -> None:
        response = self.client.get("/builder")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Builder", response.text)
        self.assertIn('href="/builder"', response.text)

    def test_unauthorized_api(self) -> None:
        import web.app as appmod

        anon = TestClient(appmod.create_app())
        self.assertEqual(anon.get("/api/rules").status_code, 401)

    def test_rule_start_forces_indicators_only(self) -> None:
        with patch("web.app.paper_books.start", return_value=None) as start:
            response = self.client.post("/api/paper/start", json={
                "market": "us", "n_symbols": 5, "rule_id": "rule-1",
                "rule_filter_id": "rule-2", "rule_filter_mode": "shadow",
                "rule_filter_position": "after",
            })
        self.assertEqual(response.status_code, 200, response.text)
        kwargs = start.call_args.kwargs
        self.assertTrue(kwargs["indicators_only"])
        self.assertFalse(kwargs["pattern_only"])
        self.assertFalse(kwargs["patterns_v2"])
        self.assertEqual(kwargs["rule_id"], "rule-1")
        self.assertEqual(kwargs["rule_filter_id"], "rule-2")
        self.assertEqual(kwargs["rule_filter_mode"], "shadow")
        self.assertEqual(kwargs["rule_filter_position"], "after")

    def test_same_rule_cannot_be_its_own_filter(self) -> None:
        with patch("web.app.paper_books.start", return_value=None) as start:
            response = self.client.post("/api/paper/start", json={
                "market": "us", "n_symbols": 5, "rule_id": "same",
                "rule_filter_id": "same", "rule_filter_mode": "filter",
            })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("own filter", response.text)
        start.assert_not_called()

    def test_dii_catalog_and_validation_without_storage(self):
        response = self.client.get("/api/rules/catalog")
        self.assertEqual(response.status_code, 200)
        entries = [item for item in response.json()["indicators"] if item["id"] == "directional_imbalance_index"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["outputs"], ["bulls", "bears", "up", "down", "upper", "lower"])
        self.assertEqual([(p["name"], p["default"], p["min"], p["max"]) for p in entry["params"]],
                         [("length", 10, 1, 250), ("period", 70, 1, 250)])
        body = payload("DII bullish context")
        left = body["root"]["children"][0]["left"]
        left.update(id="directional_imbalance_index", params={"length": 10, "period": 70}, field="bulls")
        body["root"]["children"][0]["right"]["value"] = 50
        self.assertEqual(self.client.post("/api/rules/validate", json=body).status_code, 200)
        left["field"] = "bad"
        self.assertEqual(self.client.post("/api/rules/validate", json=body).status_code, 400)
        left["field"] = "bulls"
        left["params"]["period"] = 251
        self.assertEqual(self.client.post("/api/rules/validate", json=body).status_code, 400)

    @needs_db
    def test_dii_save_load_preview(self):
        body = payload("DII context")
        body["root"]["children"][0]["left"].update(id="directional_imbalance_index", field="bulls")
        body["root"]["children"][0]["right"]["value"] = 50
        saved = self.client.post("/api/rules", json={"name": body["name"], "payload": body})
        self.assertEqual(saved.status_code, 201, saved.text)
        rule_id = saved.json()["id"]
        loaded = self.client.get(f"/api/rules/{rule_id}")
        self.assertEqual(loaded.status_code, 200, loaded.text)
        with patch("core.rule_backtest.load_candles", return_value=candles()):
            preview = self.client.post("/api/rules/preview", json={"rule_id": rule_id, "symbol": "TEST", "bars": 5})
        self.assertEqual(preview.status_code, 200, preview.text)
        from analysis.rule_catalog import compute
        import pandas as pd
        expected = compute("directional_imbalance_index", pd.DataFrame([dict(open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume) for c in candles()]), {})["bulls"].iloc[-1]
        self.assertEqual(preview.json()["bars"][-1]["conditions"][0]["left"], expected)

    # ── storage-backed ─────────────────────────────────────────────────────
    @needs_db
    def test_catalog_and_crud(self) -> None:
        catalog = self.client.get("/api/rules/catalog")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        ids = {item["id"] for item in catalog.json()["indicators"]}
        self.assertIn("rsi", ids)
        self.assertIn("adx", ids)
        patterns = {item["id"] for item in catalog.json()["patternsv2_patterns"]}
        self.assertIn("double_bottom", patterns)
        self.assertIn("symmetrical_triangle", patterns)

        created = self.client.post("/api/rules", json={
            "name": "indicator1",
            "payload": payload("indicator1", {
                "pattern": "double_bottom", "position": "before",
                "mode": "filter"}),
        })
        self.assertEqual(created.status_code, 201, created.text)
        row = created.json()
        self.assertEqual(row["name"], "indicator1")
        self.assertEqual(row["generation"], 0)
        self.assertEqual(
            row["payload"]["attachments"]["patternsv2"]["pattern"],
            "double_bottom")

        listed = self.client.get("/api/rules").json()["rules"]
        self.assertEqual([item["name"] for item in listed], ["indicator1"])
        self.assertEqual(listed[0]["patternsv2"]["pattern"], "double_bottom")

        fetched = self.client.get(f"/api/rules/{row['id']}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["payload"]["name"], "indicator1")

        updated = self.client.post("/api/rules", json={
            "name": "indicator1", "payload": payload(), "rule_id": row["id"],
            "expected_generation": 0,
        })
        self.assertEqual(updated.status_code, 201, updated.text)
        self.assertEqual(updated.json()["generation"], 1)

        stale = self.client.post("/api/rules", json={
            "name": "indicator1", "payload": payload(), "rule_id": row["id"],
            "expected_generation": 0,
        })
        self.assertEqual(stale.status_code, 409, stale.text)

        duplicate = self.client.post("/api/rules", json={"name": "indicator1",
                                                         "payload": payload()})
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

        deleted = self.client.delete(f"/api/rules/{row['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.client.get(f"/api/rules/{row['id']}").status_code, 400)

    @needs_db
    def test_validation_and_preview(self) -> None:
        bad = self.client.post("/api/rules/validate", json={
            "name": "bad",
            "payload": {**payload("bad"), "root": {"kind": "group", "op": "AND",
                                                   "children": []}},
        })
        self.assertEqual(bad.status_code, 400, bad.text)
        self.assertFalse(bad.json()["ok"])
        self.assertTrue(bad.json()["errors"])

        unknown = payload("unknown")
        unknown["root"]["children"][0]["left"]["id"] = "nope"
        bad_id = self.client.post("/api/rules/validate",
                                  json={"name": "unknown", "payload": unknown})
        self.assertEqual(bad_id.status_code, 400, bad_id.text)
        self.assertIn("nope", bad_id.text)

        good = self.client.post("/api/rules/validate", json={"name": "ok",
                                                             "payload": payload("ok")})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertTrue(good.json()["ok"])

        import core.rule_backtest as rb

        with patch.object(rb, "load_candles", lambda symbol, market="us": candles()):
            preview = self.client.post("/api/rules/preview", json={
                "payload": payload("preview"), "symbol": "TEST",
                "market": "us", "bars": 20,
            })
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertEqual(len(body["bars"]), 20)
        self.assertIn("matched_count", body)
        self.assertTrue(body["bars"][-1]["conditions"])

        created = self.client.post("/api/rules", json={"name": "replay",
                                                       "payload": payload("replay")})
        rule_id = created.json()["id"]
        with patch.object(rb, "load_candles", lambda symbol, market="us": candles()):
            replay = self.client.post(f"/api/rules/{rule_id}/backtest", json={
                "symbols": "TEST", "market": "us", "position_notional": 10000,
            })
        self.assertEqual(replay.status_code, 200, replay.text)
        replay_body = replay.json()
        self.assertIn("trades", replay_body)
        self.assertIn("matches", replay_body)
        self.assertIn("max_dd", replay_body["summary"])
        self.assertEqual(replay_body["summary"]["max_dd"],
                         replay_body["summary"]["max_drawdown"])
