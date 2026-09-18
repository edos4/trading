"""Real PostgreSQL integration tests. No SQLite fixture or production DSN fallback."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import multiprocessing
from pathlib import Path
import shutil

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from core.pattern_edit_store import EditStore, EditError, Conflict, digest, uid
from core.pattern_editor_db import DatabaseUnavailable, MigrationRequired, connection, migrate


def version(store, pattern='pattern_test', parent=None):
    with store.transaction() as con:
        store.create_pattern(pattern, con=con)
        ref = store.blob(b'original\r\n\x00\xff', 'application/octet-stream', con)
        return store.insert_version({
            'pattern_id': pattern, 'parent_version_id': parent,
            'files': {'patterns/test.py': ref},
        }, con)


def allocate(args):
    dsn, schema, parent = args
    return version(EditStore(dsn=dsn, schema=schema), parent=parent)['version_number']


def migrate_worker(args):
    return migrate(*args)


def test_constructor_is_offline_and_never_creates_storage(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Constructor must not connect')
    monkeypatch.setattr('psycopg.connect', forbidden)
    root = tmp_path/'absent'
    EditStore(root, dsn='postgresql:///never_opened')
    assert not root.exists()
    with pytest.raises(DatabaseUnavailable, match='Configure'):
        EditStore(root, dsn='')
    with pytest.raises(EditError, match='schema'):
        EditStore(root, dsn='postgresql:///never_opened', schema='public')


def test_connection_errors_are_redacted_and_do_not_fallback(tmp_path, monkeypatch):
    import psycopg
    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError('secret-password host=private-server')
    monkeypatch.setattr('psycopg.connect', unavailable)
    store = EditStore(tmp_path/'absent', dsn='postgresql:///not_used')
    with pytest.raises(DatabaseUnavailable) as failure:
        store.list('versions')
    assert 'secret' not in str(failure.value)
    assert failure.value.__suppress_context__
    assert not (tmp_path/'absent').exists()


def test_migrations_are_explicit_idempotent_and_checksum_checked(editor_store_factory, tmp_path):
    factory = editor_store_factory
    assert migrate(factory.dsn, factory.schema) == []
    source = Path(__file__).resolve().parents[1]/'migrations/pattern_editor'
    altered = tmp_path/'migrations'
    shutil.copytree(source, altered)
    migration = next(altered.glob('*.sql'))
    migration.write_text(migration.read_text()+'\n-- modified\n')
    with pytest.raises(MigrationRequired, match='differ'):
        migrate(factory.dsn, factory.schema, directory=altered)
    assert migrate(factory.dsn, factory.schema) == []
    empty_schema = 'pattern_editor_test_'+uid()
    store = EditStore(dsn=factory.dsn, schema=empty_schema)
    with pytest.raises(MigrationRequired):
        store.list('versions')
    with connection(factory.dsn, factory.schema) as con:
        assert con.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (empty_schema,)).fetchone() is None


def test_concurrent_migrations_and_failed_migration_rollback(editor_store_factory, tmp_path):
    factory = editor_store_factory
    schema = 'pattern_editor_test_'+uid()
    try:
        with ProcessPoolExecutor(2, mp_context=multiprocessing.get_context('spawn')) as pool:
            results = list(pool.map(migrate_worker, [(factory.dsn, schema)]*2))
        assert sorted(results, key=len) == [[], [1]]
    finally:
        with connection(factory.dsn, factory.schema) as con:
            con.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
    broken = tmp_path/'broken'
    broken.mkdir()
    (broken/'0001_fail.sql').write_text('CREATE TABLE incomplete(id INT); SELECT missing_column;')
    with pytest.raises(EditError):
        migrate(factory.dsn, schema, directory=broken)
    with connection(factory.dsn, factory.schema) as con:
        assert con.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone() is None


def test_version_numbers_are_serialized_across_spawned_processes(editor_store_factory):
    factory = editor_store_factory
    first = version(factory())
    with ProcessPoolExecutor(3, mp_context=multiprocessing.get_context('spawn')) as pool:
        numbers = list(pool.map(allocate, [(factory.dsn, factory.schema, first['version_id'])]*6))
    assert sorted(numbers) == list(range(2, 8))
    assert len(factory().list('versions')) == 7
    assert factory().active_set() == {}  # staged versions are never auto-published


def test_blob_and_version_immutability_and_transaction_rollback(editor_store_factory):
    store = editor_store_factory()
    first = version(store)
    ref = first['files']['patterns/test.py']
    assert store.read_blob(ref) == b'original\r\n\x00\xff'
    assert store.blob(b'original\r\n\x00\xff', 'text/plain')['sha256'] == ref['sha256']
    for query in (
        "UPDATE versions SET payload='{}'::jsonb", 'DELETE FROM versions',
        "UPDATE content_blobs SET data='corrupt'::bytea", 'DELETE FROM content_blobs',
        'TRUNCATE content_blobs CASCADE', 'DELETE FROM version_files',
    ):
        with pytest.raises(Conflict):
            with store.transaction() as con:
                con.execute(query)
    assert store.read_blob(ref) == b'original\r\n\x00\xff'
    rolled_back = b'rollback-content'
    with pytest.raises(RuntimeError):
        with store.transaction() as con:
            store.blob(rolled_back, con=con)
            store.insert_version({'pattern_id': 'pattern_test', 'files': {}}, con)
            raise RuntimeError('simulate interrupted write')
    with pytest.raises(EditError, match='missing'):
        store.read_blob({'sha256': digest(rolled_back), 'size_bytes':len(rolled_back), 'media_type':'text/plain'})
    assert version(store)['version_number'] == 2
    with pytest.raises(Conflict):
        with store.transaction() as con:
            con.execute('INSERT INTO content_blobs(sha256,data,size_bytes) VALUES(%s,%s,%s)', ('0'*64,b'bad',3))
    with pytest.raises(EditError, match='mismatch'):
        store.read_blob({**ref, 'size_bytes': 99})


def test_cross_pattern_parent_default_and_default_archive_constraints(editor_store_factory):
    store = editor_store_factory()
    a, b = version(store, 'pattern_a'), version(store, 'pattern_b')
    with pytest.raises(Conflict):
        version(store, 'pattern_a', b['version_id'])
    with pytest.raises(Conflict):
        store.update_pattern('pattern_a', 0, active=b['version_id'], published=True, enabled=True)
    with pytest.raises(Conflict):
        store.update_pattern('pattern_a', 0, active=None, published=True, enabled=True)
    store.update_pattern('pattern_a', 0, active=a['version_id'], published=True, enabled=True)
    with pytest.raises(Conflict):
        with store.transaction() as con:
            con.execute('UPDATE version_lifecycle SET archived_at=now() WHERE version=%s', (a['version_id'],))
    replacement = version(store, 'pattern_a', a['version_id'])
    with store.transaction() as con:
        store.update_pattern('pattern_a', 1, active=replacement['version_id'], published=True, enabled=True, con=con)
        con.execute('UPDATE version_lifecycle SET archived_at=now() WHERE version=%s', (a['version_id'],))
    assert store.active_set() == {'pattern_a': replacement['version_id']}
    with pytest.raises(Conflict):
        store.update_pattern('pattern_a', 2, active=a['version_id'], published=True, enabled=True)
    assert store.get('versions', a['version_id']) == a


def test_optimistic_default_change_has_one_winner(editor_store_factory):
    store = editor_store_factory()
    first = version(store)
    second = version(store, parent=first['version_id'])
    def change(identity):
        try:
            return store.update_pattern('pattern_test', 0, active=identity, published=True, enabled=True)['active']
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(change, [first['version_id'], second['version_id']]))
    assert results.count('conflict') == 1
    assert len(store.active_set()) == 1


def test_storage_graph_for_import_jobs_presets_reports_results(editor_store_factory):
    store = editor_store_factory()
    v = version(store)
    with store.transaction() as con:
        con.execute('INSERT INTO import_batches(id,snapshot_sha256,manifest) VALUES(%s,%s,%s)',
                    ('batch', digest(b'manifest'), Jsonb({'patterns':['pattern_test']})))
        con.execute('INSERT INTO import_state(batch) VALUES(%s)', ('batch',))
        store.insert_report({'report_id':'import-report', 'batch_id':'batch', 'verified':True}, con)
        con.execute("UPDATE import_state SET report_id='import-report',state='verified' WHERE batch='batch'")
        con.execute('INSERT INTO presets(id,payload) VALUES(%s,%s)', ('preset',Jsonb({'symbols':['TEST']})))
        con.execute('INSERT INTO workers(id,heartbeat,payload) VALUES(%s,now(),%s)', ('worker',Jsonb({'pid':1})))
        con.execute('INSERT INTO jobs(id,kind,idempotency_key,request_sha256,payload,pattern,base_version,preset) '
                    'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                    ('job','backtest','request',digest(b'request'),Jsonb({'version':v['version_id']}),'pattern_test',v['version_id'],'preset'))
        con.execute("UPDATE jobs SET owner='worker',lease_token='token',lease_expires_at=now()+interval '1 minute' WHERE id='job'")
        con.execute('INSERT INTO backtest_runs(id,job,inputs_sha256,payload) VALUES(%s,%s,%s,%s)',
                    ('run','job',digest(b'inputs'),Jsonb({'symbols':['TEST']})))
        con.execute('INSERT INTO run_versions(run,pattern,version) VALUES(%s,%s,%s)',('run','pattern_test',v['version_id']))
        con.execute('INSERT INTO results(id,run,payload) VALUES(%s,%s,%s)',('result','run',Jsonb({'trades':[]})))
        con.execute("UPDATE version_lifecycle SET baseline_report_id='import-report',successful_run_id='run' WHERE version=%s",(v['version_id'],))
        con.execute('INSERT INTO events(id,pattern,payload) VALUES(%s,%s,%s)',('event','pattern_test',Jsonb({'action':'test'})))
    assert store.get('jobs','job')['version'] == v['version_id']
    assert store.get('results','result') == {'trades':[]}
    for query in (
        "UPDATE import_batches SET manifest='{}'::jsonb", "DELETE FROM reports", "DELETE FROM results",
        "INSERT INTO jobs(id,kind,idempotency_key,request_sha256,payload) SELECT 'duplicate',kind,idempotency_key,request_sha256,payload FROM jobs",
    ):
        with pytest.raises(Conflict):
            with store.transaction() as con:
                con.execute(query)


def test_repository_reports_and_worker_upsert(editor_store_factory):
    store = editor_store_factory()
    store.create_pattern('pattern_test')
    session = {'session_id':'session', 'pattern_id':'pattern_test','generation':0}
    store.save_session(session)
    revision = {'revision_id':'revision', 'session_id':'session','explanation':'test'}
    report = {'report_id':'report', 'revision_id':'revision', 'ready':False}
    with store.transaction() as con:
        store.insert_revision(revision, con)
        store.insert_report(report, con)
    assert store.get('reports','report') == report
    assert store.list('revisions', 'session','session') == [revision]
    payload = {'worker_id':'worker', 'heartbeat':'2026-09-18T00:00:00+00:00','stopped':False}
    store.save_worker(payload)
    payload['stopped'] = True
    store.save_worker(payload)
    assert store.list('workers') == [payload]
    with pytest.raises(EditError):
        store.list('versions', 'arbitrary_column','value')


def test_real_connection_failure_does_not_create_files(tmp_path):
    # This socket directory deliberately doesn't exist; no application DB is used.
    store = EditStore(tmp_path/'no-files', dsn=f'host={tmp_path}/absent dbname=nonexistent')
    with pytest.raises(DatabaseUnavailable):
        store.blob(b'never persisted')
    assert list(tmp_path.iterdir()) == []
