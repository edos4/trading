"""Ensure the pattern-editor PostgreSQL database exists, is migrated and seeded.

Idempotent and safe to run on every deploy:

1. create the target database when it is missing (via postgres/template1);
2. apply all pending editor migrations;
3. seed the catalog from the working-tree detector files **only when it is still
   empty** — an already-published catalog is left alone.

A changed detector file is a deliberate new-version operation, not an automatic
re-import, so drift against the imported snapshot is reported as a warning
rather than silently re-imported or failing the deploy.

Exit status is non-zero only when the database cannot be made ready.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _target(dsn: str) -> tuple[dict, str]:
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    return info, (info.get("dbname") or "postgres")


def _maintenance_dsn(dsn: str, name: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    info = conninfo_to_dict(dsn)
    info["dbname"] = name
    return make_conninfo(**info)


def ensure_database(dsn: str) -> None:
    """Raise if PostgreSQL is unreachable; create the database if it is missing."""
    import psycopg
    from psycopg import sql

    from core.pattern_editor_db import DatabaseUnavailable

    _info, name = _target(dsn)
    try:
        with psycopg.connect(dsn, connect_timeout=5):
            return  # database exists and is reachable
    except psycopg.OperationalError as exc:
        text = str(exc)
        missing = "does not exist" in text or "3D000" in text
        if not missing:
            raise DatabaseUnavailable(
                "PostgreSQL is not reachable for the editor database. Start the "
                "server (first time: scripts/pg18.sh init; otherwise "
                "scripts/pg18.sh start) or fix PATTERN_EDITOR_DATABASE_URL / "
                "DATABASE_URL.") from None

    last: Exception | None = None
    for maintenance in ("postgres", "template1"):
        try:
            with psycopg.connect(_maintenance_dsn(dsn, maintenance),
                                 connect_timeout=5, autocommit=True) as con:
                con.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            print(f"    created database {name!r}")
            return
        except psycopg.OperationalError as exc:  # maintenance DB may not exist
            last = exc
        except psycopg.Error as exc:
            raise DatabaseUnavailable(
                f"could not create database {name!r}: {exc}") from None
    raise DatabaseUnavailable(
        f"database {name!r} is missing and no maintenance database is reachable: {last}")


def main() -> int:
    from config import settings
    from core.pattern_editor_db import DatabaseUnavailable, configured_dsn, migrate
    from core.pattern_edit_store import EditError, EditStore

    dsn = configured_dsn()
    if not dsn or not str(dsn).strip():
        print("No editor DSN configured (PATTERN_EDITOR_DATABASE_URL or DATABASE_URL).",
              file=sys.stderr)
        return 1

    schema = settings.pattern_editor_schema
    _info, name = _target(dsn)
    print(f"editor database: {name} (schema {schema})")

    try:
        ensure_database(dsn)
        applied = migrate(dsn, schema)
        print(f"    migrations: {'applied ' + ','.join(map(str, applied)) if applied else 'already up to date'}")

        store = EditStore(schema=schema)
        with store.connect() as con:
            published = con.execute(
                "SELECT COUNT(*) AS n FROM patterns WHERE published").fetchone()["n"]

        if not published:
            from core.pattern_bootstrap import Bootstrap

            report = Bootstrap(store).run()
            print(f"    imported catalog: batch={report['batch_id']} "
                  f"report={report['report_id']} status={report['status']}")
            if report.get("status") != "passed":
                print("Editor catalog import did not pass; see the stored report.",
                      file=sys.stderr)
                return 1
        else:
            _warn_on_drift(store)
            print(f"    catalog already published: {published} patterns (no import needed)")
    except (EditError, ValueError) as exc:
        print(f"Editor database setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _warn_on_drift(store) -> None:
    """Report (do not resolve) detector files that differ from the imported snapshot."""
    try:
        from core.pattern_bootstrap import freeze

        snapshot = freeze(store.root, disabled=[])
        with store.connect() as con:
            row = con.execute("SELECT snapshot_sha256 FROM import_batches "
                              "ORDER BY created_at DESC LIMIT 1").fetchone()
    except Exception as exc:  # noqa: BLE001 - informational only
        print(f"    note: could not compare the working tree to the import ({exc})")
        return
    if row and row["snapshot_sha256"] != snapshot.sha256:
        print("    WARNING: detector files differ from the imported snapshot. "
              "Run scripts/import_pattern_baselines.py deliberately to publish new "
              "versions; the deploy will not do this automatically.",
              file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
