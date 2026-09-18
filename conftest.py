"""Pytest root config — ensures the project root is importable and registers markers."""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: end-to-end golden-number checks over the pinned fixture barcache "
        "(minutes, not seconds) — skip with `-m 'not slow'`.",
    )


# Explicit opt-in only: never derive a test DSN from application Settings/.env.
import os
import uuid

import pytest


@pytest.fixture
def editor_store_factory():
    from psycopg import sql
    from core.pattern_edit_store import EditStore
    from core.pattern_editor_db import connection, migrate

    dsn = os.environ.get('PATTERN_EDITOR_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set PATTERN_EDITOR_TEST_DATABASE_URL to a disposable PostgreSQL database')
    schema = 'pattern_editor_test_' + uuid.uuid4().hex
    migrate(dsn, schema)

    def factory(root=None):
        kwargs = {'dsn': dsn, 'schema': schema}
        return EditStore(**kwargs) if root is None else EditStore(root, **kwargs)

    factory.dsn = dsn
    factory.schema = schema
    try:
        yield factory
    finally:
        # Generated name only; never clean up the application's pattern_editor schema.
        with connection(dsn, schema) as con:
            con.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture
def published_pattern_catalog(editor_store_factory, monkeypatch):
    """Consumer fixture: real storage; parity is tested separately by P03's full gate."""
    from core.pattern_bootstrap import Bootstrap, freeze
    import core.pattern_bootstrap_parity as parity
    store = editor_store_factory()
    snapshot = freeze(store.root, disabled=[])
    with monkeypatch.context() as patch:
        patch.setattr(parity, 'verify_parity', lambda store, version, snapshot:
                      {'version_id': version['version_id'], 'consumer_fixture': True})
        Bootstrap(store).verify_and_publish(Bootstrap(store).stage(snapshot), snapshot)
    monkeypatch.setattr('core.pattern_loader.EditStore', lambda: store)
    monkeypatch.setattr('core.pattern_versions.EditStore', lambda: store)
    return store
