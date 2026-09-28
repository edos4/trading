"""Source inspection and immutable PostgreSQL snapshots; bootstrap in pattern_bootstrap."""
from __future__ import annotations

import ast
import importlib.metadata
from pathlib import Path
import sys

from core.pattern_edit_store import (EditError, Conflict, canonical, digest, now,
                                     open_pattern_store, safe_relative, uid)


def runtime_manifest(root):
    root = Path(root)
    files = {}
    # Trusted adapters are compatibility inputs, never model-editable files.
    for name in ('patterns/base_pattern.py','analysis/indicator_engine.py','data/ohlcv_store.py',
                 'core/backtester.py','core/engine_defaults.py','core/market.py'):
        path = root / name
        if path.exists():
            # Detector/helper sources are versioned blobs, not trusted runtime.
            for dependency, data in collect_sources(root, name, paired=False, prune_package='patterns').items():
                if not dependency.startswith('patterns/') or dependency == 'patterns/base_pattern.py':
                    files[dependency] = digest(data)
    return {'python':sys.version, 'cache_tag':sys.implementation.cache_tag, 'files':files,
            'packages':{d.metadata['Name']: d.version for d in importlib.metadata.distributions()
                        if d.metadata['Name']}}


def pattern_source(pattern_id):
    if not pattern_id.startswith('pattern_'):
        raise EditError('Select a file-backed chart pattern')
    name = pattern_id.removeprefix('pattern_')
    safe_relative(name)
    if '/' in name or not name[:3].isdigit():
        raise EditError('Invalid pattern identity')
    return f'patterns/{name}.py'


def collect_sources(root, source, paired=True, *, strict=False, prune_package=None):
    """Static transitive repository imports; no source execution is needed.

    prune_package excludes a package subtree from the dependency walk. The
    trusted runtime fingerprint uses it to stay independent of detector
    sources, so execution never depends on files the editor may not ship.
    """
    root = Path(root).resolve()
    prune_prefix = f'{prune_package}/' if prune_package else None
    pending, found = [source] + ([str(Path(source).with_suffix('.md'))] if paired else []), {}
    while pending:
        name = pending.pop()
        if name in found:
            continue
        path = root / str(safe_relative(name))
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise EditError('Source symlinks are not allowed')
        if not path.is_file():
            raise EditError(f'Missing required source: {name}')
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise Conflict('Source changed during collection: ' + name)
        found[name] = data
        for parent in Path(name).parents:
            initializer = str(parent / '__init__.py')
            if (parent != Path('.') and (root / initializer).is_file() and initializer not in found
                    and not (prune_prefix and initializer.startswith(prune_prefix))):
                pending.append(initializer)
        if not name.endswith('.py'):
            continue
        tree = ast.parse(data, filename=name)
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ''
                if node.level:
                    parts = name[:-3].split('/')[:-node.level]
                    prefix = '.'.join(parts + ([prefix] if prefix else []))
                modules = [prefix] + [f'{prefix}.{a.name}' for a in node.names]
            if strict and modules:
                required = [a.name for a in node.names] if isinstance(node, ast.Import) else modules[:1]
                for module in required:
                    if module.split('.')[0] in {'patterns', 'analysis', 'data', 'core', 'utils', 'config'}:
                        relative = module.replace('.', '/')
                        if not any((root / p).is_file() for p in (relative + '.py', relative + '/__init__.py')):
                            raise EditError('Missing repository dependency: ' + module)
            for module in modules:
                rel = module.replace('.', '/')
                for candidate in (rel+'.py', rel+'/__init__.py'):
                    if ((root / candidate).is_file() and candidate not in found
                            and not (prune_prefix and candidate.startswith(prune_prefix))):
                        pending.append(candidate)
    return found


