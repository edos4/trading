"""P03 gates use real PostgreSQL and exact trusted source bytes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil

import pytest

from core.pattern_bootstrap import Bootstrap, freeze
from core.pattern_edit_store import ROOT, Conflict, EditError
from core.pattern_loader import VersionPattern


@pytest.fixture
def snapshot():
    return freeze(ROOT, disabled=[])


def test_complete_bootstrap_parity_and_rerun(editor_store_factory, snapshot):
    store = editor_store_factory()
    bootstrap = Bootstrap(store)
    batch = bootstrap.stage(snapshot)
    assert store.active_set() == {}
    report = bootstrap.verify_and_publish(batch, snapshot)
    assert report['status'] == 'passed'
    assert len(report['byte_checks']) == len(report['parity']) == 10
    assert len(store.active_set()) == 6
    assert bootstrap.stage(snapshot) == batch
    assert bootstrap.verify_and_publish(batch, snapshot)['report_id'] == report['report_id']
    versions = store.list('versions')
    assert len(versions) == 10
    assert all(v['version_number'] == 1 and v['parent_version_id'] is None for v in versions)
    snapshot.unchanged()


def test_concurrent_staging_and_changed_inputs(editor_store_factory, snapshot):
    bootstrap = Bootstrap(editor_store_factory())
    with ThreadPoolExecutor(2) as pool:
        batches = list(pool.map(bootstrap.stage, [snapshot, snapshot]))
    assert batches[0] == batches[1]
    changed = freeze(ROOT, disabled=['pattern_002_double_top'])
    with pytest.raises(Conflict, match='changed'):
        bootstrap.stage(changed)
    assert len(bootstrap.store.list('versions')) == 10
    assert bootstrap.store.active_set() == {}


def test_partial_stage_rolls_back(editor_store_factory, snapshot, monkeypatch):
    store = editor_store_factory()
    original = store.insert_version
    calls = []

    def fail(version, con):
        calls.append(version)
        if len(calls) == 4:
            raise RuntimeError('interrupted')
        return original(version, con)
    monkeypatch.setattr(store, 'insert_version', fail)
    with pytest.raises(RuntimeError):
        Bootstrap(store).stage(snapshot)
    assert store.list('versions') == []
    assert store.active_set() == {}
    monkeypatch.setattr(store, 'insert_version', original)
    Bootstrap(store).stage(snapshot)
    assert len(store.list('versions')) == 10


def test_failed_verification_unpublished(editor_store_factory, snapshot, monkeypatch):
    import core.pattern_bootstrap_parity as parity
    bootstrap = Bootstrap(editor_store_factory())
    batch = bootstrap.stage(snapshot)
    monkeypatch.setattr(parity, 'verify_parity', lambda *args: (_ for _ in ()).throw(EditError('parity failure')))
    with pytest.raises(EditError, match='parity failure'):
        bootstrap.verify_and_publish(batch, snapshot)
    assert bootstrap.store.active_set() == {}
    with bootstrap.store.connect() as con:
        state = con.execute('SELECT * FROM import_state WHERE batch=%s', (batch,)).fetchone()
    assert state['state'] == 'failed'
    assert bootstrap.store.get('reports', state['report_id'])['status'] == 'failed'
    assert bootstrap.stage(snapshot) == batch


def copy_snapshot(tmp_path, snapshot):
    for name, data in snapshot.files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    # runtime files outside detector import closure
    for name in snapshot.manifest['runtime']['files']:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    return tmp_path


def test_file_mutation_missing_docs_helpers(snapshot, tmp_path):
    from dataclasses import replace
    root = copy_snapshot(tmp_path, snapshot)
    frozen = replace(snapshot, root=root)
    target = root / 'patterns/002_double_top.md'
    target.write_bytes(b'changed')
    with pytest.raises(Conflict, match='changed'):
        frozen.unchanged()
    target.unlink()
    with pytest.raises(EditError, match='Missing required source'):
        freeze(root, disabled=[])
    target.write_bytes(snapshot.files['patterns/002_double_top.md'])
    (root / 'patterns/_channels.py').unlink()
    with pytest.raises((EditError, ImportError), match='dependency|Dependency'):
        freeze(root, disabled=[])


def test_load_without_detector_files_and_isolated_helpers(editor_store_factory, snapshot, tmp_path):
    root = copy_snapshot(tmp_path, snapshot)
    store = editor_store_factory(root)
    # stage against the original snapshot; byte-identical runtime in copied root
    Bootstrap(store).stage(snapshot)
    version = next(v for v in store.list('versions') if v['pattern_id'] == 'pattern_004_rounding_bottom')
    for path in (root / 'patterns').glob('*'):
        if path.name != 'base_pattern.py':
            path.unlink()
    first = VersionPattern(version['version_id'], store)
    second = VersionPattern(version['version_id'], store)
    assert first._baseline_modules['patterns._dedup'] is not second._baseline_modules['patterns._dedup']
    first._baseline_modules['patterns._dedup'].mark(42)
    assert not second._baseline_modules['patterns._dedup'].used(42)
    rationale = first._baseline_modules['patterns._rationale']
    module = rationale.importlib.import_module('patterns.005_rounding_top')
    assert module.__name__.startswith('_pattern_baseline_')
    (root / 'patterns/base_pattern.py').write_bytes(b'# changed interface')
    with pytest.raises(EditError, match='incompatible'):
        VersionPattern(version['version_id'], store)


def test_corrupt_readback_blocks_publication(editor_store_factory, snapshot, monkeypatch):
    store = editor_store_factory()
    bootstrap = Bootstrap(store)
    batch = bootstrap.stage(snapshot)
    original = store.read_blob
    source_ref = store.list('versions')[0]['files']['patterns/002_double_top.py']

    def corrupted(ref, con=None):
        data = original(ref, con)
        return data + b'corruption' if ref == source_ref else data
    monkeypatch.setattr(store, 'read_blob', corrupted)
    with pytest.raises(EditError, match='bytes differ'):
        bootstrap.verify_and_publish(batch, snapshot)
    assert store.active_set() == {}


def test_source_changes_while_collecting(snapshot, tmp_path, monkeypatch):
    import core.pattern_bootstrap as module
    root = copy_snapshot(tmp_path, snapshot)
    original = module.collect_sources
    calls = []

    def changing(root, source, *args, **kwargs):
        captured = original(root, source, *args, **kwargs)
        if not calls:
            path = root / 'patterns/_rules.py'
            path.write_bytes(path.read_bytes() + b'\n# changed during collection\n')
        calls.append(source)
        return captured
    monkeypatch.setattr(module, 'collect_sources', changing)
    with pytest.raises(Conflict, match='changed during collection'):
        module.freeze(root, disabled=[])


def test_ambiguous_class_rejected(snapshot, tmp_path):
    root = copy_snapshot(tmp_path, snapshot)
    path = root / 'patterns/002_double_top.py'
    path.write_bytes(path.read_bytes() + b'\nclass Duplicate(DoubleTopPattern):\n    pass\n')
    with pytest.raises(ValueError, match='exactly one'):
        freeze(root, disabled=[])


def test_unresolved_dynamic_import_rejected(snapshot, tmp_path):
    root = copy_snapshot(tmp_path, snapshot)
    path = root / 'patterns/002_double_top.py'
    path.write_bytes(path.read_bytes() + b'\nimport importlib\nimportlib.import_module("patterns.unrecorded")\n')
    with pytest.raises(EditError, match='Unresolved dynamic import'):
        freeze(root, disabled=[])


def test_concurrent_publication_and_atomic_retry(editor_store_factory, snapshot, monkeypatch):
    import core.pattern_bootstrap_parity as parity
    store = editor_store_factory()
    bootstrap = Bootstrap(store)
    batch = bootstrap.stage(snapshot)
    # This test isolates publication transactions; the full suite above executes
    # real clean-worker parity for every detector before publishing.
    monkeypatch.setattr(parity, 'verify_parity', lambda store, version, snapshot:
                        {'version_id': version['version_id'], 'transaction_test_double': True})
    original = store.update_pattern
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(args)
        if len(calls) == 4:
            raise RuntimeError('publication interrupted')
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'update_pattern', interrupted)
    with pytest.raises(RuntimeError, match='publication interrupted'):
        bootstrap.verify_and_publish(batch, snapshot)
    assert store.active_set() == {}
    with store.connect() as con:
        assert con.execute('SELECT count(*) AS n FROM patterns WHERE published').fetchone()['n'] == 0
    monkeypatch.setattr(store, 'update_pattern', original)
    with ThreadPoolExecutor(2) as pool:
        reports = list(pool.map(lambda _: bootstrap.verify_and_publish(batch, snapshot), range(2)))
    assert reports[0]['report_id'] == reports[1]['report_id']
    assert len(store.active_set()) == 6


def test_changed_source_conflicts_with_existing_batch(editor_store_factory, snapshot, tmp_path):
    from copy import deepcopy
    from dataclasses import replace
    from core.pattern_edit_store import digest
    bootstrap = Bootstrap(editor_store_factory())
    bootstrap.stage(snapshot)
    root = copy_snapshot(tmp_path, snapshot)
    name = 'patterns/002_double_top.py'
    changed_bytes = snapshot.files[name] + b'\n# later local edit\n'
    (root / name).write_bytes(changed_bytes)
    manifest = deepcopy(snapshot.manifest)
    manifest['files'][name] = dict(sha256=digest(changed_bytes), size_bytes=len(changed_bytes))
    changed = replace(snapshot, root=root, files={**snapshot.files, name: changed_bytes}, manifest=manifest)
    with pytest.raises(Conflict, match='inputs changed'):
        bootstrap.stage(changed)
    assert (root / name).read_bytes() == changed_bytes
