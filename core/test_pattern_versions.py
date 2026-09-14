from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
from pathlib import Path

import pytest

from core.pattern_edit_store import EditStore, EditError, Conflict, canonical, uid
from core.pattern_versions import PatternVersions


def repository(root):
    (root/'patterns').mkdir()
    (root/'patterns/008_head_and_shoulders.py').write_text('from patterns._helper import VALUE\nVALUE2=VALUE\n')
    (root/'patterns/008_head_and_shoulders.md').write_text('Original rules')
    (root/'patterns/_helper.py').write_text('VALUE=42\n')
    return root


def create_baseline(root):
    return PatternVersions(EditStore(root)).baseline('pattern_008_head_and_shoulders')['version_id']


def test_two_process_baseline(tmp_path):
    repository(tmp_path)
    with ProcessPoolExecutor(2, mp_context=multiprocessing.get_context('spawn')) as pool:
        ids = list(pool.map(create_baseline, [str(tmp_path)]*2))
    assert ids[0] == ids[1]
    assert len(EditStore(tmp_path).list('versions')) == 1


def test_snapshots_and_corruption(tmp_path):
    versions = PatternVersions(EditStore(repository(tmp_path)))
    version = versions.baseline('pattern_008_head_and_shoulders')
    assert 'patterns/_helper.py' in version['files']
    ref = version['files']['patterns/_helper.py']
    (versions.store.directory/ref['path']).write_text('corruption')
    with pytest.raises(EditError, match='mismatch'):
        versions.verify(version['version_id'])


def test_manual_edit_preserved(tmp_path):
    versions = PatternVersions(EditStore(repository(tmp_path)))
    versions.baseline('pattern_008_head_and_shoulders')
    source = tmp_path/'patterns/008_head_and_shoulders.py'
    source.write_text('manual edit')
    with pytest.raises(Conflict):
        versions.baseline('pattern_008_head_and_shoulders')
    assert source.read_text() == 'manual edit'


def test_session_reopen_and_concurrency(tmp_path):
    store = EditStore(repository(tmp_path))
    baseline = PatternVersions(store).baseline('pattern_008_head_and_shoulders')
    session = {'session_id':uid(),'pattern_id':baseline['pattern_id'],'generation':0,
               'messages':[{'text':'Later shoulder','anchors':[{'role':'RS','index':210}]}]}
    store.save_session(session)
    reopened = EditStore(tmp_path)
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


def test_artifacts_reject_paths_missing_and_symlinks(tmp_path):
    store = EditStore(repository(tmp_path))
    ref = store.blob(b'keep forever')
    assert EditStore(tmp_path).read_blob(ref) == b'keep forever'
    with pytest.raises(EditError):
        store.read_blob({**ref,'path':'../secret'})
    path = store.directory/ref['path']
    path.unlink()
    with pytest.raises(EditError,match='missing'):
        store.read_blob(ref)
    path.symlink_to(tmp_path/'patterns/_helper.py')
    with pytest.raises(EditError,match='escapes'):
        store.read_blob(ref)