class PatternVersions:
    def __init__(self, store=None):
        self.store = store or open_pattern_store()

    def verify(self, version_id, *, runtime=False):
        version = self.store.get('versions', version_id)
        for ref in version['files'].values():
            self.store.read_blob(ref)
        if runtime and version.get('provenance') == 'file-import':
            for name, ref in version['files'].items():
                # Application settings are live inputs, not frozen detector code.
                if name == 'config.py':
                    continue
                if name.startswith('patterns/') and name != 'patterns/base_pattern.py':
                    continue
                path = self.store.root / name
                if not path.is_file() or digest(path.read_bytes()) != ref['sha256']:
                    raise EditError('Version runtime dependency is incompatible: ' + name)
        if runtime:
            recorded = version['runtime']
            current = runtime_manifest(self.store.root)
            # Installed package versions and unrelated application modules are
            # recorded for provenance, not enforced: a dependency upgrade or a
            # change to code the detector never imports does not make the
            # interface incompatible, and every candidate is validated against
            # the current environment in the sandbox anyway. The Python build is
            # still enforced here, and the version's own trusted files are
            # checked above.
            if (recorded.get('python') != current.get('python')
                    or recorded.get('cache_tag') != current.get('cache_tag')):
                raise EditError('Version runtime is incompatible; restore its runtime before execution')
        return version

    def mirror_matches(self, version):
        for name in version['mirror_paths']:
            path = self.store.root / name
            if path.is_symlink() or not path.resolve().is_relative_to(self.store.root):
                return False
            if not path.exists() or digest(path.read_bytes()) != version['files'][name]['sha256']:
                return False
        return True

    def baseline(self, pattern_id):
        with self.store.transaction() as con:
            # Compatibility snapshot API: never publishes an unverified baseline.
            # P03 supplies the full inventory/parity/publication workflow.
            self.store.create_pattern(pattern_id, con=con)
            existing = con.execute('SELECT id FROM versions WHERE pattern=%s AND number=1', (pattern_id,)).fetchone()
            if existing:
                version = self.verify(existing['id'])
                if not self.mirror_matches(version):
                    raise Conflict('Baseline source was manually changed; reconcile before editing')
                return version
            source = pattern_source(pattern_id)
            files = collect_sources(self.store.root, source)
            version_id = uid()
            version = {'schema_version':1, 'version_id':version_id, 'pattern_id':pattern_id,
                       'version_number':1, 'parent_version_id':None, 'restored_from_version_id':None,
                       'provenance':'trusted-snapshot', 'created_at':now(), 'actor':'baseline', 'description':'Original source baseline',
                       'explanation':'Snapshot before the first edit', 'source_path':source,
                       'mirror_paths':[source,str(Path(source).with_suffix('.md'))],
                       'files':{name:self.store.blob(data, 'text/plain', con) for name,data in files.items()},
                       'runtime':runtime_manifest(self.store.root), 'candidate_sha256':None,
                       'validation_report_id':None, 'session_id':None}
            version['content_sha256'] = digest(canonical(version['files']))
            # Verify input did not change during the dependency snapshot.
            if any((self.store.root/name).read_bytes() != data for name,data in files.items()):
                raise Conflict('Source changed during baseline snapshot')
            return self.store.insert_version(version, con)

    def recover(self):
        """Do not replay legacy filesystem activation journals against PostgreSQL."""
        journals = self.store.list('activations')
        if any(j.get('state') in ('staged', 'registered') for j in journals):
            raise Conflict('Legacy source-mirror activation requires explicit reconciliation')
        return []

    def catalog(self):
        with self.store.connect() as con:
            rows = con.execute('SELECT * FROM patterns WHERE published ORDER BY id').fetchall()
        if not rows:
            raise EditError('Pattern catalog is empty; run scripts/import_pattern_baselines.py explicitly')
        return rows

    def list_versions(self, pattern_id, *, include_archived=False):
        with self.store.connect() as con:
            return con.execute('''SELECT v.payload, v.created_at AS created_at, l.* FROM versions v
                JOIN version_lifecycle l ON l.version=v.id JOIN patterns p ON p.id=v.pattern
                WHERE p.published AND p.id=%s AND (%s OR l.archived_at IS NULL)
                ORDER BY v.number''', (pattern_id, include_archived)).fetchall()

    def detail(self, version_id):
        with self.store.connect() as con:
            version = self.store.get('versions', version_id, con)
            created_at = con.execute('SELECT created_at FROM versions WHERE id=%s',
                                     (version_id,)).fetchone()['created_at']
            lifecycle = con.execute('SELECT * FROM version_lifecycle WHERE version=%s', (version_id,)).fetchone()
            reports = con.execute('SELECT payload FROM reports WHERE version=%s OR id=%s OR id=%s',
                                  (version_id, lifecycle['validation_report_id'], lifecycle['baseline_report_id'])).fetchall()
            runs = con.execute('''SELECT v.run AS run_id, b.payload, j.state, r.payload AS result
                FROM run_versions v
                JOIN backtest_runs b ON b.id=v.run JOIN jobs j ON j.id=b.job
                LEFT JOIN results r ON r.run=b.id WHERE v.version=%s ORDER BY b.created_at DESC''', (version_id,)).fetchall()
        return dict(version=version, created_at=created_at, lifecycle=lifecycle,
                    reports=[r['payload'] for r in reports], backtests=runs)

    def source(self, version_id):
        version = self.store.get('versions', version_id)
        source = version['source_path']
        return {name: self.store.read_blob(version['files'][name])
                for name in (source, str(Path(source).with_suffix('.md')))}

    def diff(self, version_id, base_version_id=None):
        import difflib
        version = self.store.get('versions', version_id)
        base_id = base_version_id or version['parent_version_id']
        if base_id and self.store.get('versions', base_id)['pattern_id'] != version['pattern_id']:
            raise EditError('Diff base must belong to the same pattern')
        before = self.source(base_id) if base_id else {}
        return {name: ''.join(difflib.unified_diff(
                    before.get(name, b'').decode('utf-8').splitlines(keepends=True),
                    data.decode('utf-8').splitlines(keepends=True), fromfile='base/'+name, tofile='version/'+name))
                for name, data in self.source(version_id).items()}

    def _eligible(self, version_id, pattern_id, con, *, default=False):
        version = self.store.get('versions', version_id, con)
        life = con.execute('SELECT * FROM version_lifecycle WHERE version=%s', (version_id,)).fetchone()
        if version['pattern_id'] != pattern_id:
            raise EditError('Version must belong to the selected pattern')
        if life['archived_at'] is not None:
            raise EditError('Archived versions cannot be selected for new runs or defaults')
        if version.get('metadata') is None or version.get('metadata', {}).get('skipped', True):
            raise EditError('Skipped or unvalidated version is unavailable')
        if life['validation'] != 'passed':
            raise EditError('Version requires successful validation')
        baseline = False
        if version.get('provenance') == 'file-import' and life['baseline_report_id']:
            report = self.store.get('reports', life['baseline_report_id'], con)
            baseline = (report.get('status') == 'passed' and report.get('batch_id') == version.get('import_batch_id')
                        and version_id in report.get('byte_checks', []))
        if not baseline:
            report_id = life['validation_report_id']
            report = self.store.get('reports', report_id, con) if report_id else {}
            if report.get('version_id') != version_id or report.get('status') != 'passed':
                raise EditError('Version requires successful validation evidence')
            if default:
                success = con.execute('''SELECT 1 FROM backtest_runs b JOIN jobs j ON j.id=b.job
                    JOIN run_versions v ON v.run=b.id JOIN results r ON r.run=b.id
                    WHERE b.id=%s AND v.version=%s AND j.state='completed' ''',
                                      (life['successful_run_id'], version_id)).fetchone()
                if not success:
                    raise EditError('Default requires a successful backtest')
        return version

    def resolve(self, selected=None, disabled=()):
        """Pin once at submission/start. Workers load these IDs without resolving again."""
        pinned = getattr(self.store, 'pinned', None)
        if pinned is not None:
            # A remote registry decides eligibility where the lifecycle, report
            # and run rows live; a client cannot compute the verdict.
            return pinned(disabled=tuple(disabled), selected=selected)
        with self.store.transaction() as con:
            rows = con.execute('SELECT * FROM patterns WHERE published ORDER BY id FOR SHARE').fetchall()
            if not rows:
                raise EditError('Pattern catalog is empty; run scripts/import_pattern_baselines.py explicitly')
            available = {p['id']: p for p in rows if p['enabled'] and p['id'] not in disabled}
            chosen = {p: r['active'] for p, r in available.items()} if selected is None else dict(selected)
            for pattern_id, version_id in chosen.items():
                if pattern_id not in available:
                    raise EditError('Pattern is unavailable: ' + pattern_id)
                self._eligible(version_id, pattern_id, con)
            return chosen

    def set_default(self, request, *, actor='user'):
        from core.pattern_editor_contracts import DefaultChange
        return self._change(DefaultChange.model_validate(request), 'default', actor)

    def archive(self, request, *, actor='user'):
        from core.pattern_editor_contracts import ArchiveVersion
        return self._change(ArchiveVersion.model_validate(request), 'archive', actor)

    def _change(self, request, operation, actor):
        from psycopg.types.json import Jsonb
        data = dict(operation=operation, actor=actor, **request.model_dump(mode='json'))
        with self.store.transaction() as con:
            # Lock the idempotency key before the pattern, including cross-pattern retries.
            con.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                        (self.store.schema + ':' + request.idempotency_key,))
            prior = con.execute('SELECT payload FROM activations WHERE idempotency_key=%s',
                                (request.idempotency_key,)).fetchone()
            if prior:
                if prior['payload'].get('request') != data:
                    raise Conflict('Idempotency key was used for a different request')
                return prior['payload']['result']
            pattern = self.store.lock_pattern(request.pattern_id, con)
            if not pattern['published']:
                raise EditError('Pattern is not published; complete explicit bootstrap')
            if pattern['generation'] != request.expected_generation:
                raise Conflict('Pattern changed; reload before retrying')
            version = self.store.get('versions', request.version_id, con)
            if version['pattern_id'] != request.pattern_id:
                raise EditError('Version must belong to the selected pattern')
            active = pattern['active']
            if operation == 'default':
                self._eligible(request.version_id, request.pattern_id, con, default=True)
                active = request.version_id
            else:
                replacement = request.replacement_default_version_id
                if active == request.version_id and not replacement:
                    raise EditError('Archiving the default requires a replacement')
                if replacement:
                    if active != request.version_id or replacement == request.version_id:
                        raise EditError('Replacement is only valid when archiving the current default')
                    self._eligible(replacement, request.pattern_id, con, default=True)
                    active = replacement
                # A valid surviving default protects the last usable version.
                self._eligible(active, request.pattern_id, con, default=True)
                con.execute('UPDATE version_lifecycle SET archived_at=COALESCE(archived_at,clock_timestamp()), '
                            'generation=generation+1 WHERE version=%s', (request.version_id,))
            result = self.store.update_pattern(pattern['id'], pattern['generation'], active=active,
                       published=True, enabled=pattern['enabled'], con=con)
            event = dict(request=data, result=result, created_at=now(), event_id=uid())
            con.execute('INSERT INTO activations(id,pattern,idempotency_key,payload) VALUES(%s,%s,%s,%s)',
                        (event['event_id'], pattern['id'], request.idempotency_key, Jsonb(event)))
            con.execute('INSERT INTO events(id,pattern,payload) VALUES(%s,%s,%s)',
                        (event['event_id'], pattern['id'], Jsonb(event)))
            return result
