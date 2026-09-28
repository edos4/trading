"""Version-pinned execution: an approved pattern snapshot runs its own source.

The candle fixture is a head-and-shoulders series that fires `pattern_008`, so
the test can prove the frozen baseline -- not the live helper modules -- is what
produced the signal.
"""

from pathlib import Path

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
