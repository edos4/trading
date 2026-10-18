"""Durable local Laya store (docs/laya.md section 9).

A dedicated local SQLite database through the standard library:
proposed `data/cache/laya.sqlite3`. Deliberate local store, not a fallback
for PostgreSQL failure. Short transactions, foreign keys, schema version, and
a bounded busy timeout. Owned by the web-side application service; the
inference worker returns results to the service instead of writing directly.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS laya_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS laya_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    assessment_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_code TEXT,
    error_detail TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    cancelled INTEGER NOT NULL DEFAULT 0,
    previous_attempt_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_laya_jobs_idem ON laya_jobs(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_laya_jobs_state ON laya_jobs(state);
CREATE TABLE IF NOT EXISTS laya_assessments (
    id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    task_id TEXT NOT NULL,
    input_mode TEXT NOT NULL,
    decision_at TEXT NOT NULL,
    as_of TEXT NOT NULL,
    input_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    coalescing_hash TEXT NOT NULL,
    raw_json TEXT,
    normalized_json TEXT,
    calibration_status TEXT NOT NULL DEFAULT 'uncalibrated',
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    truncated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    rerun_of TEXT
);
CREATE INDEX IF NOT EXISTS idx_laya_asm_market_symbol ON laya_assessments(market, symbol);
CREATE INDEX IF NOT EXISTS idx_laya_asm_task ON laya_assessments(task_id);
CREATE TABLE IF NOT EXISTS laya_policies (
    policy_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS laya_policy_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    candidate_id TEXT,
    book_id TEXT,
    run_id TEXT,
    mode TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    decision_hash TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_laya_dec_assessment ON laya_policy_decisions(assessment_id);
"""

_lock = threading.Lock()
_store_dir_override: Path | None = None


def default_store_path() -> Path:
    try:
        from config import settings

        configured = (getattr(settings, "laya_store_path", "") or "").strip()
        if configured:
            return Path(configured)
    except Exception:
        pass
    return Path("data/cache/laya.sqlite3")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


