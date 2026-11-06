"""Dated universe inputs bind the actual symbols and reject future selection."""
from __future__ import annotations

import hashlib
import json
import pytest

from core.replay_universe import load_replay_universe
from main import _parse_args


def test_dated_snapshot_membership_is_loaded_and_hashed(tmp_path):
    value={'schema_version':1,'market':'us','as_of':'2025-10-31','source':'vendor snapshot',
           'members':[{'symbol':'AAPL','exchange':'NASDAQ','security_id':'stable-id-1'}]}
    path=tmp_path/'universe.json'; path.write_text(json.dumps(value))
    rows,evidence=load_replay_universe(str(path),'us','2025-11-03',2000)
    assert rows==[('AAPL','NASDAQ')]
    assert evidence['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert evidence['returned']==1 and not evidence['membership_independently_verified']
    assert _parse_args(['--paper','--paper-universe',str(path)]).paper_universe==str(path)


@pytest.mark.parametrize('change', ['future','market','duplicate','missing_id'])
def test_invalid_snapshot_cannot_silently_use_current_screener(tmp_path,change):
    member={'symbol':'AAPL','exchange':'NASDAQ','security_id':'id'}
    value={'schema_version':1,'market':'us','as_of':'2025-10-31','members':[member]}
    if change=='future': value['as_of']='2025-11-04'
    if change=='market': value['market']='ph'
    if change=='duplicate': value['members'].append(dict(member))
    if change=='missing_id': member['security_id']=''
    path=tmp_path/'universe.json'; path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_replay_universe(str(path),'us','2025-11-03',2000)
