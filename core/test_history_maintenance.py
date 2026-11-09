"""Daily repair fills internal holes and never inserts unclosed or fabricated bars."""
from __future__ import annotations
from datetime import date,datetime
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock
import pytest
from core.history_maintenance import merge_provider_rows,valid_ohlcv,closed_sessions


def ts(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('America/New_York')).timestamp())


def test_interior_hole_is_inserted_even_when_newer_rows_exist(monkeypatch):
    conn=MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value=[
        (ts('2026-11-02'),date(2026,11,2),100.,101.,99.,100.,1000),
        (ts('2026-11-04'),date(2026,11,4),100.,101.,99.,100.,1000)]
    writes=[]
    monkeypatch.setattr('core.history_maintenance.db.upsert_bars',lambda c,s,rows,**kw:writes.extend(rows))
    monkeypatch.setattr('core.history_maintenance.db.refresh_symbol_meta',lambda *_:None)
    rows=[(ts(d),100.,101.,99.,100.,1000) for d in ('2026-11-02','2026-11-03','2026-11-04','2026-11-05')]
    report=merge_provider_rows(conn,'AAA','us',rows,date(2026,11,2),date(2026,11,4))
    assert [r[0] for r in writes]==[ts('2026-11-03')]
    assert report['inserted_sessions']==1 and report['provider_sessions']==3


def test_repairs_bad_values_without_creating_duplicate_session(monkeypatch):
    conn=MagicMock()
    original=ts('2026-11-04')
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value=[(original,date(2026,11,4),100.,99.,101.,100.,1000)]
    writes=[]
    monkeypatch.setattr('core.history_maintenance.db.upsert_bars',lambda c,s,rows,**kw:writes.extend(rows))
    monkeypatch.setattr('core.history_maintenance.db.refresh_symbol_meta',lambda *_:None)
    result=merge_provider_rows(conn,'AAA','us',[(original+9*3600,100.,101.,99.,100.,1000)],date(2026,11,4),date(2026,11,4))
    assert writes==[(original,100.,101.,99.,100.,1000)]
    assert result['corrected_sessions']==1 and result['inserted_sessions']==0


@pytest.mark.parametrize('values',[(0,1,0,1,10),(1,2,0,1,10),(1,2,1,1,-1),(1,1,1,float('nan'),10),(1,1,1,2,10)])
def test_bad_source_bars_are_not_stored(values):
    assert not valid_ohlcv(values)


def test_holiday_is_not_treated_as_missing_us_session():
    ends=closed_sessions(datetime(2026,7,4,7,tzinfo=ZoneInfo('Asia/Manila')))
    assert ends['us']==date(2026,7,2)


def test_secondary_pagination_never_returns_partial_failed_history():
    from core.history_maintenance import fetch_secondary_us
    client=MagicMock()
    client._request.side_effect=[{'bars':{'AAA':[]},'next_page_token':'next'},None]
    with pytest.raises(RuntimeError,match='partial pages'):
        fetch_secondary_us(['AAA'],date(2026,11,2),date(2026,11,6),client)
    assert client._request.call_args.args[1]['timeframe']=='1Day'
    assert client._request.call_args.args[1]['feed']=='sip'
    assert client._request.call_args.args[1]['adjustment']=='raw'


def test_secondary_requires_nearby_matching_raw_price_basis(monkeypatch):
    from core.history_maintenance import merge_secondary_rows
    conn=MagicMock()
    known=['2026-11-02','2026-11-03','2026-11-05']
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value=[
        (ts(d),date.fromisoformat(d),100.,101.,99.,100.,1000) for d in known]
    calls=[]
    monkeypatch.setattr('core.history_maintenance.merge_provider_rows',
        lambda c,s,m,rows,start,end: calls.extend(rows) or {})
    rows=[(ts(d),100.,101.,99.,100.,1000) for d in known+['2026-11-04']]
    result=merge_secondary_rows(conn,'AAA',rows,date(2026,11,2),date(2026,11,6))
    assert calls==[rows[-1]] and not result['incompatible_or_unverified_dates']
    calls.clear()
    # An adjusted/raw basis mismatch must not splice a different price scale.
    rows=[(r[0],*(v*.1 for v in r[1:5]),r[5]) for r in rows]
    result=merge_secondary_rows(conn,'AAA',rows,date(2026,11,2),date(2026,11,6))
    assert not calls and result['incompatible_or_unverified_dates']==['2026-11-04']


