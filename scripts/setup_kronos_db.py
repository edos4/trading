"""Ensure the Kronos forecast-history PostgreSQL schema exists and is migrated.

Idempotent and safe to run on every deploy:

1. when ``KRONOS_DATABASE_URL`` names a dedicated database, create it if missing;
2. apply all pending Kronos migrations.

There is no seeding. Exit status is non-zero only when the schema cannot be made
ready.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from config import settings
    from core.kronos_db import KronosError, configured_dsn, migrate

    dsn = configured_dsn()
    if not dsn or not str(dsn).strip():
        print("No Kronos DSN configured (KRONOS_DATABASE_URL or DATABASE_URL).",
              file=sys.stderr)
        return 1

    schema = settings.kronos_schema
    print(f"kronos schema: {schema}")
    try:
        if (settings.kronos_database_url or "").strip():
            from scripts.setup_editor_db import ensure_database

            ensure_database(dsn)
        applied = migrate(dsn, schema)
        print("    migrations: "
              + ("applied " + ",".join(map(str, applied)) if applied else "already up to date"))
    except (KronosError, ValueError) as exc:
        print(f"Kronos schema setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
