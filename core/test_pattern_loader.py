"""Version-pinned execution: an approved pattern snapshot runs its own source.

The candle fixture is a head-and-shoulders series that fires `pattern_008`, so
the test can prove the frozen baseline -- not the live helper modules -- is what
produced the signal.
"""

from pathlib import Path

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core.pattern_edit_store import ROOT
from core.pattern_versions import PatternVersions, collect_sources
from core.pattern_loader import VersionPattern
from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle
from patterns.chart_scan import _snapshot
from patterns import _dedup

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/pattern_versions"


def candles(name: str) -> list[OHLCVCandle]:
    frame = pd.read_csv(FIXTURES / f"{name}.csv", index_col="date", parse_dates=True)
    return [
        OHLCVCandle(**row.to_dict(), timestamp=ts.to_pydatetime())
        for ts, row in frame.iterrows()
    ]


def test_chart_view_survives_config_drift(tmp_path, editor_store_factory, monkeypatch):
    """A settings change must not blank a saved detection's chart.

    Settings remain live for execution and for saved chart geometry.
    """
    from core.pattern_bootstrap import Bootstrap, freeze
    from core.pattern_provenance import pattern_annotations

    snapshot = freeze(ROOT, disabled=[])
    root = tmp_path
    for name, data in snapshot.files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    store = editor_store_factory(root)
    Bootstrap(store).stage(snapshot)
    version = next(v for v in store.list('versions') if v['pattern_id'] == 'pattern_003_double_bottom')
    config = root / 'config.py'
    config.write_bytes(config.read_bytes() + b'\n# chart-view drift\n')
    monkeypatch.setattr('core.pattern_loader.open_pattern_store', lambda **kwargs: store)
    assert VersionPattern(version['version_id'], store).name == version['pattern_id']
    rendered = pattern_annotations(
        [{'type': 'marker', 'date': '2020-01-02', 'price': 1.0, 'label': 'L1',
          'pattern_version_id': version['version_id']}],
        None, 'pattern_003_double_bottom')
    assert rendered[0]['label'] == 'First bottom (L1)'


def test_frozen_baseline_does_not_import_current_helper(tmp_path, editor_store_factory):
    for name,data in collect_sources(ROOT,'patterns/008_head_and_shoulders.py').items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    version=PatternVersions(editor_store_factory(tmp_path)).baseline('pattern_008_head_and_shoulders')
    pattern=VersionPattern(version['version_id'],editor_store_factory(tmp_path))
    # A later helper edit must not change this already loaded version.
    (tmp_path/'patterns/_rules.py').write_text('raise RuntimeError("wrong helper")')
    store=OHLCVStore(window=512,session_tz='America/New_York')
    bars=candles('hs_control');store.replace_all('FIXTURE','1d',bars)
    _dedup.reset()
    try:
        signal=pattern.analyze(_snapshot('FIXTURE','1d',bars[-1]),store)
        assert signal.pattern_version_id==version['version_id']
        assert signal.provenance=='versioned'
        assert signal.setup_key==(150,210)
        assert signal.requested_rules['stop_loss']==94.5
    finally:
        _dedup.reset()


@pytest.mark.parametrize("provenance", ["file-import", "trusted-snapshot"])
def test_execution_allows_live_config(tmp_path, provenance):
    from types import SimpleNamespace
    from core.pattern_edit_store import EditError, digest
    from core.pattern_versions import runtime_manifest

    source = b"""from patterns.base_pattern import BasePattern
from config import settings
class Detector(BasePattern):
    name = 'pattern_001_test'
    timeframes = ['1d']
    skipped = False
    def analyze(self, snapshot, store):
        return None
"""
    files = {'patterns/001_test.py': source, 'config.py': b'# old settings\n',
             'patterns/base_pattern.py': (ROOT / 'patterns/base_pattern.py').read_bytes()}
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    refs = {name: {'sha256': digest(data)} for name, data in files.items()}
    blobs = {digest(data): data for data in files.values()}
    version = dict(version_id='test', pattern_id='pattern_001_test',
                   parent_version_id=None, provenance=provenance,
                   source_path='patterns/001_test.py', files=refs,
                   runtime=runtime_manifest(tmp_path))
    store = SimpleNamespace(root=tmp_path, get=lambda *args: version,
                            read_blob=lambda ref: blobs[ref['sha256']])
    (tmp_path / 'config.py').write_text('# changed settings\n')
    assert VersionPattern('test', store).name == 'pattern_001_test'
    if provenance == 'file-import':
        (tmp_path / 'patterns/base_pattern.py').write_text('# incompatible interface\n')
        with pytest.raises(EditError, match='base_pattern.py'):
            VersionPattern('test', store)


