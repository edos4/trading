"""Baseline snapshots, dependency manifests, and recoverable source mirrors."""
from __future__ import annotations

import ast
import importlib.metadata
from pathlib import Path
import sys

from core.pattern_edit_store import EditStore, EditError, Conflict, atomic_write, canonical, digest, now, uid, safe_relative


def runtime_manifest(root):
    root = Path(root)
    files = {}
    # Trusted adapters are compatibility inputs, never model-editable files.
    for name in ('patterns/base_pattern.py','analysis/indicator_engine.py','data/ohlcv_store.py',
                 'core/backtester.py','core/engine_defaults.py','core/market.py'):
        path = root / name
        if path.exists():
            files[name] = digest(path.read_bytes())
    return {'python':sys.implementation.cache_tag, 'files':files,
            'packages':{p:importlib.metadata.version(p) for p in ('numpy','pandas')}}


def pattern_source(pattern_id):
    if not pattern_id.startswith('pattern_'):
        raise EditError('Select a file-backed chart pattern')
    name = pattern_id.removeprefix('pattern_')
    safe_relative(name)
    if '/' in name or not name[:3].isdigit():
        raise EditError('Invalid pattern identity')
    return f'patterns/{name}.py'


def collect_sources(root, source, paired=True):
    """Static transitive repository imports; no source execution is needed."""
    root = Path(root).resolve()
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
        data = path.read_bytes()
        found[name] = data
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
            for module in modules:
                rel = module.replace('.', '/')
                for candidate in (rel+'.py', rel+'/__init__.py'):
                    if (root / candidate).is_file() and candidate not in found:
                        pending.append(candidate)
    return found


class PatternVersions:
    def __init__(self, store=None):
        self.store = store or EditStore()

    def verify(self, version_id, *, runtime=False):
        version = self.store.get('versions', version_id)
        for ref in version['files'].values():
            self.store.read_blob(ref)
        if runtime and version['runtime'] != runtime_manifest(self.store.root):
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
            active = self.store.active_set(con).get(pattern_id)
            if active:
                version = self.verify(active)
                if not self.mirror_matches(version):
                    raise Conflict('Active source was manually changed; reconcile before editing')
                return version
            source = pattern_source(pattern_id)
            files = collect_sources(self.store.root, source)
            version_id = uid()
            version = {'schema_version':1, 'version_id':version_id, 'pattern_id':pattern_id,
                       'version_number':1, 'parent_version_id':None, 'restored_from_version_id':None,
                       'created_at':now(), 'actor':'baseline', 'description':'Original source baseline',
                       'explanation':'Snapshot before the first edit', 'source_path':source,
                       'mirror_paths':[source,str(Path(source).with_suffix('.md'))],
                       'files':{name:self.store.blob(data, 'text/plain') for name,data in files.items()},
                       'runtime':runtime_manifest(self.store.root), 'candidate_sha256':None,
                       'validation_report_id':None, 'session_id':None}
            version['content_sha256'] = digest(canonical(version['files']))
            # Verify input did not change during the dependency snapshot.
            if any((self.store.root/name).read_bytes() != data for name,data in files.items()):
                raise Conflict('Source changed during baseline snapshot')
            atomic_write(self.store.directory / pattern_id / version_id / 'manifest.json', canonical(version))
            con.execute('INSERT INTO patterns(id,active) VALUES(?,?)',(pattern_id,version_id))
            con.execute('INSERT INTO versions VALUES(?,?,?,?)',(version_id,pattern_id,1,canonical(version).decode()))
            return version

    def recover(self):
        """Complete only journaled writes whose old/new bytes still match.

        Unexpected manual edits are preserved and make recovery a conflict.
        Registry transactions serialize recovery with concurrent activation.
        """
        recovered = []
        with self.store.transaction() as con:
            rows = con.execute('SELECT id,payload FROM activations').fetchall()
            import json
            for row in rows:
                journal = json.loads(row['payload'])
                if journal['state'] not in ('staged','registered'):
                    continue
                old = self.store.get('versions',journal['previous_version_id'],con)
                new = self.store.get('versions',journal['registered_version_id'],con)
                for name in new['mirror_paths']:
                    path = self.store.root/name
                    if path.is_symlink() or not path.resolve().is_relative_to(self.store.root):
                        raise Conflict('Manual source path change prevents recovery')
                    current = digest(path.read_bytes()) if path.exists() else None
                    if current not in (old['files'][name]['sha256'],new['files'][name]['sha256']):
                        raise Conflict('Manual source change prevents recovery; files preserved')
                for name in new['mirror_paths']:
                    atomic_write(self.store.root/name,self.store.read_blob(new['files'][name]))
                active = self.store.active_set(con)[new['pattern_id']]
                if active not in (old['version_id'],new['version_id']):
                    raise Conflict('Activation journal conflicts with active version')
                con.execute('UPDATE patterns SET active=?,generation=generation+1 WHERE id=?',
                            (new['version_id'],new['pattern_id']))
                journal['state'] = 'worker-pending'
                con.execute('UPDATE activations SET payload=? WHERE id=?',(canonical(journal).decode(),row['id']))
                recovered.append(journal)
        return recovered
