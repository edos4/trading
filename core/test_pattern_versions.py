from concurrent.futures import ProcessPoolExecutor
import multiprocessing

import pytest

from core.pattern_edit_store import EditStore, EditError, Conflict, uid
from core.pattern_versions import PatternVersions


def repository(root):
    (root/'patterns').mkdir()
    (root/'patterns/008_head_and_shoulders.py').write_text('from patterns._helper import VALUE\nVALUE2=VALUE\n')
    (root/'patterns/008_head_and_shoulders.md').write_text('Original rules')
    (root/'patterns/_helper.py').write_text('VALUE=42\n')
    return root


def create_baseline(args):
    root, dsn, schema = args
    return PatternVersions(EditStore(root, dsn=dsn, schema=schema)).baseline('pattern_008_head_and_shoulders')['version_id']


def test_two_process_baseline(tmp_path, editor_store_factory):
    repository(tmp_path)
    with ProcessPoolExecutor(2, mp_context=multiprocessing.get_context('spawn')) as pool:
        ids = list(pool.map(create_baseline, [(str(tmp_path), editor_store_factory.dsn, editor_store_factory.schema)]*2))
    assert ids[0] == ids[1]
    assert len(editor_store_factory(tmp_path).list('versions')) == 1


def test_snapshots_and_corruption(tmp_path, editor_store_factory):
    versions = PatternVersions(editor_store_factory(repository(tmp_path)))
    version = versions.baseline('pattern_008_head_and_shoulders')
    assert 'patterns/_helper.py' in version['files']
    ref = version['files']['patterns/_helper.py']
    with pytest.raises(EditError, match='mismatch'):
        versions.store.read_blob({**ref, 'size_bytes': ref['size_bytes'] + 1})
    assert versions.verify(version['version_id']) == version
    assert not (tmp_path / 'pattern_versions').exists()


def test_manual_edit_preserved(tmp_path, editor_store_factory):
    versions = PatternVersions(editor_store_factory(repository(tmp_path)))
    versions.baseline('pattern_008_head_and_shoulders')
    source = tmp_path/'patterns/008_head_and_shoulders.py'
    source.write_text('manual edit')
    with pytest.raises(Conflict):
        versions.baseline('pattern_008_head_and_shoulders')
    assert source.read_text() == 'manual edit'


def test_session_reopen_and_concurrency(tmp_path, editor_store_factory):
    store = editor_store_factory(repository(tmp_path))
    baseline = PatternVersions(store).baseline('pattern_008_head_and_shoulders')
    session = {'session_id':uid(),'pattern_id':baseline['pattern_id'],'generation':0,
               'messages':[{'text':'Later shoulder','anchors':[{'role':'RS','index':210}]}]}
    store.save_session(session)
    reopened = editor_store_factory(tmp_path)
    assert reopened.get('sessions',session['session_id']) == session
    session['generation'] = 1
    reopened.save_session(session,0)
    with pytest.raises(Conflict):
        store.save_session(session,0)
    with pytest.raises(RuntimeError):
        with store.transaction() as con:
            con.execute('DELETE FROM sessions')
            raise RuntimeError('interrupted')
    assert store.get('sessions',session['session_id'])['generation'] == 1


def test_artifacts_reject_paths_and_missing_blobs(tmp_path, editor_store_factory):
    store = editor_store_factory(repository(tmp_path))
    ref = store.blob(b'keep forever')
    assert editor_store_factory(tmp_path).read_blob(ref) == b'keep forever'
    with pytest.raises(EditError):
        store.read_blob({**ref, 'path': '../secret'})
    with pytest.raises(EditError, match='missing'):
        store.read_blob({**ref, 'sha256': '0' * 64})
    assert not (tmp_path / 'pattern_versions').exists()
    assert not (tmp_path / 'data/pattern_edit').exists()


def test_legacy_activation_recovery_never_rewrites_files(tmp_path, editor_store_factory):
    from psycopg.types.json import Jsonb

    store = editor_store_factory(repository(tmp_path))
    versions = PatternVersions(store)
    baseline = versions.baseline('pattern_008_head_and_shoulders')
    assert versions.recover() == []
    before = (tmp_path/'patterns/008_head_and_shoulders.py').read_bytes()
    with store.transaction() as con:
        con.execute('INSERT INTO activations(id,pattern,idempotency_key,payload) VALUES(%s,%s,%s,%s)',
                    ('old',baseline['pattern_id'],'old',Jsonb({'state':'staged'})))
    with pytest.raises(Conflict, match='reconciliation'):
        versions.recover()
    assert (tmp_path/'patterns/008_head_and_shoulders.py').read_bytes() == before
    assert store.active_set() == {}
