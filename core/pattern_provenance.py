"""Additive provenance and entry-rule snapshots shared by all trade adapters."""
from copy import deepcopy
from pathlib import Path
import hashlib

RULE_FIELDS = ('stop_loss','stop_loss_on_close','take_profit','trailing_stop_pct','trailing_stop_mode',
               'trailing_stop_on_close','stop_loss_pct_cap','reclaim_exit','reclaim_lower_rail',
               'exit_fill_at_close','trailing_ref_after_check','exit_order','neckline',
               'neckline_break_direction','exit_bars_after_neckline_break','exit_bars_after_entry',
               'trailing_activation_pct','time_exit_only_unfavorable','time_exit_min_mfe_pct')


def engine_hash():
    return hashlib.sha256(Path(__file__).with_name('backtester.py').read_bytes()).hexdigest()


def rules(value, stage='signal', **extra):
    return {'schema_version':1,'stage':stage,'engine_sha256':engine_hash(),
            **{key:deepcopy(getattr(value,key,None)) for key in RULE_FIELDS},
            'action':value.action,'qty':value.qty,'price':getattr(value,'price',getattr(value,'entry_price',None)),**extra}


def payload(value):
    return {key:deepcopy(getattr(value,key,None)) for key in
            ('trade_id','signal_id','pattern_version_id','provenance','requested_rules','resolved_rules')}


def row_payload(value):
    return {key:deepcopy(value.get(key)) for key in
            ('trade_id','signal_id','pattern_version_id','provenance','requested_rules','resolved_rules')}


def pattern_annotations(annotations, df=None, pattern=None):
    """Never infer historical rules from today's mutable detector files."""
    ids = {a.get('pattern_version_id') for a in annotations}
    if ids == {None} or not ids:
        # Legacy anchors remain readable, without inventing detector provenance.
        from patterns._annotations import pattern_annotations as legacy, _historical_geometry
        saved = deepcopy(annotations)
        if df is not None and pattern:
            markers = {a.get('label'): a for a in saved if a.get('type') == 'marker'}
            saved.extend(_historical_geometry(df, pattern, saved, markers))
        return legacy(saved, None, pattern)
    if len(ids) != 1 or None in ids:
        return deepcopy(annotations)
    from core.pattern_loader import VersionPattern
    # Presentation only. Execution still refuses a drifted config.py; a chart
    # of an already saved detection must not.
    try:
        pinned = VersionPattern(next(iter(ids)), runtime=False)
    except Exception:
        from utils.logger import log
        log.exception("Pinned chart geometry unavailable; showing saved annotations")
        return deepcopy(annotations)
    if pinned._baseline is None:
        # Only connect saved anchors with trusted presentation code. Never run
        # generated helpers or infer a new setup from the price history here.
        from patterns._annotations import pattern_annotations as saved_geometry
        return saved_geometry(annotations)
    module = pinned._baseline_modules['patterns.' + pinned.version['source_path'].split('/')[-1][:-3]]
    helper = module.__builtins__['__import__']('patterns._annotations', fromlist=['pattern_annotations'])
    return helper.pattern_annotations(annotations, df, pattern or pinned.name)
