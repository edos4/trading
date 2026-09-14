import base64
import io
import json
import time
from pathlib import Path

import pytest
from PIL import Image

from analysis.pattern_edit_ai import DeepSeekEditor, parse_response
from core.pattern_edit_service import PatternEditService
from core.pattern_edit_store import EditStore, EditError, Conflict
from core.pattern_versions import PatternVersions
from core.pattern_edit_worker import CandidateRunner, SandboxUnavailable
from core.test_pattern_versions import repository


def png():
    image=Image.new('RGB',(32,32),'blue');stream=io.BytesIO();image.save(stream,format='PNG')
    return base64.b64encode(stream.getvalue()).decode()


def chart():
    return {'edit_context':{'trade_id':'trade-1','pattern_id':'pattern_008_head_and_shoulders','market':'us',
            'pattern_version_id':None,'dataset':{'market':'us','symbol':'TEST','timeframe':'1d',
            'session_timezone':'America/New_York','candles':[{'time':'2024-01-02','timestamp':'2024-01-02T00:00:00-05:00',
            'open':100,'high':101,'low':99,'close':100,'volume':1000}]}}}


def wait(service,job):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        value=service.store.get('jobs',job['job_id'])
        if value['state'] not in ('queued','running'):return value
        time.sleep(.01)
    pytest.fail('Job did not terminate')


def provider(request):
    context=json.loads(request['messages'][1]['content'][0]['text'])
    files={p:context['source'][p]+'\n' for p in context['allowed_paths']}
    return {'id':'mock-request','model':'deepseek-flash','choices':[{'message':{'content':json.dumps({
        'files':files,'description':'General correction','explanation':'Later confirmed shoulder',
        'unresolved_questions':[]})}}]}


def test_mock_multimodal_draft_reopen_and_failed_apply(tmp_path):
    root=repository(tmp_path)
    store=EditStore(root)
    captured=[]
    def transport(request):captured.append(request);return provider(request)
    service=PatternEditService(store,DeepSeekEditor(transport))
    session=service.create(chart())
    service.message(session['session_id'],'Prefer this shoulder',[{'dataset_index':0,'role':'RS','snap':'high'}],png())
    job=service.submit(session['session_id'],idempotency_key='once')
    assert service.submit(session['session_id'],idempotency_key='once')['job_id']==job['job_id']
    assert wait(service,job)['state']=='complete'
    result=service.read(session['session_id'])
    assert result['state']=='needs-revision'
    assert result['revision']['returned_model']=='deepseek-flash'
    assert result['report']['ready'] is False
    assert captured[0]['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    reopened=PatternEditService(EditStore(root),DeepSeekEditor(provider))
    assert reopened.read(session['session_id'])['revision']==result['revision']
    with pytest.raises(EditError):
        service.apply(session['session_id'],result['current_revision_id'],result['report_id'],result['revision']['candidate_sha256'],'apply')
    assert (root/'patterns/008_head_and_shoulders.py').read_text().endswith('VALUE2=VALUE\n')
    service.executor.shutdown();reopened.executor.shutdown()


def test_selection_rejects_forecast_outside_data_and_invalidates_report(tmp_path):
    service=PatternEditService(EditStore(repository(tmp_path)),DeepSeekEditor(provider))
    session=service.create(chart())
    with pytest.raises(EditError,match='outside'):
        service.message(session['session_id'],'change',[{'dataset_index':1,'role':'RS'}],png())
    state=service.message(session['session_id'],'change',[{'dataset_index':0,'role':'RS','snap':'high'}],png())
    assert state['messages'][0]['anchors'][0]['price']==101
    assert state['generation']==1 and state['report_id'] is None
    service.executor.shutdown()


def test_provider_rejects_expanded_scope():
    with pytest.raises(EditError,match='exactly'):
        parse_response(json.dumps({'files':{'core/backtester.py':'bad'},'description':'x','explanation':'x','unresolved_questions':[]}),['patterns/a.py'])


def test_sandbox_missing_configuration_fails_closed(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings,'pattern_edit_cgroup_root','')
    monkeypatch.setattr(settings,'pattern_edit_worker_python','')
    with pytest.raises(SandboxUnavailable):
        CandidateRunner().run({'candidate.py':'raise AssertionError("must not run")'}, {})


def test_rollback_creates_reviewable_draft(tmp_path):
    root=repository(tmp_path)
    service=PatternEditService(EditStore(root),DeepSeekEditor(provider))
    session=service.create(chart())
    draft=service.rollback(session['session_id'],session['base_version_id'],'revert a regression')
    assert draft['session_id']!=session['session_id']
    assert draft['state']=='draft'
    assert draft['revision']['restored_from_version_id']==session['base_version_id']
    assert draft['revision']['description']=='Restore v1: revert a regression'
    # The restored draft is a normal revision: original session history is intact.
    assert service.read(session['session_id'])['state']=='draft'
    service.executor.shutdown()