def test_deduplication_preserves_conflicting_or_malformed_source_records():
    from core.history_maintenance import redundant_session_timestamps
    def rows(day,values):
        return [(ts(day)+i*3600,date.fromisoformat(day),*v) for i,v in enumerate(values)]
    valid=(100.,101.,99.,100.,1000)
    same=rows('2026-11-02',[valid,valid,valid])
    different=rows('2026-11-03',[valid,(100.,102.,99.,100.,1000)])
    bad=rows('2026-11-04',[(100.,99.,101.,100.,1000)]*2)
    assert redundant_session_timestamps(same+different+bad)==[same[0][0],same[1][0]]


def test_resume_retains_verified_symbols_and_retries_only_unfinished(tmp_path,monkeypatch):
    import json
    from core import history_maintenance as maintenance
    ends={'us':date(2026,11,6),'ph':date(2026,11,6)}
    audit={'symbols':{s:dict(symbol=s,market='us',potential_gap_dates=[])
                      for s in ('AAA','BBB','CCC')},'summary':{}}
    path=tmp_path/'repair.json'
    path.write_text(json.dumps(dict(start='2021-10-31',ends={k:str(v) for k,v in ends.items()},
        scope='all_symbols',total=3,completed=2,results={'AAA':{'status':'provider_verified'},
        'BBB':{'status':'source_unverified'}},started_at='earlier')))
    monkeypatch.setattr(maintenance.db,'get_conn',lambda:MagicMock())
    monkeypatch.setattr(maintenance,'merge_provider_rows',lambda *_:{'status':'provider_verified'})
    monkeypatch.setattr(maintenance,'audit_database',lambda *_:{'summary':{}})
    requested=[]
    def fetch(symbol,*_):
        requested.append(symbol)
        return [],{'source_invalid_dates':[],'source_latest_date':None}
    result=maintenance.repair_database(date(2021,10,31),ends,audit=audit,report_path=path,
        fetcher=fetch,resume=True,workers=1)
    assert requested==['BBB','CCC'] and result['completed']==3
    assert result['results']['AAA']=={'status':'provider_verified'}


def test_exchange_correction_refuses_to_overwrite_an_unreviewed_change(monkeypatch):
    from core import history_maintenance as maintenance
    conn=MagicMock()
    conn.__enter__.return_value=conn
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value=[(1,100.,102.,99.,101.,1000)]
    monkeypatch.setattr(maintenance.db,'get_conn',lambda:conn)
    correction=dict(symbol='AAA',market='us',date='2026-11-02',source_url='https://exchange.example/report',
                    expected=dict(open=100.,high=101.,low=99.,close=100.,volume=-1),
                    bar=dict(open=100.,high=101.,low=99.,close=100.,volume=1000))
    with pytest.raises(ValueError,match='stored bar changed'):
        maintenance.apply_authoritative_corrections([correction],{'us':date(2026,11,6)})


def test_secondary_share_class_spellings_are_asset_verified():
    from core.history_maintenance import secondary_source_symbol
    assets={'ABR.PRF':'NYSE','ALUB.WS':'NYSE','AIIA.RT':'NYSE','BRK.B':'NYSE'}
    assert secondary_source_symbol('ABR-PF',assets)=='ABR.PRF'
    assert secondary_source_symbol('ALUB-WT',assets)=='ALUB.WS'
    assert secondary_source_symbol('AIIA-RI',assets)=='AIIA.RT'
    assert secondary_source_symbol('BRK-B',assets)=='BRK.B'
    assert secondary_source_symbol('UNKNOWN-PA',assets) is None
    assert secondary_source_symbol('OLD',assets)=='OLD'


def test_secondary_repairs_bad_range_when_exact_same_day_close_confirms_scale(monkeypatch):
    from core.history_maintenance import merge_secondary_rows
    conn=MagicMock()
    day=date(2026,11,4)
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value=[(ts(str(day)),day,100.,101.,0.,100.,1000)]
    accepted=[]
    monkeypatch.setattr('core.history_maintenance.merge_provider_rows',lambda c,s,m,rows,*_:accepted.extend(rows) or {})
    source=(ts(str(day)),100.,101.,99.,100.,1000)
    result=merge_secondary_rows(conn,'AAA',[source],day,day)
    assert accepted==[source] and not result['incompatible_or_unverified_dates']


def test_quarantine_preserves_valid_holiday_otc_prints_and_flags_only_bad_non_sessions():
    from core.history_maintenance import closed_session_placeholders
    holiday=date(2026,9,7)
    invalid=('AAA','us',1,holiday,0.,0.,0.,100.,0)
    valid=('OTC','us',2,holiday,1.,1.,1.,1.,100)
    trading_day=('BBB','us',3,date(2026,9,8),0.,0.,0.,100.,0)
    assert closed_session_placeholders([invalid,valid,trading_day])==[invalid]
