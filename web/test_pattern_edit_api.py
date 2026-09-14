"""Authenticated HTTP adapter checks for the shared pattern editor service."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from core.pattern_edit_store import Conflict, EditError


class FakeService:
    def __init__(self):
        self.calls = []
        self.heartbeat = None

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def create(self, chart, actor):
        self._record('create', chart, actor)
        return {'session_id': 's1', 'pattern_id': 'pattern_008_head_and_shoulders', 'state': 'draft'}

    def read(self, session_id):
        self._record('read', session_id)
        return {'session_id': session_id, 'pattern_id': 'pattern_008_head_and_shoulders', 'state': 'ready'}

    def sessions(self, trade_id):
        self._record('sessions', trade_id)
        return []

    def message(self, session_id, text, selections, image):
        self._record('message', session_id, text, selections, image)
        return {'session_id': session_id, 'state': 'draft'}

    def submit(self, session_id, operation='preview', idempotency_key=None, datasets=None):
        self._record('submit', session_id, operation, idempotency_key, datasets)
        return {'job_id': 'j1', 'state': 'queued'}

    def cancel(self, session_id, discard=False):
        self._record('cancel', session_id, discard)
        return {'session_id': session_id, 'state': 'discarded' if discard else 'cancelled'}

    def preview_chart(self, session_id, kind):
        self._record('preview_chart', session_id, kind)
        return {'title': kind}

    def history(self, pattern_id):
        self._record('history', pattern_id)
        return [{'version_number': 1, 'version_id': 'v1', 'description': 'baseline'}]

    def rollback(self, session_id, version_id, reason):
        self._record('rollback', session_id, version_id, reason)
        return {'session_id': 's2', 'state': 'draft'}

    def apply(self, session_id, revision_id, report_id, candidate_sha256, idempotency_key):
        self._record('apply', session_id, revision_id, report_id, candidate_sha256, idempotency_key)
        return {'session_id': session_id, 'state': 'applied'}

    def activation_status(self, activation_id):
        self._record('activation_status', activation_id)
        return {'activation': {'state': 'active'}}


class PatternEditApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeService()
        self.patches = [
            patch('web.auth.settings'),
            patch('web.app.settings'),
            patch('web.pattern_edit_api.get_pattern_edit_service', return_value=self.service),
            patch('web.pattern_edit_api.paper_books.chart', return_value={'title': 'chart'}),
        ]
        self.auth_s = self.patches[0].start()
        self.app_s = self.patches[1].start()
        self.get_service = self.patches[2].start()
        self.chart = self.patches[3].start()
        for s in (self.auth_s, self.app_s):
            s.web_ui_password = 'correct-horse'
            s.web_ui_username = 'admin'
            s.web_ui_secret_key = 'test-secret-key'
            s.web_ui_https = False
            s.web_ui_session_hours = 12
        from web.app import create_app
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def _login(self) -> None:
        ok = self.client.post('/login', data={'username': 'admin', 'password': 'correct-horse', 'next': '/'},
                              follow_redirects=False)
        self.assertEqual(ok.status_code, 303)
        self.client.cookies.update(ok.cookies)

    def test_requires_authentication(self) -> None:
        self.assertEqual(self.client.get('/api/pattern-edits/s1').status_code, 401)
        self.assertEqual(self.client.post('/api/pattern-edits', json={'market': 'us', 'trade_id': 't1'}).status_code, 401)

    def test_create_validates_identity_and_passes_chart(self) -> None:
        self._login()
        bad = self.client.post('/api/pattern-edits', json={'market': 'eu', 'trade_id': 't1'})
        self.assertEqual(bad.status_code, 422)
        ok = self.client.post('/api/pattern-edits', json={'market': 'us', 'trade_id': 't1'})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()['session_id'], 's1')
        self.chart.assert_called_once_with('us', side='open', trade_id='t1')

    def test_cross_origin_mutations_rejected(self) -> None:
        self._login()
        origin = self.client.post('/api/pattern-edits/s1/messages', json={'text': 'x'},
                                  headers={'origin': 'http://evil.example'})
        self.assertEqual(origin.status_code, 403)
        site = self.client.post('/api/pattern-edits/s1/messages', json={'text': 'x'},
                                headers={'sec-fetch-site': 'cross-site'})
        self.assertEqual(site.status_code, 403)

    def test_messages_preview_and_discard(self) -> None:
        self._login()
        msg = self.client.post('/api/pattern-edits/s1/messages',
                               json={'text': 'Prefer this shoulder', 'selections': [{'dataset_index': 0, 'role': 'RS'}],
                                     'image': 'data:image/png;base64,AAAA'})
        self.assertEqual(msg.status_code, 200)
        preview = self.client.post('/api/pattern-edits/s1/preview', json={'idempotency_key': 'k1'})
        self.assertEqual(preview.status_code, 200)
        discard = self.client.request('DELETE', '/api/pattern-edits/s1', json={})
        self.assertEqual(discard.status_code, 200)
        names = [c[0] for c in self.service.calls]
        self.assertEqual(names, ['message', 'submit', 'cancel'])
        self.assertTrue(self.service.calls[-1][1][1])

    def test_service_errors_map_to_status_codes(self) -> None:
        self._login()
        self.service.read = lambda session_id: (_ for _ in ()).throw(EditError('bad selection'))
        self.assertEqual(self.client.get('/api/pattern-edits/s1').status_code, 422)
        self.service.read = lambda session_id: (_ for _ in ()).throw(Conflict('stale draft'))
        self.assertEqual(self.client.get('/api/pattern-edits/s1').status_code, 409)

    def test_history_rollback_and_apply_contract(self) -> None:
        self._login()
        history = self.client.get('/api/patterns/pattern_008_head_and_shoulders/versions')
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()[0]['version_id'], 'v1')
        rollback = self.client.post('/api/patterns/pattern_008_head_and_shoulders/rollback',
                                    json={'session_id': 's1', 'version_id': 'v1', 'reason': 'regression'})
        self.assertEqual(rollback.status_code, 200)
        self.assertEqual(self.service.calls[-1][0], 'rollback')
        applied = self.client.post('/api/pattern-edits/s1/apply',
                                   json={'revision_id': 'r1', 'report_id': 'p1', 'candidate_sha256': 'h',
                                         'idempotency_key': 'k1'})
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.json()['state'], 'applied')


if __name__ == '__main__':
    unittest.main()
