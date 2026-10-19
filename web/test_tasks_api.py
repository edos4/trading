"""HTTP + workflow checks for the Tasks (Kanban) tab.

The storage-backed tests need a disposable PostgreSQL database via
``TASKS_TEST_DATABASE_URL`` and skip otherwise; the page/nav/auth checks run
without a database.
"""
from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

DSN = os.environ.get("TASKS_TEST_DATABASE_URL")
needs_db = unittest.skipUnless(DSN, "set TASKS_TEST_DATABASE_URL to a disposable database")


class WebTasksApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patches = [patch("web.auth.settings"), patch("web.app.settings")]
        self.auth_s = self.patches[0].start()
        self.app_s = self.patches[1].start()
        for s in (self.auth_s, self.app_s):
            s.web_ui_password = "correct-horse"
            s.web_ui_username = "admin"
            s.web_ui_users = "ed:correct-horse"
            s.web_ui_secret_key = "test-secret-key"
            s.web_ui_https = False
            s.web_ui_session_hours = 12
            s.volume_gate_enabled = False

        self.schema = None
        import config

        self._prev_url = config.settings.task_database_url
        self._prev_schema = config.settings.task_schema
        if DSN:
            from core.task_db import migrate

            self.schema = "tasks_test_" + uuid.uuid4().hex
            migrate(DSN, self.schema)
            config.settings.task_database_url = DSN
            config.settings.task_schema = self.schema
            import core.task_store as ts

            ts._store = None

        from fastapi.testclient import TestClient
        from web.app import create_app

        self.client = TestClient(create_app())
        self._login("admin")

    def tearDown(self) -> None:
        try:
            import config
            import core.task_store as ts

            ts._store = None
            config.settings.task_database_url = self._prev_url
            config.settings.task_schema = self._prev_schema
            if self.schema:
                from psycopg import sql

                from core.task_db import connection

                with connection(DSN, self.schema) as con:
                    con.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(self.schema)))
        finally:
            for patch_obj in self.patches:
                patch_obj.stop()

    def _login(self, user: str) -> None:
        self.client.post("/login", data={
            "username": user, "password": "correct-horse", "next": "/"})

    # ── no database required ───────────────────────────────────────────────
    def test_page_renders(self) -> None:
        r = self.client.get("/tasks")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Tasks", r.text)

    def test_nav_link_present_on_other_pages(self) -> None:
        r = self.client.get("/laya")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/tasks"', r.text)

    def test_unauthorized_api(self) -> None:
        from fastapi.testclient import TestClient
        import web.app as appmod

        anon = TestClient(appmod.create_app())
        self.assertEqual(anon.get("/api/tasks/board").status_code, 401)

    def test_degradation_when_database_unreachable(self) -> None:
        import config
        import core.task_store as ts

        prev = config.settings.task_database_url
        ts._store = None
        config.settings.task_database_url = "postgresql://x@/nope?host=/tmp/no-such-sock"
        try:
            r = self.client.get("/tasks")
            self.assertEqual(r.status_code, 200)
            self.assertIn("setup_tasks_db.py", r.text)
            # Other tabs keep serving.
            self.assertEqual(self.client.get("/laya").status_code, 200)
        finally:
            config.settings.task_database_url = prev
            ts._store = None

    # ── storage-backed ─────────────────────────────────────────────────────
    @needs_db
    def test_board_roundtrip_and_authorship(self) -> None:
        lst = self.client.post("/api/tasks/lists", json={"title": "Backlog"}).json()
        card = self.client.post("/api/tasks/cards", json={
            "list_id": lst["id"], "title": "First", "description": "d"}).json()
        self.assertEqual(card["author"], "admin")
        self.client.post(f"/api/tasks/cards/{card['id']}/subtasks", json={"text": "step"})
        self.client.post(f"/api/tasks/cards/{card['id']}/comments", json={"body": "hi"})

        # A second named user is attributed distinctly.
        self._login("ed")
        comment = self.client.post(
            f"/api/tasks/cards/{card['id']}/comments", json={"body": "from ed"}).json()
        self.assertEqual(comment["author"], "ed")

        board = self.client.get("/api/tasks/board").json()
        cards = board["lists"][0]["cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["author"], "admin")
        self.assertEqual(cards[0]["comment_count"], 2)
        self.assertEqual(len(cards[0]["subtasks"]), 1)

        # Editing a card leaves its author unchanged.
        patched = self.client.patch(f"/api/tasks/cards/{card['id']}",
                                    json={"title": "Renamed"}).json()
        self.assertEqual(patched["author"], "admin")

    @needs_db
    def test_comment_crud_and_threading(self) -> None:
        lst = self.client.post("/api/tasks/lists", json={"title": "L"}).json()
        card = self.client.post("/api/tasks/cards", json={"list_id": lst["id"], "title": "T"}).json()
        other = self.client.post("/api/tasks/cards", json={"list_id": lst["id"], "title": "U"}).json()

        top = self.client.post(f"/api/tasks/cards/{card['id']}/comments",
                               json={"body": "top"}).json()
        reply = self.client.post(f"/api/tasks/cards/{card['id']}/comments",
                                 json={"body": "reply", "parent_id": top["id"]}).json()
        self.assertEqual(reply["parent_id"], top["id"])

        # No third level.
        deep = self.client.post(f"/api/tasks/cards/{card['id']}/comments",
                                json={"body": "deep", "parent_id": reply["id"]})
        self.assertIn(deep.status_code, (409, 422))
        # Cross-card parent rejected.
        cross = self.client.post(f"/api/tasks/cards/{other['id']}/comments",
                                 json={"body": "cross", "parent_id": top["id"]})
        self.assertIn(cross.status_code, (409, 422))

        edited = self.client.patch(f"/api/tasks/comments/{reply['id']}",
                                   json={"body": "reply edited"}).json()
        self.assertEqual(edited["body"], "reply edited")

        listing = self.client.get(f"/api/tasks/cards/{card['id']}/comments").json()
        self.assertEqual(len(listing["comments"]), 2)

        # Deleting the top removes the reply too.
        self.assertEqual(self.client.delete(f"/api/tasks/comments/{top['id']}").status_code, 200)
        remaining = self.client.get(f"/api/tasks/cards/{card['id']}/comments").json()
        self.assertEqual(len(remaining["comments"]), 0)

    @needs_db
    def test_move_and_validation(self) -> None:
        a = self.client.post("/api/tasks/lists", json={"title": "A"}).json()
        b = self.client.post("/api/tasks/lists", json={"title": "B"}).json()
        c1 = self.client.post("/api/tasks/cards", json={"list_id": a["id"], "title": "one"}).json()
        c2 = self.client.post("/api/tasks/cards", json={"list_id": a["id"], "title": "two"}).json()

        self.client.post(f"/api/tasks/cards/{c2['id']}/move",
                         json={"list_id": a["id"], "before_id": c1["id"]})
        titles = [c["title"] for c in self.client.get("/api/tasks/board").json()["lists"][0]["cards"]]
        self.assertEqual(titles, ["two", "one"])

        self.client.post(f"/api/tasks/cards/{c2['id']}/move", json={"list_id": b["id"]})
        board = {l["title"]: [c["title"] for c in l["cards"]]
                 for l in self.client.get("/api/tasks/board").json()["lists"]}
        self.assertEqual(board["A"], ["one"])
        self.assertEqual(board["B"], ["two"])

        # Errors.
        self.assertEqual(self.client.post(
            "/api/tasks/cards", json={"list_id": "nope", "title": "x"}).status_code, 404)
        self.assertEqual(self.client.post(
            "/api/tasks/lists", json={"title": "   "}).status_code, 422)
        # A non-empty list cannot be deleted.
        self.assertEqual(self.client.delete(f"/api/tasks/lists/{a['id']}").status_code, 409)

    @needs_db
    def test_export_includes_comments(self) -> None:
        lst = self.client.post("/api/tasks/lists", json={"title": "L"}).json()
        card = self.client.post("/api/tasks/cards", json={"list_id": lst["id"], "title": "T"}).json()
        self.client.post(f"/api/tasks/cards/{card['id']}/comments", json={"body": "note"})
        export = self.client.get("/api/tasks/export").json()
        comments = export["lists"][0]["cards"][0]["comments"]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["author"], "admin")


if __name__ == "__main__":
    unittest.main()
