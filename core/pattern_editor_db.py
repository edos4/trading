"""PostgreSQL connection and explicit migration support; never creates files/DBs."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import re

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations/pattern_editor"


class EditError(ValueError):
    pass


class Conflict(EditError):
    pass


class DatabaseUnavailable(EditError):
    pass


class MigrationRequired(EditError):
    pass


def schema_name(value):
    if not re.fullmatch(r"pattern_editor(?:_[a-z0-9_]+)?", value) or len(value) > 63:
        raise EditError("Use pattern_editor or a pattern_editor_* schema (at most 63 characters)")
    return value


def configured_dsn():
    from config import settings
    from data.db import build_dsn
    return settings.pattern_editor_database_url or build_dsn()


@contextmanager
def connection(dsn, schema="pattern_editor"):
    """New connection per operation, with bounded waits and redacted failures."""
    schema_name(schema)
    if not isinstance(dsn, str) or not dsn.strip():
        raise DatabaseUnavailable("Configure PostgreSQL for the pattern editor")
    try:
        # Do not accept conninfo search_path/timeout overrides from a DSN.
        with psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row,
                             options="-c statement_timeout=20000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=30000") as con:
            con.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            yield con
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        # DSNs, server addresses, credentials and arbitrary SQL are not reflected.
        if isinstance(exc, (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)):
            raise Conflict("Concurrent editor transaction; retry the operation") from None
        raise DatabaseUnavailable("PostgreSQL unavailable or operation timed out; check editor connection and server") from None
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
        raise MigrationRequired("Run scripts/migrate_pattern_editor.py before using the editor") from None
    except psycopg.IntegrityError:
        raise Conflict("Editor integrity constraint rejected the operation") from None
    except psycopg.errors.InsufficientPrivilege:
        raise EditError("PostgreSQL role lacks required editor privileges") from None
    except psycopg.ProgrammingError:
        raise EditError("Invalid PostgreSQL editor operation or connection configuration") from None


def migration_files(directory=MIGRATIONS):
    result = []
    for path in sorted(Path(directory).glob("*.sql")):
        match = re.fullmatch(r"([0-9]{4})_[a-z0-9_]+\.sql", path.name)
        if not match:
            raise EditError("Invalid editor migration filename")
        data = path.read_bytes()
        result.append((int(match[1]), path.name, hashlib.sha256(data).hexdigest(), data.decode("utf-8")))
    if not result or [r[0] for r in result] != list(range(1, len(result) + 1)):
        raise EditError("Editor migrations must be contiguous starting at 0001")
    return result


def verify_migrations(con, directory=MIGRATIONS):
    expected = [(n, name, checksum) for n, name, checksum, _ in migration_files(directory)]
    rows = con.execute("SELECT version,name,sha256 FROM schema_migrations ORDER BY version").fetchall()
    actual = [(r['version'], r['name'], r['sha256']) for r in rows]
    if actual != expected:
        raise MigrationRequired("Editor schema version/checksum mismatch; run or inspect editor migrations")


def migrate(dsn=None, schema="pattern_editor", *, directory=MIGRATIONS):
    """Apply all pending migrations atomically, serializing concurrent commands."""
    schema_name(schema)
    migrations = migration_files(directory)
    with connection(configured_dsn() if dsn is None else dsn, schema) as con:
        # Lock before CREATE SCHEMA/table so simultaneous initialization is safe.
        con.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("pattern-editor-migrations:" + schema,))
        con.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        con.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())""")
        applied = con.execute("SELECT version,name,sha256 FROM schema_migrations ORDER BY version").fetchall()
        expected = [(n, name, checksum) for n, name, checksum, _ in migrations]
        if [(r['version'], r['name'], r['sha256']) for r in applied] != expected[:len(applied)] or len(applied) > len(expected):
            raise MigrationRequired("Applied editor migrations differ from source; restore matching migrations")
        done = []
        for version, name, checksum, source in migrations[len(applied):]:
            con.execute(source)
            con.execute("INSERT INTO schema_migrations(version,name,sha256) VALUES(%s,%s,%s)", (version, name, checksum))
            done.append(version)
        return done
