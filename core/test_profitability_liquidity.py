"""Liquidity uses past volume, not the execution candle's future turnover."""
from __future__ import annotations

from dataclasses import replace
import pandas as pd
import pytest

from core.paper_liquidity import execution_liquidity
from core.test_short_execution_pipeline import bar,candidate
from data.ohlcv_store import OHLCVStore
from data.tv_client import TVClient
from main import _parse_args


def test_future_volume_cannot_approve_an_oversized_order():
    store=OHLCVStore(); s=candidate(False); s.qty=100
    bars=[replace(bar(100,i),volume=1000) for i in range(-19,1)]
    bars.append(replace(bar(100,1),volume=1e9))
    store.replace_all('TEST','1d',bars)
    with pytest.raises(ValueError,match='exceeds'):
        execution_liquidity(s,store,.01,True)
    s.qty=10
    assert execution_liquidity(s,store,.01,True)['participation']==.01


def test_missing_history_requires_explicit_certification_policy():
    s=candidate(False)
    assert not execution_liquidity(s,OHLCVStore(),.01,False)['verified']
    with pytest.raises(ValueError,match='20 valid'):
        execution_liquidity(s,OHLCVStore(),.01,True)
    assert _parse_args(['--paper','--kronos-exit-mode','off']).kronos_exit_mode=='off'


def test_screener_does_not_relax_an_empty_dollar_floor(monkeypatch):
    monkeypatch.setattr('data.tv_client._get_scanner_data',lambda q:(1,pd.DataFrame([
        {'name':'THIN','exchange':'NASDAQ','value':100,'volume':1}])))
    assert TVClient.fetch_top_symbols_with_exchanges(2000,'america',order_by='value',min_value=20e6)==[]
