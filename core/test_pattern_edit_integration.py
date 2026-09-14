"""Full-service integration: create -> chat -> preview -> validate -> apply -> rollback.

Uses a mocked provider and a fake candidate runner so it exercises the real
registry, artifact, activation and source-mirror code paths without the Q002
sandbox. This is the executable end-to-end contract for the shared service.
"""
from dataclasses import asdict
import json
import time

import pandas as pd

from analysis.pattern_edit_ai import DeepSeekEditor
from core.pattern_edit_service import PatternEditService
from core.pattern_edit_store import EditStore
from core.pattern_edit_validation import Validator, candle_rows
from core.pattern_edit_worker import SandboxUnavailable
from core.test_pattern_versions import repository
from patterns.base_pattern import TradeSignal

PATTERN = 'pattern_008_head_and_shoulders'


def _frame():
    return pd.DataFrame(
        {'open': [100.0, 101.0, 102.0], 'high': [101.0, 102.0, 103.0],
         'low': [99.0, 100.0, 101.0], 'close': [100.0, 101.0, 102.0],
         'volume': [1000, 1000, 1000]},
        index=pd.to_datetime(['2024-01-02', '2024-01-03', '2024-01-04']),
    )


def _dataset():
    return {'symbol': 'FIXTURE', 'market': 'us', 'timeframe': '1d',
            'session_timezone': 'America/New_York',
            'candles': candle_rows(_frame(), 'America/New_York')}


def _chart():
    return {'title': 'FIXTURE chart',
            'edit_context': {'trade_id': 'trade-1', 'pattern_id': PATTERN, 'market': 'us',
                             'pattern_version_id': None, 'provenance': 'legacy_unknown',
                             'dataset': _dataset()}}


class FakeRunner:
    """Returns a valid candidate signal that satisfies the selected anchor."""

    def __init__(self):
        row = _dataset()['candles'][2]
        self.signal = asdict(TradeSignal(
            symbol='FIXTURE', action='BUY', pattern=PATTERN, timeframe='1d', confidence=1,
            price=row['close'], qty=1,
            chart_annotations=[{'type': 'marker', 'date': row['time'],
                                'price': row['high'], 'label': 'RS'}],
        ))
        self.metadata = {'name': PATTERN, 'timeframes': ['1d'], 'MIN_BARS': 2,
                         'HORIZON_BARS': 5, 'MAX_OPEN_PER_SYMBOL': None}

    def run(self, files, request, cancel=None):
        if request.get('operation') == 'tests':
            return {'tests_passed': True, 'exit_code': 0}
        return {'signals': [self.signal], 'metadata': self.metadata}


def _image():
    import base64
    import io
    from PIL import Image
    stream = io.BytesIO()
    Image.new('RGB', (48, 32), 'blue').save(stream, format='PNG')
    return base64.b64encode(stream.getvalue()).decode()


def _provider(request):
    context = json.loads(request['messages'][1]['content'][0]['text'])
    files = {p: context['source'][p] + '\n' for p in context['allowed_paths']}
    return {'id': 'mock-request', 'model': 'deepseek-flash', 'choices': [{'message': {'content': json.dumps({
        'files': files, 'description': 'Later confirmed right shoulder',
        'explanation': 'Select the later confirmed RS and keep the stop above its high.',
        'unresolved_questions': []})}}]}


def _service(root):
    store = EditStore(root)
    return PatternEditService(store, DeepSeekEditor(_provider), Validator(store, FakeRunner()))


def _wait(service, job):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = service.store.get('jobs', job['job_id'])
        if value['state'] not in ('queued', 'running'):
            return value
        time.sleep(.01)
    raise AssertionError('job did not finish')


def test_end_to_end_preview_validate_apply_rollback(tmp_path):
    root = repository(tmp_path)
    service = _service(root)
    try:
        session = service.create(_chart())
        assert session['state'] == 'draft'
        base_version = session['base_version_id']
        original = (root / 'patterns/008_head_and_shoulders.py').read_bytes()

        state = service.message(session['session_id'], 'Prefer this later peak as the right shoulder',
                                [{'dataset_index': 2, 'role': 'RS', 'snap': 'high'}], _image())
        assert state['messages'][0]['anchors'][0]['price'] == 103.0

        job = service.submit(session['session_id'], idempotency_key='generate-1')
        assert _wait(service, job)['state'] == 'complete'

        result = service.read(session['session_id'])
        assert result['state'] == 'ready', result.get('report')
        assert result['report']['ready'] is True
        assert result['revision']['requested_model'] == 'deepseek-flash'
        assert result['revision']['returned_model'] == 'deepseek-flash'
        assert 'Later confirmed right shoulder' in result['revision']['description']
        assert '@@' in result['diff']

        activation = service.apply(session['session_id'], result['current_revision_id'],
                                   result['report_id'], result['revision']['candidate_sha256'], 'apply-1')
        assert activation['activation']['state'] == 'active'
        assert activation['state'] == 'applied'

        active = service.store.active_set()[PATTERN]
        assert active != base_version
        history = service.history(PATTERN)
        assert [v['version_number'] for v in history] == [1, 2]
        assert history[1]['validation_report_id'] == result['report_id']
        # The active source mirror now holds the reviewed candidate.
        mirror = (root / 'patterns/008_head_and_shoulders.py').read_bytes()
        assert mirror != original and b'VALUE2=VALUE' in mirror

        # Rollback to v1 opens a fresh reviewable draft; history is preserved.
        draft = service.rollback(session['session_id'], base_version, 'later peak regressed')
        assert draft['revision']['restored_from_version_id'] == base_version
        assert len(service.history(PATTERN)) == 2
        assert service.read(session['session_id'])['state'] == 'applied'
    finally:
        service.executor.shutdown()


def test_apply_is_idempotent_and_sandbox_failure_preserves_source(tmp_path):
    root = repository(tmp_path)
    service = _service(root)
    try:
        session = service.create(_chart())
        service.message(session['session_id'], 'change', [{'dataset_index': 2, 'role': 'RS', 'snap': 'high'}], _image())
        _wait(service, service.submit(session['session_id']))
        result = service.read(session['session_id'])
        first = service.apply(session['session_id'], result['current_revision_id'],
                              result['report_id'], result['revision']['candidate_sha256'], 'same-key')
        second = service.apply(session['session_id'], result['current_revision_id'],
                               result['report_id'], result['revision']['candidate_sha256'], 'same-key')
        assert first['activation']['activation_id'] == second['activation']['activation_id']
        assert len(service.history(PATTERN)) == 2
    finally:
        service.executor.shutdown()


def test_missing_sandbox_blocks_apply_without_touching_active_source(tmp_path):
    root = repository(tmp_path)
    store = EditStore(root)
    # A validator whose runner always fails closed, as on a host without Q002.
    class Down(FakeRunner):
        def run(self, files, request, cancel=None):
            raise SandboxUnavailable('no delegated cgroup')
    service = PatternEditService(store, DeepSeekEditor(_provider), Validator(store, Down()))
    try:
        session = service.create(_chart())
        service.message(session['session_id'], 'change', [{'dataset_index': 2, 'role': 'RS', 'snap': 'high'}], _image())
        _wait(service, service.submit(session['session_id']))
        result = service.read(session['session_id'])
        assert result['state'] == 'needs-revision'
        assert result['report']['ready'] is False
        assert any(c['outcome'] == 'unavailable' for c in result['report']['checks'])
        assert (root / 'patterns/008_head_and_shoulders.py').read_bytes().endswith(b'VALUE2=VALUE\n')
    finally:
        service.executor.shutdown()
