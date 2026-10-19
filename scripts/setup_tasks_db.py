"""Ensure the Tasks (Kanban) PostgreSQL schema exists and is migrated.

Idempotent and safe to run on every deploy:

1. when ``TASKS_DATABASE_URL`` names a dedicated database, create it if missing;
2. apply all pending Tasks migrations.

There is no seeding. Exit status is non-zero only when the schema cannot be made
ready.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from config import settings
    from core.task_db import TaskError, configured_dsn, migrate

    dsn = configured_dsn()
    if not dsn or not str(dsn).strip():
        print("No Tasks DSN configured (TASKS_DATABASE_URL or DATABASE_URL).",
              file=sys.stderr)
        return 1

    schema = settings.task_schema
    print(f"tasks schema: {schema}")
    try:
        if (settings.task_database_url or "").strip():
            from scripts.setup_editor_db import ensure_database

            ensure_database(dsn)
        applied = migrate(dsn, schema)
        print("    migrations: "
              + ("applied " + ",".join(map(str, applied)) if applied else "already up to date"))
    except (TaskError, ValueError) as exc:
        print(f"Tasks schema setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
