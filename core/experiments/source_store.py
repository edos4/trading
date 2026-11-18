"""Durable URL-generation jobs and their versioned source documents."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from core.experiments.store import Conflict, ExperimentStore, packed, unpacked

ACTIVE = {"queued", "preparing", "generating"}
TERMINAL = {"completed", "failed", "blocked", "interrupted", "cancelled"}
LEASE = 90


class SourceStore:
    def __init__(self, store: ExperimentStore):
        self.store = store
        self.root = store.root / "sources"
        with store.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS source_imports (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, lease_until REAL NOT NULL,
                    attempt INTEGER NOT NULL, source_id TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS generated_sources (
                    id TEXT PRIMARY KEY, import_id TEXT NOT NULL UNIQUE, payload BLOB NOT NULL);
            """)

    @staticmethod
    def _recover(con) -> None:
        con.execute("UPDATE source_imports SET state='interrupted', stage='interrupted', "
                    "message='Generation was interrupted. Retry to continue from saved evidence.', "
                    "error='Worker lease expired', updated=? WHERE state IN ('queued','preparing','generating') "
                    "AND lease_until < ?", (time.time(), time.time()))

    def create(self, url: str) -> tuple[dict, bool]:
        identity = "url-" + hashlib.sha256(url.encode()).hexdigest()[:24]
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._recover(con)
            row = con.execute("SELECT * FROM source_imports WHERE url=?", (url,)).fetchone()
            if row:
                return dict(row), False
            if con.execute("SELECT count(*) FROM source_imports WHERE state IN ('queued','preparing','generating')").fetchone()[0] >= 8:
                raise Conflict("Eight URLs are already pending; wait for one to finish")
            now = time.time()
            con.execute("INSERT INTO source_imports VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (identity, url, "queued", "queued", "Waiting to prepare the source video", now, now,
                         now + LEASE, 1, None, None))
            return dict(con.execute("SELECT * FROM source_imports WHERE id=?", (identity,)).fetchone()), True

    def get(self, identity: str) -> dict:
        with self.store.connect() as con:
            self._recover(con)
            row = con.execute("SELECT * FROM source_imports WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise KeyError("Unknown URL experiment")
            return dict(row)

    def listing(self) -> list[dict]:
        with self.store.connect() as con:
            self._recover(con)
            return [dict(row) for row in con.execute("SELECT * FROM source_imports ORDER BY created DESC")]

    def retry(self, identity: str) -> dict:
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._recover(con)
            row = con.execute("SELECT * FROM source_imports WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise KeyError("Unknown URL experiment")
            if row["state"] not in TERMINAL - {"completed"}:
                raise Conflict("Only failed, blocked or interrupted generation can be retried")
            now = time.time()
            con.execute("UPDATE source_imports SET state='queued', stage='queued', message='Retry queued', "
                        "updated=?, lease_until=?, attempt=attempt+1, error=NULL WHERE id=?",
                        (now, now + LEASE, identity))
        return self.get(identity)

    def claim(self, identity: str) -> int | None:
        with self.store.connect() as con:
            row = con.execute("UPDATE source_imports SET state='preparing', stage='preparing', "
                              "updated=?, lease_until=? WHERE id=? AND state='queued' RETURNING attempt",
                              (time.time(), time.time() + LEASE, identity)).fetchone()
            return row[0] if row else None

    def update(self, identity: str, *, attempt: int, state: str, stage: str, message: str, error: str | None = None) -> bool:
        with self.store.connect() as con:
            return bool(con.execute("UPDATE source_imports SET state=?, stage=?, message=?, error=?, "
                                    "updated=?, lease_until=? WHERE id=? AND attempt=? AND state IN ('preparing','generating')",
                                    (state, stage, message, error, time.time(), time.time() + LEASE, identity, attempt)).rowcount)

    def heartbeat(self, identity: str, attempt: int) -> bool:
        with self.store.connect() as con:
            return bool(con.execute("UPDATE source_imports SET lease_until=?, updated=? WHERE id=? "
                                    "AND attempt=? AND state IN ('preparing','generating')",
                                    (time.time() + LEASE, time.time(), identity, attempt)).rowcount)

    def cancel(self, identity: str) -> dict:
        with self.store.connect() as con:
            con.execute("UPDATE source_imports SET state='cancelled', stage='cancelled', "
                        "message='Cancelled; saved evidence is retained', updated=? WHERE id=? "
                        "AND state IN ('queued','preparing','generating')", (time.time(), identity))
        return self.get(identity)

    def finish(self, identity: str, source: dict, *, attempt: int, existing: bool = False) -> bool:
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT state,attempt FROM source_imports WHERE id=?", (identity,)).fetchone()
            if row is None or row["state"] not in {"preparing", "generating"} or row["attempt"] != attempt:
                return False
            if not existing:
                con.execute("INSERT INTO generated_sources VALUES(?,?,?)",
                            (source["id"], identity, packed(source)))
            ready = source.get("availability") == "ready"
            message = ("Reused the archived experiment" if existing else
                       "Experiment generated; runnable rules are ready" if ready else
                       "Experiment generated; inspect its plan for unresolved or unsupported rules")
            con.execute("UPDATE source_imports SET state='completed', stage='completed', message=?, "
                        "source_id=?, updated=?, error=NULL WHERE id=?",
                        (message, source["id"], time.time(), identity))
            return True

    def sources(self) -> tuple[dict, ...]:
        with self.store.connect() as con:
            return tuple(unpacked(row[0]) for row in con.execute("SELECT payload FROM generated_sources ORDER BY rowid"))

    def directory(self, identity: str) -> Path:
        self.get(identity)
        return self.root / identity