class LayaStore:
    """Thread-safe SQLite owner for jobs, assessments, policies, decisions."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_store_path()
        if _store_dir_override is not None and path is None:
            self.path = _store_dir_override / "laya.sqlite3"
        with _lock:
            con = _connect(self.path)
            try:
                con.executescript(_SCHEMA)
                row = con.execute("SELECT value FROM laya_meta WHERE key='schema_version'").fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO laya_meta(key, value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
            finally:
                con.close()

    # ── jobs ──────────────────────────────────────────────────────────────
    def create_job(self, row: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            con = _connect(self.path)
            try:
                con.execute("BEGIN IMMEDIATE")
                existing = con.execute(
                    "SELECT * FROM laya_jobs WHERE idempotency_key=?",
                    (row["idempotency_key"],),
                ).fetchone()
                if existing is not None:
                    con.execute("ROLLBACK")
                    return dict(existing)
                con.execute(
                    "INSERT INTO laya_jobs(id, idempotency_key, input_hash, assessment_id,"
                    " state, created_at, updated_at, error_code, error_detail, attempt,"
                    " cancelled, previous_attempt_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["id"], row["idempotency_key"], row["input_hash"],
                     row["assessment_id"], row["state"], row["created_at"],
                     row["updated_at"], row.get("error_code"), row.get("error_detail"),
                     int(row.get("attempt") or 1), int(row.get("cancelled") or 0),
                     row.get("previous_attempt_id")),
                )
                con.execute("COMMIT")
                return dict(row)
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                con.close()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        con = _connect(self.path)
        try:
            row = con.execute("SELECT * FROM laya_jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def get_job_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        con = _connect(self.path)
        try:
            row = con.execute(
                "SELECT * FROM laya_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_job(job_id)
        keys = list(fields)
        with _lock:
            con = _connect(self.path)
            try:
                con.execute("BEGIN IMMEDIATE")
                setters = ", ".join(f"{k}=?" for k in keys)
                con.execute(
                    f"UPDATE laya_jobs SET {setters} WHERE id=?",
                    tuple(fields[k] for k in keys) + (job_id,),
                )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                con.close()
        return self.get_job(job_id)

    # ── assessments ───────────────────────────────────────────────────────
    def save_assessment(self, row: dict[str, Any]) -> None:
        with _lock:
            con = _connect(self.path)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "INSERT OR REPLACE INTO laya_assessments(id, market, symbol, task_id,"
                    " input_mode, decision_at, as_of, input_json, input_hash,"
                    " coalescing_hash, raw_json, normalized_json, calibration_status,"
                    " elapsed_ms, warnings_json, truncated, created_at, rerun_of)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["id"], row["market"], row["symbol"], row["task_id"],
                     row["input_mode"], row["decision_at"], row["as_of"],
                     row["input_json"], row["input_hash"], row["coalescing_hash"],
                     row.get("raw_json"), row.get("normalized_json"),
                     row.get("calibration_status") or "uncalibrated",
                     int(row.get("elapsed_ms") or 0),
                     row.get("warnings_json") or "[]",
                     int(row.get("truncated") or 0),
                     row["created_at"], row.get("rerun_of")),
                )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                con.close()

    def get_assessment(self, assessment_id: str) -> dict[str, Any] | None:
        con = _connect(self.path)
        try:
            row = con.execute(
                "SELECT * FROM laya_assessments WHERE id=?", (assessment_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def find_by_coalescing(self, coalescing_hash: str) -> dict[str, Any] | None:
        con = _connect(self.path)
        try:
            row = con.execute(
                "SELECT * FROM laya_assessments WHERE coalescing_hash=? ORDER BY created_at DESC LIMIT 1",
                (coalescing_hash,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def list_assessments(
        self, *,
        market: str = "", symbol: str = "", task_id: str = "",
        mode: str = "", result: str = "",
        limit: int = 50, offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = []
        args: list[Any] = []
        if market:
            clauses.append("market=?")
            args.append(market)
        if symbol:
            clauses.append("symbol=?")
            args.append(symbol.upper())
        if task_id:
            clauses.append("task_id=?")
            args.append(task_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        con = _connect(self.path)
        try:
            total = con.execute(
                f"SELECT COUNT(*) AS n FROM laya_assessments{where}", tuple(args)).fetchone()["n"]
            rows = con.execute(
                f"SELECT * FROM laya_assessments{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                tuple(args) + (int(limit), int(offset)),
            ).fetchall()
            return [dict(r) for r in rows], int(total)
        finally:
            con.close()

    # ── policies / decisions ──────────────────────────────────────────────
    def seed_policy(self, policy_id: str, version: int, payload: dict[str, Any], created_at: str) -> None:
        """Insert the policy, or migrate it when the incoming version differs.

        First seed wins only within a version: code ships threshold changes by
        bumping ``version`` (same version leaves an operator-edited row alone,
        so local tuning is never silently clobbered by a restart).
        """
        with _lock:
            con = _connect(self.path)
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT version FROM laya_policies WHERE policy_id=?",
                    (policy_id,),
                ).fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO laya_policies(policy_id, version, payload_json, created_at)"
                        " VALUES(?,?,?,?)",
                        (policy_id, int(version), json.dumps(payload), created_at),
                    )
                elif int(row["version"]) != int(version):
                    con.execute(
                        "UPDATE laya_policies SET version=?, payload_json=?, created_at=?"
                        " WHERE policy_id=?",
                        (int(version), json.dumps(payload), created_at, policy_id),
                    )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                con.close()

    def get_policy(self, policy_id: str) -> dict[str, Any] | None:
        con = _connect(self.path)
        try:
            row = con.execute(
                "SELECT * FROM laya_policies WHERE policy_id=?", (policy_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def save_decision(self, row: dict[str, Any]) -> int:
        with _lock:
            con = _connect(self.path)
            try:
                con.execute("BEGIN IMMEDIATE")
                cur = con.execute(
                    "INSERT INTO laya_policy_decisions(assessment_id, policy_id, candidate_id,"
                    " book_id, run_id, mode, outcome, reasons_json, decision_hash, evaluated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (row["assessment_id"], row["policy_id"], row.get("candidate_id"),
                     row.get("book_id"), row.get("run_id"), row["mode"], row["outcome"],
                     row.get("reasons_json") or "[]", row["decision_hash"], row["evaluated_at"]),
                )
                con.execute("COMMIT")
                return int(cur.lastrowid)
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                con.close()

    def decisions_for(self, assessment_id: str) -> list[dict[str, Any]]:
        con = _connect(self.path)
        try:
            rows = con.execute(
                "SELECT * FROM laya_policy_decisions WHERE assessment_id=? ORDER BY id DESC",
                (assessment_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


_store: LayaStore | None = None


def get_store(path: Path | str | None = None) -> LayaStore:
    global _store
    if path is not None:
        return LayaStore(path)
    if _store is None:
        _store = LayaStore()
    return _store
