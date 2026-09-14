"""Validation orchestration with a fake runner (sandbox execution needs P3 setup)."""
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pytest

from core.pattern_edit_store import EditStore, EditError, canonical, digest, uid
from core.pattern_edit_validation import Validator, candle_rows, validate_candles
from core.pattern_edit_worker import SandboxUnavailable
from core.pattern_versions import PatternVersions
from core.test_pattern_versions import repository
from patterns.base_pattern import TradeSignal

PATTERN = 'pattern_008_head_and_shoulders'


class FakeRunner:
    """Stands in for CandidateRunner; returns crafted worker output."""

    def __init__(self, signals=(), tests=True, malformed=False, unavailable=False):
        self.signals = list(signals)
        self.tests = tests
        self.malformed = malformed
        self.unavailable = unavailable

    def run(self, files, request, cancel=None):
        if self.unavailable:
            raise SandboxUnavailable('No delegated cgroup subtree configured')
        if request.get('operation') == 'tests':
            return {'tests_passed': self.tests, 'exit_code': 0 if self.tests else 1}
        if self.malformed:
            return {'signals': 'not-a-list'}
        return {'signals': self.signals, 'metadata': {}}


def dataset():
    frame = pd.DataFrame(
        {'open': [100.0, 101.0], 'high': [101.0, 102.0],
         'low': [99.0, 100.0], 'close': [100.0, 101.0], 'volume': [1000, 1000]},
        index=pd.to_datetime(['2024-01-02', '2024-01-03']),
    )
    return {'symbol': 'FIXTURE', 'market': 'us', 'timeframe': '1d',
            'session_timezone': 'America/New_York', 'candles': candle_rows(frame, 'America/New_York')}


def anchor():
    row = dataset()['candles'][1]
    return {'anchor_id': 'RS', 'role': 'RS', 'price': row['high'], 'snap': 'high',
            'candle': {'session_date': row['time'], 'dataset_index': 1}}


def candidate_signal():
    row = dataset()['candles'][1]
    return asdict(TradeSignal(
        symbol='FIXTURE', action='BUY', pattern=PATTERN, timeframe='1d', confidence=1,
        price=row['close'], qty=1,
        chart_annotations=[{'type': 'marker', 'date': row['time'], 'price': row['high'], 'label': 'RS'}],
    ))


def revision(store, version, source='from patterns._helper import VALUE\nVALUE2=VALUE\n# edit\n'):
    doc = str(Path(version['source_path']).with_suffix('.md'))
    files = {version['source_path']: store.blob(source.encode(), 'text/plain'),
             doc: store.blob(b'Later confirmed right shoulder rules', 'text/plain')}
    return {'revision_id': uid(), 'session_id': uid(), 'base_version_id': version['version_id'],
            'files': files, 'explanation': 'Later confirmed shoulder', 'unresolved_questions': [],
            'candidate_sha256': digest(canonical({'base': version['version_id'], 'files': files})),
            'patch': store.blob(b'diff', 'text/x-diff')}


def validators(tmp_path, runner):
    store = EditStore(repository(tmp_path))
    return store, PatternVersions(store).baseline(PATTERN), Validator(store, runner)


def test_ready_report_binds_hashes_and_passes_required_checks(tmp_path):
    store, version, validator = validators(tmp_path, FakeRunner([candidate_signal()]))
    rev = revision(store, version)
    report = validator.validate(version, rev, dataset(), [anchor()])
    assert report['ready'] is True
    assert report['candidate_sha256'] == rev['candidate_sha256']
    assert report['dataset_sha256'] == digest(canonical(dataset()))
    assert report['validator_sha256'] == digest(Path('core/pattern_edit_validation.py').resolve().read_bytes())
    outcomes = {c['name']: c for c in report['checks']}
    assert outcomes['anchor:RS']['outcome'] == 'passed'
    assert outcomes['determinism']['outcome'] == 'passed'
    assert outcomes['trusted-regressions']['outcome'] == 'passed'
    assert all(c['outcome'] == 'passed' for c in report['checks'] if c['required'])


def test_anchor_mismatch_and_failed_tests_block_apply(tmp_path):
    store, version, _ = validators(tmp_path, None)
    rev = revision(store, version)
    bad = candidate_signal()
    bad['chart_annotations'] = [{'type': 'marker', 'date': '1999-01-01', 'price': bad['price'], 'label': 'LS'}]
    report = Validator(store, FakeRunner([bad])).validate(version, rev, dataset(), [anchor()])
    assert report['ready'] is False
    assert any(c['name'] == 'anchor:RS' and c['outcome'] == 'failed' for c in report['checks'])
    failed = Validator(store, FakeRunner([candidate_signal()], tests=False)).validate(version, rev, dataset(), [anchor()])
    assert failed['ready'] is False
    assert any(c['name'] == 'trusted-regressions' and c['outcome'] == 'failed' for c in failed['checks'])


def test_unavailable_sandbox_and_malformed_output_fail_closed(tmp_path):
    store, version, _ = validators(tmp_path, None)
    rev = revision(store, version)
    unavailable = Validator(store, FakeRunner([candidate_signal()], unavailable=True)).validate(
        version, rev, dataset(), [anchor()])
    assert unavailable['ready'] is False
    assert any(c['outcome'] == 'unavailable' for c in unavailable['checks'])
    malformed = Validator(store, FakeRunner(malformed=True)).validate(version, rev, dataset(), [anchor()])
    assert malformed['ready'] is False
    assert any(c['name'] == 'execution' and c['outcome'] == 'failed' for c in malformed['checks'])


def test_execute_rejects_unknown_fields_and_wrong_pattern(tmp_path):
    store, version, _ = validators(tmp_path, None)
    extra = dict(candidate_signal())
    extra['evil'] = 1
    with pytest.raises(EditError, match='incompatible'):
        Validator(store, FakeRunner([extra])).execute(version, {}, dataset())
    wrong = dict(candidate_signal())
    wrong['pattern'] = 'other'
    with pytest.raises(EditError, match='incompatible'):
        Validator(store, FakeRunner([wrong])).execute(version, {}, dataset())


def test_candle_validation_rejects_unsorted_and_non_finite():
    rows = dataset()['candles']
    validate_candles(rows)
    with pytest.raises(EditError):
        validate_candles([rows[1], rows[0]])
    broken = [dict(rows[0], high=float('nan')), rows[1]]
    with pytest.raises(EditError):
        validate_candles(broken)


def test_candle_validation_tolerates_real_vendor_ohlc_quirks():
    # Split rounding / stale sub-penny bars violate intra-bar ordering but are
    # valid frozen inputs; the editor must not reject the whole chart.
    rows = dataset()['candles']
    open_above_high = [dict(rows[0], open=rows[0]['high'] + 0.5), rows[1]]
    validate_candles(open_above_high)
    close_below_low = [dict(rows[0], close=rows[0]['low'] - 0.5), rows[1]]
    validate_candles(close_below_low)
    for bad in ([dict(rows[0], high=rows[0]['low'] - 0.1)],
                [dict(rows[0], low=0.0)],
                [dict(rows[0], volume=-1)]):
        with pytest.raises(EditError):
            validate_candles(bad + [rows[1]])

