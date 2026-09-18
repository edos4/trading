"""PostgreSQL editor repository; source/artifact bytes never live in a file store."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import uuid

from psycopg import sql
from psycopg.types.json import Jsonb

from core.pattern_editor_db import (
    EditError, Conflict, DatabaseUnavailable, MigrationRequired,
    configured_dsn, connection, schema_name, verify_migrations,
)

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative(value):
    p = PurePosixPath(value)
    if not value or value == '.' or '\x00' in value or p.is_absolute() or '..' in p.parts or '\\' in value or str(p) != value:
        raise EditError('Invalid artifact/source path')
    return p


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    """Legacy paper-ledger utility; the editor repository never calls it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.staging-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


class EditStore:
    """No connection or schema/file creation in the constructor.

    Only the DSN/schema configuration crosses process boundaries, never a live
    connection. Use a supplied transaction for atomic multi-record writes.
    Domain eligibility and complete bootstrap publication belong to P03/P04.
    """
    _columns = {
        'versions': 'pattern', 'sessions': 'pattern', 'revisions': 'session',
        'reports': 'revision', 'jobs': 'session', 'activations': 'pattern',
        'events': 'pattern', 'workers': None, 'presets': None,
        'backtest_runs': 'job', 'results': 'run',
    }

    def __init__(self, root=ROOT, *, dsn=None, schema='pattern_editor'):
        self.root = Path(root).resolve()  # trusted import/runtime inputs only
        self._dsn = configured_dsn() if dsn is None else dsn
        if not isinstance(self._dsn, str) or not self._dsn.strip():
            raise DatabaseUnavailable('Configure PostgreSQL for the pattern editor')
        self.schema = schema_name(schema)

    @contextmanager
    def connect(self):
        with connection(self._dsn, self.schema) as con:
            verify_migrations(con)
            yield con

    @contextmanager
    def transaction(self):
        with self.connect() as con:
            yield con

    def blob(self, data, media_type='application/json', con=None):
        if not isinstance(data, bytes):
            data = canonical(data)
        ref = {'sha256': digest(data), 'media_type': media_type, 'size_bytes': len(data)}
        if con is None:
            with self.transaction() as db:
                return self.blob(data, media_type, db)
        con.execute('INSERT INTO content_blobs(sha256,data,size_bytes) VALUES(%s,%s,%s) '
                    'ON CONFLICT(sha256) DO NOTHING', (ref['sha256'], data, len(data)))
        if self.read_blob(ref, con) != data:
            raise EditError('Corrupt immutable artifact')
        return ref

    def read_blob(self, ref, con=None):
        # Reject legacy filesystem references, even if a hash is also supplied.
        if set(ref) != {'sha256', 'size_bytes', 'media_type'}:
            raise EditError('Invalid database artifact reference')
        from core.pattern_editor_contracts import ContentRef
        try:
            ContentRef.model_validate(ref)
        except ValueError:
            raise EditError('Invalid database artifact reference') from None
        if con is None:
            with self.connect() as db:
                return self.read_blob(ref, db)
        row = con.execute('SELECT data,size_bytes FROM content_blobs WHERE sha256=%s',
                          (ref['sha256'],)).fetchone()
        if row is None:
            raise EditError('Required artifact is missing')
        data = bytes(row['data'])
        if len(data) != ref['size_bytes'] or row['size_bytes'] != len(data) or digest(data) != ref['sha256']:
            raise EditError('Artifact hash/size mismatch')
        return data

    def get(self, table, identity, con=None):
        if table not in self._columns:
            raise EditError('Invalid registry table')
        if con is None:
            with self.connect() as db:
                return self.get(table, identity, db)
        row = con.execute(sql.SQL('SELECT payload FROM {} WHERE id=%s').format(sql.Identifier(table)),
                          (identity,)).fetchone()
        if row is None:
            raise EditError(f'{table} record not found')
        return row['payload']

    def list(self, table, column=None, value=None):
        if table not in self._columns or (column is not None and column != self._columns[table]):
            raise EditError('Invalid registry query')
        query = sql.SQL('SELECT payload FROM {}').format(sql.Identifier(table))
        if column:
            query += sql.SQL(' WHERE {}=%s').format(sql.Identifier(column))
        query += sql.SQL(' ORDER BY id')
        with self.connect() as con:
            return [r['payload'] for r in con.execute(query, (value,) if column else ())]

    def active_set(self, con=None):
        if con is None:
            with self.connect() as db:
                return self.active_set(db)
        return {r['id']: r['active'] for r in con.execute(
            'SELECT id,active FROM patterns WHERE published AND enabled AND active IS NOT NULL')}

    def create_pattern(self, pattern_id, *, display_name=None, enabled=True, con=None):
        if con is None:
            with self.transaction() as db:
                return self.create_pattern(pattern_id, display_name=display_name, enabled=enabled, con=db)
        con.execute('INSERT INTO patterns(id,display_name,enabled) VALUES(%s,%s,%s) '
                    'ON CONFLICT(id) DO NOTHING', (pattern_id, display_name or pattern_id, enabled))
        return self.lock_pattern(pattern_id, con)

    def lock_pattern(self, pattern_id, con):
        row = con.execute('SELECT * FROM patterns WHERE id=%s FOR UPDATE', (pattern_id,)).fetchone()
        if row is None:
            raise EditError('Pattern not found')
        return row

    def insert_version(self, version, con=None):
        """Allocate a number and insert immutable source links under the pattern lock."""
        if con is None:
            with self.transaction() as db:
                return self.insert_version(version, db)
        pattern = self.lock_pattern(version['pattern_id'], con)
        payload = dict(version, version_number=pattern['next_version'])
        payload.setdefault('version_id', uid())
        payload.setdefault('parent_version_id', None)
        canonical(payload)  # reject nonfinite JSON before persistence
        files = payload.get('files', {})
        for name, ref in files.items():
            safe_relative(name)
            self.read_blob(ref, con)
        con.execute('UPDATE patterns SET next_version=next_version+1 WHERE id=%s', (pattern['id'],))
        con.execute('INSERT INTO versions(id,pattern,number,parent,import_batch,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                    (payload['version_id'], pattern['id'], payload['version_number'],
                     payload['parent_version_id'], payload.get('import_batch_id'), Jsonb(payload)))
        for name, ref in files.items():
            con.execute('INSERT INTO version_files(version,path,sha256,media_type) VALUES(%s,%s,%s,%s)',
                        (payload['version_id'], name, ref['sha256'], ref['media_type']))
        con.execute('INSERT INTO version_lifecycle(version) VALUES(%s)', (payload['version_id'],))
        return payload

    def update_pattern(self, pattern_id, expected_generation, *, active, published, enabled, con=None):
        """Storage CAS; caller must establish domain validation/publication eligibility."""
        if con is None:
            with self.transaction() as db:
                return self.update_pattern(pattern_id, expected_generation, active=active,
                                           published=published, enabled=enabled, con=db)
        row = self.lock_pattern(pattern_id, con)
        if row['generation'] != expected_generation:
            raise Conflict('Pattern changed; reload before retrying')
        result = con.execute('UPDATE patterns SET active=%s,published=%s,enabled=%s,generation=generation+1 '
                             'WHERE id=%s AND generation=%s RETURNING *',
                             (active, published, enabled, pattern_id, expected_generation)).fetchone()
        if result is None:
            raise Conflict('Pattern changed; reload before retrying')
        return result

    def save_session(self, session, expected=None, con=None):
        if con is None:
            with self.transaction() as db:
                return self.save_session(session, expected, db)
        canonical(session)
        if expected is None:
            if session['generation'] != 0:
                raise EditError('New session generation must be zero')
            con.execute('INSERT INTO sessions(id,pattern,generation,payload) VALUES(%s,%s,%s,%s)',
                        (session['session_id'],session['pattern_id'],0,Jsonb(session)))
        else:
            if session['generation'] != expected + 1:
                raise EditError('Session generation must advance by one')
            result = con.execute('UPDATE sessions SET generation=%s,payload=%s '
                                 'WHERE id=%s AND pattern=%s AND generation=%s',
                                 (session['generation'],Jsonb(session),session['session_id'],session['pattern_id'],expected))
            if result.rowcount != 1:
                raise Conflict('Session changed; reload before retrying')
        return session

    def insert_revision(self, revision, con):
        canonical(revision)
        con.execute('INSERT INTO revisions(id,session,payload) VALUES(%s,%s,%s)',
                    (revision['revision_id'],revision['session_id'],Jsonb(revision)))

    def insert_report(self, report, con):
        canonical(report)
        con.execute('INSERT INTO reports(id,revision,version,batch,payload) VALUES(%s,%s,%s,%s,%s)',
                    (report['report_id'],report.get('revision_id'),report.get('version_id'),
                     report.get('batch_id'),Jsonb(report)))

    def save_worker(self, payload):
        canonical(payload)
        with self.transaction() as con:
            con.execute('INSERT INTO workers(id,heartbeat,payload) VALUES(%s,%s,%s) '
                        'ON CONFLICT(id) DO UPDATE SET heartbeat=EXCLUDED.heartbeat,payload=EXCLUDED.payload',
                        (payload['worker_id'],payload['heartbeat'],Jsonb(payload)))
