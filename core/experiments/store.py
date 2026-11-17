"""Transactional, resumable-view storage for isolated historical experiments.

SQLite is local run storage, never a daily-bar provider or a fallback for the
history PostgreSQL database. One batch owns the worker lease across processes.
Expired work is interrupted, never silently rerun against changed history.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import threading
import uuid
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


TERMINAL = {"completed", "completed-with-blockers", "failed", "cancelled", "interrupted", "blocked"}
LEASE_SECONDS = 180


class Conflict(ValueError):
    """A second batch or inconsistent idempotency request was submitted."""


def packed(value: object) -> bytes:
    return zlib.compress(json.dumps(value, allow_nan=False, separators=(",", ":")).encode())


def unpacked(value: bytes) -> object:
    return json.loads(zlib.decompress(value))


class ExperimentStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "experiments.sqlite3"
        self._initialized = False
        self._schema_lock = threading.Lock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with self._schema_lock:
                if not self._initialized:
                    con.execute("PRAGMA journal_mode=WAL")
                    con.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE, request BLOB NOT NULL,
                    manifest BLOB NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    lease_until REAL NOT NULL, cancel INTEGER NOT NULL DEFAULT 0,
                    progress BLOB NOT NULL, error TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_experiment_batch
                    ON batches((1)) WHERE state IN ('queued','loading','processing');
                CREATE TABLE IF NOT EXISTS runs (
                    batch TEXT NOT NULL REFERENCES batches(id), source TEXT NOT NULL,
                    state TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    error TEXT, summary BLOB NOT NULL,
                    PRIMARY KEY(batch, source));
                CREATE TABLE IF NOT EXISTS datasets (
                    batch TEXT NOT NULL, symbol TEXT NOT NULL, payload BLOB NOT NULL,
                    digest TEXT NOT NULL, PRIMARY KEY(batch, symbol));
                CREATE TABLE IF NOT EXISTS results (
                    batch TEXT NOT NULL, source TEXT NOT NULL, symbol TEXT NOT NULL,
                    payload BLOB NOT NULL, PRIMARY KEY(batch, source, symbol));
                CREATE TABLE IF NOT EXISTS evidence (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
                    symbol TEXT NOT NULL, payload BLOB NOT NULL);
                CREATE INDEX IF NOT EXISTS experiment_evidence_lookup
                    ON evidence(batch,source,kind,seq);
                    """)
                    self._initialized = True
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _recover(con: sqlite3.Connection) -> None:
        now = time.time()
        stale = con.execute("SELECT id,cancel FROM batches WHERE state IN ('queued','loading','processing') "
                            "AND lease_until < ?", (now,)).fetchall()
        for row in stale:
            state = "cancelled" if row["cancel"] else "interrupted"
            reason = "Cancelled by user; partial results are retained." if row["cancel"] else "Worker lease expired; start a new batch to retry."
            con.execute("UPDATE batches SET state=?, updated=?, error=? WHERE id=?",
                        (state, now, reason, row["id"]))
            con.execute("UPDATE runs SET state=?,error=? WHERE batch=? "
                        "AND state IN ('queued','processing')",
                        (state, reason, row["id"]))

    def create(self, request: dict, manifest: list[dict]) -> tuple[str, bool]:
        digest = hashlib.sha256(packed({k: v for k, v in request.items()
                                       if k != "idempotency_key"})).hexdigest()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._recover(con)
            old = con.execute("SELECT id,request_hash FROM batches WHERE idempotency_key=?",
                              (request["idempotency_key"],)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise Conflict("This request key was already used with different inputs")
                return old["id"], False
            if con.execute("SELECT 1 FROM batches WHERE state IN ('queued','loading','processing')").fetchone():
                raise Conflict("An experiment batch is already running; cancel it or wait for completion")
            batch_id, now = uuid.uuid4().hex, time.time()
            con.execute("INSERT INTO batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, digest, request["idempotency_key"], packed(request),
                         packed(manifest), "queued", now, now, now + LEASE_SECONDS, 0,
                         packed({"stage": "queued", "completed_symbols": 0,
                                 "total_symbols": None}), None))
            for source in manifest:
                state = "blocked" if source["availability"] == "blocked" else "queued"
                con.execute("INSERT INTO runs VALUES(?,?,?,0,?,?)",
                            (batch_id, source["id"], state,
                             source.get("blocker") if state == "blocked" else None,
                             packed({})))
            return batch_id, True

    def batch(self, batch_id: str, *, manifest: bool = False) -> dict:
        with self.connect() as con:
            self._recover(con)
            row = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError("Experiment batch not found")
            out = dict(row)
            out["request"] = unpacked(out["request"])
            out["progress"] = unpacked(out["progress"])
            if manifest:
                out["manifest"] = unpacked(out["manifest"])
            else:
                out.pop("manifest")
            for key in ("request_hash", "idempotency_key", "lease_until"):
                out.pop(key)
            return out

    def listing(self, limit: int = 30) -> list[dict]:
        with self.connect() as con:
            self._recover(con)
            rows = con.execute("SELECT id,state,created,updated,error,request,progress FROM batches "
                               "ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
            return [{**dict(r), "request": unpacked(r["request"]),
                     "progress": unpacked(r["progress"])} for r in rows]

    def runs(self, batch_id: str) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM runs WHERE batch=? ORDER BY source", (batch_id,)).fetchall()
            return [{**dict(row), "summary": unpacked(row["summary"])} for row in rows]

    def heartbeat(self, batch_id: str, stage: str, progress: dict) -> bool:
        with self.connect() as con:
            now = time.time()
            changed = con.execute("UPDATE batches SET state=?,progress=?,updated=?,lease_until=? "
                                  "WHERE id=? AND state IN ('queued','loading','processing')",
                                  (stage, packed(progress), now, now + LEASE_SECONDS, batch_id)).rowcount
            row = con.execute("SELECT cancel FROM batches WHERE id=?", (batch_id,)).fetchone()
            return bool(changed and row and not row["cancel"])

    def cancelled(self, batch_id: str) -> bool:
        with self.connect() as con:
            row = con.execute("SELECT cancel,state FROM batches WHERE id=?", (batch_id,)).fetchone()
            return row is None or bool(row["cancel"]) or row["state"] in TERMINAL

    def cancel(self, batch_id: str) -> None:
        with self.connect() as con:
            row = con.execute("SELECT state FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError("Experiment batch not found")
            if row["state"] not in TERMINAL:
                con.execute("UPDATE batches SET cancel=1,updated=? WHERE id=?", (time.time(), batch_id))

    def finish(self, batch_id: str, state: str, error: str | None = None) -> None:
        if state not in TERMINAL:
            raise ValueError("Expected a terminal batch state")
        with self.connect() as con:
            # An expired worker cannot overwrite the replacement/interrupted state.
            con.execute("UPDATE batches SET state=?,error=?,updated=? WHERE id=? "
                        "AND state IN ('queued','loading','processing')",
                        (state, error, time.time(), batch_id))
            unfinished = "cancelled" if state == "cancelled" else "failed"
            con.execute("UPDATE runs SET state=?,error=? WHERE batch=? AND state IN ('queued','processing')",
                        (unfinished, error or "Batch ended before this folder finished", batch_id))

    def update_run(self, batch_id: str, source: str, state: str,
                   progress: int, summary: dict, error: str | None = None) -> None:
        with self.connect() as con:
            con.execute("UPDATE runs SET state=?,progress=?,summary=?,error=? WHERE batch=? AND source=?",
                        (state, progress, packed(summary), error, batch_id, source))

    def dataset(self, batch_id: str, symbol: str, rows: list[dict]) -> str:
        payload = packed(rows)
        digest = hashlib.sha256(payload).hexdigest()
        with self.connect() as con:
            con.execute("INSERT INTO datasets VALUES(?,?,?,?)", (batch_id, symbol, payload, digest))
        return digest

    def save_result(self, batch_id: str, source: str, symbol: str, result: dict) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO results VALUES(?,?,?,?)", (batch_id, source, symbol, packed(result)))
            for kind in ("signals", "trades", "positions", "outcomes", "logs"):
                rows = result.get(kind, [])
                con.executemany("INSERT INTO evidence(batch,source,kind,symbol,payload) VALUES(?,?,?,?,?)",
                                [(batch_id, source, kind, symbol, packed(row)) for row in rows])

    def evidence(self, batch_id: str, source: str, kind: str, *, after: int = 0,
                 limit: int = 100, symbol: str | None = None) -> dict:
        with self.connect() as con:
            clause, params = "batch=? AND source=? AND kind=? AND seq>?", [batch_id, source, kind, after]
            if symbol:
                clause += " AND symbol=?"
                params.append(symbol)
            rows = con.execute(f"SELECT seq,symbol,payload FROM evidence WHERE {clause} ORDER BY seq LIMIT ?",
                               [*params, limit + 1]).fetchall()
            count_clause = clause.replace(" AND seq>?", "")
            count_params = [batch_id, source, kind] + ([symbol] if symbol else [])
            total = con.execute(f"SELECT COUNT(*) FROM evidence WHERE {count_clause}", count_params).fetchone()[0]
            return {"rows": [{"cursor": r["seq"], **unpacked(r["payload"])} for r in rows[:limit]],
                    "next": rows[limit - 1]["seq"] if len(rows) > limit else None,
                    "total": total}

    def chart(self, batch_id: str, symbol: str) -> list[dict]:
        with self.connect() as con:
            row = con.execute("SELECT payload FROM datasets WHERE batch=? AND symbol=?",
                              (batch_id, symbol)).fetchone()
            if row is None:
                raise KeyError("No frozen history for this symbol")
            return unpacked(row["payload"])

    def record(self, batch_id: str, source: str, cursor: int) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT payload FROM evidence WHERE batch=? AND source=? AND seq=?",
                              (batch_id, source, cursor)).fetchone()
            if row is None:
                raise KeyError("Saved experiment record not found")
            return unpacked(row["payload"])