GENERATED_SOURCE = b"""from patterns.base_pattern import BasePattern, TradeSignal
class Detector(BasePattern):
    name = 'pattern_001_test'
    timeframes = ['1d']
    skipped = False
    def analyze(self, snapshot, store):
        return TradeSignal(symbol=snapshot.symbol, action='BUY', pattern=self.name,
                           timeframe=snapshot.timeframe, confidence=1.0,
                           price=snapshot.candle.close, qty=1)
"""


def generated_version(tmp_path):
    """An applied edit plus a store double that serves its version and blobs."""
    from types import SimpleNamespace
    from core.pattern_edit_store import digest
    from core.pattern_versions import runtime_manifest

    files = {'patterns/001_test.py': GENERATED_SOURCE,
             'patterns/base_pattern.py': (ROOT / 'patterns/base_pattern.py').read_bytes()}
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    blobs = {digest(data): data for data in files.values()}
    version = dict(
        version_id='generated-1', pattern_id='pattern_001_test',
        parent_version_id='base-1', provenance='generated',
        source_path='patterns/001_test.py',
        files={name: {'sha256': digest(data)} for name, data in files.items()},
        runtime=runtime_manifest(tmp_path),
        metadata={'name': 'pattern_001_test', 'timeframes': ['1d'], 'skipped': False,
                  'chart_description': 'x', 'MIN_BARS': 2, 'HORIZON_BARS': 5,
                  'MAX_OPEN_PER_SYMBOL': None})
    store = SimpleNamespace(root=tmp_path, get=lambda *args: version,
                            read_blob=lambda ref: blobs[ref['sha256']])
    return store, version


def generated_bars(count=8):
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return [OHLCVCandle(open=10.0, high=11.0, low=9.0, close=10.0, volume=100.0,
                        timestamp=start + timedelta(days=i))
            for i in range(count)]


def test_generated_version_preloads_in_process(tmp_path, monkeypatch):
    """An approved edit runs its own source here, not a sandbox process per symbol."""
    store, _ = generated_version(tmp_path)

    def _never(*args, **kwargs):
        raise AssertionError('a preloaded version must not enter the sandbox')

    monkeypatch.setattr('core.pattern_edit_worker.CandidateRunner.run', _never)
    pattern = VersionPattern('generated-1', store, in_process=True)
    assert pattern._baseline is not None
    bars = generated_bars()
    ohlcv = OHLCVStore(window=64, session_tz='America/New_York')
    ohlcv.replace_all('FIXTURE', '1d', bars)
    _dedup.reset()
    try:
        signal = pattern.analyze(_snapshot('FIXTURE', '1d', bars[-1]), ohlcv)
    finally:
        _dedup.reset()
    assert signal.symbol == 'FIXTURE'
    assert signal.pattern_version_id == 'generated-1'
    assert signal.provenance == 'versioned'


def test_sandboxed_version_batches_every_symbol_into_one_worker(tmp_path, monkeypatch):
    store, _ = generated_version(tmp_path)
    batches: list[int] = []

    class _Validator:
        def __init__(self, _store):
            pass

        def execute_many(self, version, files, datasets, cancel=None):
            batches.append(len(datasets))
            return [{'signals': [], 'metadata': {}} for _ in datasets]

        def execute(self, *args, **kwargs):
            raise AssertionError('a batch must not fall back to per-symbol calls')

    monkeypatch.setattr('core.pattern_edit_validation.Validator', _Validator)
    pattern = VersionPattern('generated-1', store)
    assert pattern._baseline is None
    bars = generated_bars()
    symbols = ['AAA', 'BBB', 'CCC']
    ohlcv = OHLCVStore(window=64, session_tz='America/New_York')
    for symbol in symbols:
        ohlcv.replace_all(symbol, '1d', bars)
    _dedup.reset()
    signals = pattern.analyze_many(
        [_snapshot(symbol, '1d', bars[-1]) for symbol in symbols], ohlcv)
    assert signals == [None, None, None]
    assert batches == [3]
