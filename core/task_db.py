"""PostgreSQL connection and explicit migration support for the Tasks board.

Mirrors ``core/pattern_editor_db.py``: one connection per operation, a
schema-qualified ``search_path``, checksum-verified migrations under an advisory
lock, and bounded waits. Never creates databases or files.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import re

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations/tasks"


class TaskError(ValueError):
    """Invalid board input or operation."""


class TaskNotFound(TaskError):
    """Referenced list/card/subtask/comment does not exist."""


class TaskConflict(TaskError):
    """Operation conflicts with current board state (e.g. non-empty list)."""


class TaskDatabaseUnavailable(TaskError):
    """PostgreSQL is unreachable or the operation timed out."""


class TaskMigrationRequired(TaskError):
    """The Tasks schema is missing or does not match the shipped migrations."""


def schema_name(value: str) -> str:
    if not re.fullmatch(r"tasks(?:_[a-z0-9_]+)?", value or "") or len(value) > 63:
        raise TaskError("Use tasks or a tasks_* schema (at most 63 characters)")
    return value


def configured_dsn() -> str:
    from config import settings
    from data.db import build_dsn

    return settings.task_database_url or build_dsn()


@contextmanager
def connection(dsn, schema="tasks"):
    """New connection per operation, with bounded waits and redacted failures."""
    schema_name(schema)
    if not isinstance(dsn, str) or not dsn.strip():
        raise TaskDatabaseUnavailable("Configure PostgreSQL for the Tasks board")
    try:
        # Do not accept conninfo search_path/timeout overrides from a DSN.
        with psycopg.connect(
            dsn, connect_timeout=5, row_factory=dict_row,
            options="-c statement_timeout=20000 -c lock_timeout=5000 "
                    "-c idle_in_transaction_session_timeout=30000",
        ) as con:
            con.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                sql.Identifier(schema)))
            yield con
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        if isinstance(exc, (psycopg.errors.DeadlockDetected,
                            psycopg.errors.SerializationFailure)):
            raise TaskConflict("Concurrent board transaction; retry the operation") from None
        raise TaskDatabaseUnavailable(
            "PostgreSQL unavailable or operation timed out; check the Tasks "
            "connection and server") from None
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
        raise TaskMigrationRequired(
            "Run scripts/setup_tasks_db.py before using the Tasks tab") from None
    except psycopg.IntegrityError:
        raise TaskConflict("Board integrity constraint rejected the operation") from None
    except psycopg.errors.InsufficientPrivilege:
        raise TaskError("PostgreSQL role lacks required Tasks privileges") from None
    except psycopg.ProgrammingError:
        raise TaskError("Invalid PostgreSQL Tasks operation or connection configuration") from None


def migration_files(directory=MIGRATIONS):
    result = []
    for path in sorted(Path(directory).glob("*.sql")):
        match = re.fullmatch(r"([0-9]{4})_[a-z0-9_]+\.sql", path.name)
        if not match:
            raise TaskError("Invalid Tasks migration filename")
        data = path.read_bytes()
        result.append((int(match[1]), path.name, hashlib.sha256(data).hexdigest(),
                       data.decode("utf-8")))
    if not result or [r[0] for r in result] != list(range(1, len(result) + 1)):
        raise TaskError("Tasks migrations must be contiguous starting at 0001")
    return result


def verify_migrations(con, directory=MIGRATIONS):
    expected = [(n, name, checksum) for n, name, checksum, _ in migration_files(directory)]
    rows = con.execute(
        "SELECT version,name,sha256 FROM schema_migrations ORDER BY version").fetchall()
    actual = [(r["version"], r["name"], r["sha256"]) for r in rows]
    if actual != expected:
        raise TaskMigrationRequired(
            "Tasks schema version/checksum mismatch; run scripts/setup_tasks_db.py")


def migrate(dsn=None, schema="tasks", *, directory=MIGRATIONS):
    """Apply all pending migrations atomically, serializing concurrent commands."""
    schema_name(schema)
    migrations = migration_files(directory)
    with connection(configured_dsn() if dsn is None else dsn, schema) as con:
        # Lock before CREATE SCHEMA/table so simultaneous initialization is safe.
        con.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("tasks-migrations:" + schema,))
        con.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        con.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())""")
        applied = con.execute(
            "SELECT version,name,sha256 FROM schema_migrations ORDER BY version").fetchall()
        expected = [(n, name, checksum) for n, name, checksum, _ in migrations]
        if ([(r["version"], r["name"], r["sha256"]) for r in applied] != expected[:len(applied)]
                or len(applied) > len(expected)):
            raise TaskMigrationRequired(
                "Applied Tasks migrations differ from source; restore matching migrations")
        done = []
        for version, name, checksum, source in migrations[len(applied):]:
            con.execute(source)
            con.execute("INSERT INTO schema_migrations(version,name,sha256) VALUES(%s,%s,%s)",
                        (version, name, checksum))
            done.append(version)
        return done
