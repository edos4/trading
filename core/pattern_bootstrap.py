"""Explicit trusted working-tree bootstrap. Never invoked by application startup."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import subprocess

from psycopg.types.json import Jsonb

from core.pattern_edit_store import Conflict, EditError, canonical, digest, now, uid
from core.pattern_versions import collect_sources, runtime_manifest


def metadata(pattern):
    return dict(name=pattern.name, timeframes=pattern.timeframes, skipped=pattern.skipped,
                chart_description=pattern.chart_description,
                **{k: getattr(pattern, k, default) for k, default in
                   [('MIN_BARS', 2), ('HORIZON_BARS', 5), ('MAX_OPEN_PER_SYMBOL', None)]})


class FrozenFiles:
    """Read adapter for trusted in-memory source; never consults a registry."""
    def __init__(self, root, files):
        self.root, self.files = Path(root), files

    def read_blob(self, ref):
        data = self.files[ref['path']]
        if digest(data) != ref['sha256'] or len(data) != ref['size_bytes']:
            raise EditError('Frozen source mismatch')
        return data


def frozen_version(source, files):
    return dict(version_id=uid(), source_path=source,
                files={p: dict(path=p, sha256=digest(b), size_bytes=len(b)) for p, b in files.items()})


@dataclass
class Snapshot:
    root: Path
    files: dict[str, bytes]
    manifest: dict

    @property
    def sha256(self):
        # Git audit context is retained but unrelated edits do not change identity.
        return digest(canonical({k: v for k, v in self.manifest.items() if k != 'git'}))

    def unchanged(self):
        paths = sorted(p.relative_to(self.root).as_posix()
                       for p in (self.root / 'patterns').glob('[0-9]*.py'))
        if paths != [e['source_path'] for e in self.manifest['entries']]:
            raise Conflict('Detector inventory changed during collection')
        for name, data in self.files.items():
            path = self.root / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise Conflict('Source changed during collection: ' + name)
        if runtime_manifest(self.root) != self.manifest['runtime']:
            raise Conflict('Runtime changed during collection')


def freeze(root, disabled=None):
    """Include skipped/disabled detectors, actual bytes, and explicit dynamic edges."""
    from config import DISABLED_PATTERNS
    from core.pattern_loader import load_version_source
    root = Path(root).resolve()
    disabled = sorted(DISABLED_PATTERNS if disabled is None else disabled)
    runtime = runtime_manifest(root)
    sources = sorted(p.relative_to(root).as_posix() for p in (root / 'patterns').glob('[0-9]*.py'))
    if len(sources) != 10:
        raise EditError('Expected all ten numbered detector/document pairs')
    files = {}
    for source in sources:
        for path, data in collect_sources(root, source, strict=True).items():
            if path in files and files[path] != data:
                raise Conflict('Source changed during collection: ' + path)
            files[path] = data
    # The only dynamic pattern import currently supported is this literal map.
    tree = ast.parse(files['patterns/_rationale.py'])
    dynamic = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '_MODULES' for t in n.targets))
    for target in dynamic.values():
        if target.replace('.', '/') + '.py' not in files:
            raise EditError('Unresolved dynamic pattern dependency: ' + target)
    for path, data in files.items():
        if not path.endswith('.py') or not path.startswith('patterns/'):
            continue
        for n in ast.walk(ast.parse(data)):
            if isinstance(n, ast.Call) and ((isinstance(n.func, ast.Name) and n.func.id == '__import__') or
                    (isinstance(n.func, ast.Attribute) and n.func.attr == 'import_module')):
                if path != 'patterns/_rationale.py' or ast.unparse(n) != 'importlib.import_module(name)':
                    raise EditError('Unresolved dynamic import: ' + path)
    entries = []
    for source in sources:
        pattern, modules = load_version_source(FrozenFiles(root, files), frozen_version(source, files))
        expected = 'pattern_' + Path(source).stem
        if pattern.name != expected:
            raise EditError('Detector identity does not match source: ' + source)
        entries.append(dict(pattern_id=pattern.name, source_path=source,
                            documentation=str(Path(source).with_suffix('.md')),
                            class_name=type(pattern).__name__, metadata=metadata(pattern),
                            configured_disabled=pattern.name in disabled))
    def git(*args):
        """Git audit context, or None where the tree is not a checkout.

        The deployed app directory is rsynced without .git, so the manifest
        records no commit there instead of failing the whole import.
        """
        try:
            return subprocess.check_output(
                ['git', *args], cwd=root, text=True,
                stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    status = git('status', '--porcelain')
    manifest = dict(entries=entries, runtime=runtime, disabled_patterns=disabled,
                    dynamic_imports=dynamic,
                    files={p: dict(sha256=digest(b), size_bytes=len(b)) for p, b in sorted(files.items())},
                    git=dict(commit=git('rev-parse', 'HEAD'), status=status,
                             dirty=bool(status)))
    snapshot = Snapshot(root, files, manifest)
    snapshot.unchanged()
    return snapshot


class Bootstrap:
    def __init__(self, store):
        self.store = store

    def stage(self, snapshot):
        snapshot.unchanged()
        with self.store.transaction() as con:
            con.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema() || ':bootstrap'))")
            existing = con.execute('SELECT * FROM import_batches').fetchall()
            if existing:
                if len(existing) != 1 or existing[0]['snapshot_sha256'] != snapshot.sha256:
                    raise Conflict('Bootstrap inputs changed; reconcile the existing immutable import')
                return existing[0]['id']
            if con.execute('SELECT 1 FROM patterns LIMIT 1').fetchone():
                raise Conflict('Bootstrap requires an empty catalog')
            batch = uid()
            con.execute('INSERT INTO import_batches(id,snapshot_sha256,manifest) VALUES(%s,%s,%s)',
                        (batch, snapshot.sha256, Jsonb(snapshot.manifest)))
            con.execute('INSERT INTO import_state(batch) VALUES(%s)', (batch,))
            refs = {p: self.store.blob(b, 'text/plain', con) for p, b in snapshot.files.items()}
            for entry in snapshot.manifest['entries']:
                self.store.create_pattern(entry['pattern_id'], enabled=not (
                    entry['metadata']['skipped'] or entry['configured_disabled']), con=con)
                self.store.insert_version(dict(
                    **entry, version_id=uid(), parent_version_id=None, provenance='file-import',
                    actor='file-import', created_at=now(), import_batch_id=batch, files=refs,
                    runtime=snapshot.manifest['runtime'],
                    mirror_paths=[entry['source_path'], entry['documentation']],
                    content_sha256=digest(canonical(refs))), con)
            snapshot.unchanged()
        return batch

    def verify_and_publish(self, batch, snapshot):
        from core.pattern_bootstrap_parity import verify_parity
        with self.store.connect() as con:
            row = con.execute('SELECT * FROM import_batches WHERE id=%s', (batch,)).fetchone()
            if row is None or row['snapshot_sha256'] != snapshot.sha256:
                raise Conflict('Import snapshot mismatch')
            prior_state = con.execute('SELECT * FROM import_state WHERE batch=%s', (batch,)).fetchone()
            versions = [r['payload'] for r in con.execute(
                'SELECT payload FROM versions WHERE import_batch=%s ORDER BY pattern', (batch,))]
        report = dict(report_id=uid(), batch_id=batch, snapshot_sha256=snapshot.sha256,
                      status='failed', byte_checks=[], parity=[])
        try:
            if len(versions) != 10:
                raise EditError('Incomplete bootstrap batch')
            for version, entry in zip(versions, snapshot.manifest['entries'], strict=True):
                if version['pattern_id'] != entry['pattern_id'] or version['version_number'] != 1 or version['parent_version_id'] is not None:
                    raise EditError('Invalid baseline identity')
                if set(version['files']) != set(snapshot.files):
                    raise EditError('Incomplete baseline files')
                with self.store.connect() as con:
                    for path, data in snapshot.files.items():
                        if self.store.read_blob(version['files'][path], con) != data:
                            raise EditError('Imported bytes differ: ' + path)
                report['byte_checks'].append(version['version_id'])
                if prior_state['state'] != 'published':
                    report['parity'].append(verify_parity(self.store, version, snapshot))
            snapshot.unchanged()
            report['status'] = 'passed'
        except Exception as exc:
            report['error'] = str(exc)
            with self.store.transaction() as con:
                self.store.insert_report(report, con)
                con.execute("UPDATE import_state SET state='failed', report_id=%s, generation=generation+1 "
                            "WHERE batch=%s AND state <> 'published'", (report['report_id'], batch))
            raise
        if prior_state['state'] == 'published':
            return self.store.get('reports', prior_state['report_id'])
        with self.store.transaction() as con:
            con.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema() || ':bootstrap'))")
            state = con.execute('SELECT * FROM import_state WHERE batch=%s FOR UPDATE', (batch,)).fetchone()
            if state['state'] == 'published':
                return self.store.get('reports', state['report_id'], con)
            self.store.insert_report(report, con)
            for version in versions:
                pattern = self.store.lock_pattern(version['pattern_id'], con)
                if pattern['published'] or pattern['active'] is not None or pattern['next_version'] != 2:
                    raise Conflict('Staged catalog changed before publication')
                con.execute("UPDATE version_lifecycle SET baseline_report_id=%s, validation='passed' WHERE version=%s",
                            (report['report_id'], version['version_id']))
                expected_enabled = not (version['metadata']['skipped'] or version['configured_disabled'])
                if pattern['enabled'] != expected_enabled:
                    raise Conflict('Staged availability changed before publication')
                self.store.update_pattern(pattern['id'], pattern['generation'], active=version['version_id'],
                                          published=True, enabled=expected_enabled, con=con)
            con.execute("UPDATE import_state SET state='published', report_id=%s, generation=generation+1 WHERE batch=%s",
                        (report['report_id'], batch))
        return report

    def run(self, disabled=None):
        snapshot = freeze(self.store.root, disabled)
        batch = self.stage(snapshot)
        return self.verify_and_publish(batch, snapshot)
