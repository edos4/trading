"""Durable backtest job records: transactional claiming, leases, cancellation.

Slow work never runs inside a database transaction. A runner claims a queued
job with ``FOR UPDATE SKIP LOCKED``, holds a lease, heartbeats while working,
and writes the terminal state. Leases that expire (process death, restart) are
marked ``interrupted`` rather than silently re-run, so recovered jobs stay
truthful until an explicit retry.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from psycopg.types.json import Jsonb

from core.pattern_edit_store import EditError, canonical, digest, now, uid

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "blocked", "interrupted"})


class UnknownJob(EditError):
    pass


class LeaseLost(EditError):
    pass


class BacktestJobStore:
    """Thin, typed wrapper over the ``jobs``/``workers`` rows."""

    def __init__(self, store):
        self.store = store

    # ── worker identity ──────────────────────────────────────────────────
    def register_worker(self, worker_id: str, con=None) -> str:
        payload = {"worker_id": worker_id, "pid": os.getpid(), "heartbeat": now(),
                   "stopped": False}
        if con is None:
            with self.store.transaction() as db:
                return self.register_worker(worker_id, db)
        con.execute(
            "INSERT INTO workers(id,heartbeat,payload) VALUES(%s,clock_timestamp(),%s) "
            "ON CONFLICT(id) DO UPDATE SET heartbeat=EXCLUDED.heartbeat,payload=EXCLUDED.payload",
            (worker_id, Jsonb(payload)))
        return worker_id

    # ── lifecycle ────────────────────────────────────────────────────────
    def create(self, *, request_sha256: str, idempotency_key: str, payload: dict,
               kind: str = "backtest", preset_id: str | None = None,
               base_version_id: str | None = None, retry_of: str | None = None,
               attempt: int = 1, state: str = "queued", con=None) -> dict:
        if con is None:
            with self.store.transaction() as db:
                return self.create(request_sha256=request_sha256,
                                   idempotency_key=idempotency_key, payload=payload,
                                   kind=kind, preset_id=preset_id,
                                   base_version_id=base_version_id, retry_of=retry_of,
                                   attempt=attempt, state=state, con=db)
        job_id = uid()
        canonical(payload)
        prior = con.execute("SELECT * FROM jobs WHERE idempotency_key=%s",
                            (idempotency_key,)).fetchone()
        if prior is not None:
            if prior["request_sha256"] != request_sha256:
                raise EditError("Idempotency key was used for a different request")
            return prior
        con.execute(
            "INSERT INTO jobs(id,kind,pattern,base_version,preset,idempotency_key,"
            "request_sha256,payload,state,attempt,retry_of) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, kind, payload.get("request", {}).get("pattern_id") if kind == "edit" else None,
             base_version_id, preset_id, idempotency_key, request_sha256,
             Jsonb(payload), state, attempt, retry_of))
        return self.find(job_id, con)

    def find(self, job_id: str, con=None) -> dict:
        if con is None:
            with self.store.connect() as db:
                return self.find(job_id, db)
        row = con.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
        if row is None:
            raise UnknownJob("Backtest job not found")
        return row

    def find_by_key(self, idempotency_key: str, con=None) -> dict | None:
        if con is None:
            with self.store.connect() as db:
                return self.find_by_key(idempotency_key, db)
        return con.execute("SELECT * FROM jobs WHERE idempotency_key=%s",
                           (idempotency_key,)).fetchone()

    def claim(self, worker_id: str, *, lease_seconds: int = 60, kinds=("backtest",)) -> dict | None:
        """Atomically lease the oldest queued job. None when nothing is due."""
        with self.store.transaction() as con:
            self.register_worker(worker_id, con)
            row = con.execute(
                "UPDATE jobs SET owner=%s, lease_token=%s, "
                "lease_expires_at=clock_timestamp() + make_interval(secs => %s), "
                "state='backtesting', updated_at=clock_timestamp() "
                "WHERE id = (SELECT id FROM jobs WHERE kind = ANY(%s) AND state='queued' "
                "  AND (lease_expires_at IS NULL OR lease_expires_at < clock_timestamp()) "
                "  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
                (worker_id, uid(), int(lease_seconds), list(kinds))).fetchone()
            return row

    def touch(self, job_id: str, owner: str, *, lease_seconds: int = 60,
              state: str | None = None, progress: dict | None = None,
              error: dict | None = None, con=None) -> dict:
        """Heartbeat and/or patch state. Ownership is enforced."""
        if con is None:
            with self.store.transaction() as db:
                return self.touch(job_id, owner, lease_seconds=lease_seconds,
                                  state=state, progress=progress, error=error, con=db)
        sets = ["lease_expires_at=clock_timestamp() + make_interval(secs => %s)",
                "updated_at=clock_timestamp()"]
        params: list = [int(lease_seconds)]
        if state is not None:
            sets.append("state=%s")
            params.append(state)
        if progress is not None or error is not None:
            patch: dict = {}
            if progress is not None:
                patch["progress"] = progress
            if error is not None:
                patch["error"] = error
            sets.append("payload = payload || %s")
            params.append(Jsonb(patch))
        params.extend([job_id, owner])
        row = con.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE id=%s AND owner=%s RETURNING *",
            params).fetchone()
        if row is None:
            raise LeaseLost("Job lease was lost or reassigned")
        return row

    def finish(self, job_id: str, owner: str, state: str, *,
               progress: dict | None = None, error: dict | None = None) -> dict:
        if state not in TERMINAL_STATES:
            raise EditError("finish requires a terminal job state")
        with self.store.transaction() as con:
            row = con.execute(
                "UPDATE jobs SET state=%s, owner=NULL, lease_token=NULL, lease_expires_at=NULL, "
                "updated_at=clock_timestamp(), payload = payload || %s "
                "WHERE id=%s AND owner=%s RETURNING *",
                (state, Jsonb({"progress": progress or {}, "error": error}), job_id, owner)
            ).fetchone()
            if row is None:
                raise LeaseLost("Job lease was lost or reassigned")
            return row

    def request_cancel(self, job_id: str, *, actor: str = "user") -> dict:
        with self.store.transaction() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            if row is None:
                raise UnknownJob("Backtest job not found")
            if row["state"] in TERMINAL_STATES:
                return row
            if row["state"] == "queued" or row["owner"] is None:
                return con.execute(
                    "UPDATE jobs SET state='cancelled', owner=NULL, lease_token=NULL, "
                    "lease_expires_at=NULL, updated_at=clock_timestamp(), "
                    "payload = payload || %s WHERE id=%s RETURNING *",
                    (Jsonb({"cancel_requested": True, "cancelled_by": actor}), job_id)
                ).fetchone()
            return con.execute(
                "UPDATE jobs SET updated_at=clock_timestamp(), payload = payload || %s "
                "WHERE id=%s RETURNING *",
                (Jsonb({"cancel_requested": True, "cancelled_by": actor}), job_id)).fetchone()

    def cancel_requested(self, job_id: str) -> bool:
        with self.store.connect() as con:
            row = con.execute("SELECT payload->>'cancel_requested' AS c FROM jobs WHERE id=%s",
                              (job_id,)).fetchone()
        if row is None:
            raise UnknownJob("Backtest job not found")
        return bool(row["c"])

    def expire_leases(self, *, lease_seconds: int = 0) -> int:
        """Mark crashed/leaked runners interrupted without re-running them."""
        with self.store.transaction() as con:
            rows = con.execute(
                "UPDATE jobs SET state='interrupted', owner=NULL, lease_token=NULL, "
                "lease_expires_at=NULL, updated_at=clock_timestamp(), "
                "payload = payload || %s "
                "WHERE state NOT IN ('completed','failed','cancelled','blocked','interrupted') "
                "AND owner IS NOT NULL AND lease_expires_at < clock_timestamp() RETURNING id",
                (Jsonb({"error": {"code": "execution-failed", "retryable": True,
                                  "message": "Job lease expired; runner stopped without finishing"}}),)
            ).fetchall()
            return len(rows)

    def retry(self, job_id: str, *, idempotency_key: str | None = None) -> dict:
        """Create a traceable retry attempt for a finished job's frozen request."""
        with self.store.transaction() as con:
            prior = con.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE",
                                (job_id,)).fetchone()
            if prior is None:
                raise UnknownJob("Backtest job not found")
            if prior["state"] not in TERMINAL_STATES:
                raise EditError("Only a finished job can be retried")
            key = idempotency_key or f"{prior['idempotency_key']}:retry:{prior['attempt'] + 1}"
            request = dict(prior["payload"].get("request") or {})
            request["retry_of_run_id"] = prior["id"]
            payload = {"request": request, "progress": {}}
            new_id = uid()
            con.execute(
                "INSERT INTO jobs(id,kind,preset,idempotency_key,request_sha256,payload,"
                "state,attempt,retry_of) VALUES(%s,%s,%s,%s,%s,%s,'queued',%s,%s)",
                (new_id, prior["kind"], prior["preset"], key,
                 digest(canonical({"retry": request})), Jsonb(payload),
                 prior["attempt"] + 1, prior["id"]))
            return self.find(new_id, con)


@contextmanager
def claim_and_release(jobs: BacktestJobStore, worker_id: str, **kwargs):
    """Claim one job; always clear the lease if the caller abandons it."""
    row = jobs.claim(worker_id, **kwargs)
    try:
        yield row
    except Exception:
        if row is not None:
            with jobs.store.transaction() as con:
                con.execute(
                    "UPDATE jobs SET owner=NULL, lease_token=NULL, lease_expires_at=NULL, "
                    "state='queued', updated_at=clock_timestamp() WHERE id=%s AND owner=%s",
                    (row["id"], worker_id))
        raise
