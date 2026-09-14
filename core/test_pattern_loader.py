from pathlib import Path
import shutil

from core.pattern_edit_store import EditStore, ROOT
from core.pattern_versions import PatternVersions, collect_sources
from core.pattern_loader import VersionPattern
from core.test_pattern_edit_contracts import candles
from data.ohlcv_store import OHLCVStore
from patterns.chart_scan import _snapshot
from patterns import _dedup


def test_frozen_baseline_does_not_import_current_helper(tmp_path):
    for name,data in collect_sources(ROOT,'patterns/008_head_and_shoulders.py').items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    version=PatternVersions(EditStore(tmp_path)).baseline('pattern_008_head_and_shoulders')
    pattern=VersionPattern(version['version_id'],EditStore(tmp_path))
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
