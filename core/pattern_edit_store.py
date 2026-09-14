"""Transactional editor registry and immutable content-addressed artifacts."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


class EditError(ValueError):
    pass


class Conflict(EditError):
    pass


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
    if not value or p.is_absolute() or '..' in p.parts or '\\' in value or str(p) != value:
        raise EditError('Invalid artifact/source path')
    return p


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
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
    """Connections are per transaction; safe across threads and processes.

    Artifacts are never garbage-collected automatically. A crash can leave an
    unreferenced blob, but cannot publish a partially written executable version.
    """
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.directory = self.root / 'pattern_versions'
        self.db = self.root / 'data/pattern_edit/registry.sqlite3'
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_migrations VALUES(1);
                CREATE TABLE IF NOT EXISTS patterns(
                    id TEXT PRIMARY KEY, active TEXT, generation INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS versions(
                    id TEXT PRIMARY KEY, pattern TEXT NOT NULL REFERENCES patterns(id),
                    number INTEGER NOT NULL, payload TEXT NOT NULL, UNIQUE(pattern,number));
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY, pattern TEXT NOT NULL REFERENCES patterns(id),
                    generation INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(
                    id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports(
                    id TEXT PRIMARY KEY, revision TEXT NOT NULL REFERENCES revisions(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id),
                    idempotency_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS activations(
                    id TEXT PRIMARY KEY, pattern TEXT NOT NULL REFERENCES patterns(id),
                    idempotency_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workers(
                    id TEXT PRIMARY KEY, heartbeat TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY, pattern TEXT NOT NULL REFERENCES patterns(id), payload TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS versions_immutable BEFORE UPDATE ON versions
                    BEGIN SELECT RAISE(ABORT, 'immutable version'); END;
                CREATE TRIGGER IF NOT EXISTS revisions_immutable BEFORE UPDATE ON revisions
                    BEGIN SELECT RAISE(ABORT, 'immutable revision'); END;
                CREATE TRIGGER IF NOT EXISTS reports_immutable BEFORE UPDATE ON reports
                    BEGIN SELECT RAISE(ABORT, 'immutable report'); END;
                COMMIT;
            ''')
            if con.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] != 1:
                raise EditError('Unsupported editor registry schema')

    def connect(self):
        con = sqlite3.connect(self.db, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA synchronous=FULL')
        return con

    @contextmanager
    def transaction(self):
        con = self.connect()
        try:
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def blob(self, data, media_type='application/json'):
        if not isinstance(data, bytes):
            data = canonical(data)
        sha = digest(data)
        rel = f'artifacts/{sha[:2]}/{sha}'
        path = self.directory / rel
        if path.exists():
            if path.is_symlink() or digest(path.read_bytes()) != sha:
                raise EditError('Corrupt immutable artifact')
        else:
            atomic_write(path, data)
        return {'path': rel, 'sha256': sha, 'media_type': media_type, 'size_bytes': len(data)}

    def read_blob(self, ref):
        path = self.directory / str(safe_relative(ref['path']))
        if path.is_symlink() or not path.resolve().is_relative_to(self.directory.resolve()):
            raise EditError('Artifact escapes storage root')
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise EditError('Required artifact is missing') from exc
        if len(data) != ref['size_bytes'] or digest(data) != ref['sha256']:
            raise EditError('Artifact hash/size mismatch')
        return data

    def get(self, table, identity, con=None):
        if table not in {'versions','sessions','revisions','reports','jobs','activations','workers','events'}:
            raise EditError('Invalid registry table')
        if con is None:
            with self.connect() as db:
                return self.get(table, identity, db)
        row = con.execute(f'SELECT payload FROM {table} WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise EditError(f'{table} record not found')
        return json.loads(row[0])

    def list(self, table, column=None, value=None):
        columns = {'versions':'pattern','sessions':'pattern','revisions':'session','reports':'revision',
                   'jobs':'session','activations':'pattern','events':'pattern','workers':None}
        if table not in columns or (column is not None and column != columns[table]):
            raise EditError('Invalid registry query')
        with self.connect() as con:
            sql = f'SELECT payload FROM {table}'
            rows = con.execute(sql + (f' WHERE {column}=?' if column else ''), (value,) if column else ())
            return [json.loads(row[0]) for row in rows]

    def active_set(self, con=None):
        if con is None:
            with self.connect() as db:
                return self.active_set(db)
        return {r['id']:r['active'] for r in con.execute('SELECT id,active FROM patterns WHERE active IS NOT NULL')}

    def save_session(self, session, expected=None, con=None):
        if con is None:
            with self.transaction() as db:
                return self.save_session(session, expected, db)
        if expected is None:
            con.execute('INSERT INTO sessions VALUES(?,?,?,?)',
                        (session['session_id'],session['pattern_id'],session['generation'],canonical(session).decode()))
        else:
            result = con.execute('UPDATE sessions SET generation=?,payload=? WHERE id=? AND generation=?',
                                 (session['generation'],canonical(session).decode(),session['session_id'],expected))
            if result.rowcount != 1:
                raise Conflict('Session changed; reload before retrying')
        return session

    def insert_revision(self, revision, con):
        con.execute('INSERT INTO revisions VALUES(?,?,?)',
                    (revision['revision_id'],revision['session_id'],canonical(revision).decode()))

    def insert_report(self, report, con):
        con.execute('INSERT INTO reports VALUES(?,?,?)',
                    (report['report_id'],report['revision_id'],canonical(report).decode()))
