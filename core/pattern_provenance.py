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
