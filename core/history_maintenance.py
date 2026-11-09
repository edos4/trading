"""Audit and repair stored daily bars against complete provider histories.

Missing calendar sessions are candidates, not invented candles: a provider may
legitimately report no trades. Every repair excludes unclosed sessions and
compares all returned dates, including gaps preceding the newest database bar.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import json
import math
import threading
import time
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import numpy as np

from core.kronos_weekly import exclusive_lock, write_json
from data import db
from utils.logger import log

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / 'logs' / 'daily-bars-repair.json'
_YAHOO_SLOT_LOCK = threading.Lock()
_YAHOO_NEXT_SLOT = 0.0


def _yahoo_request_slot() -> None:
    global _YAHOO_NEXT_SLOT
    with _YAHOO_SLOT_LOCK:
        slot=max(time.monotonic(),_YAHOO_NEXT_SLOT)
        _YAHOO_NEXT_SLOT=slot+.25
    delay=slot-time.monotonic()
    if delay>0: time.sleep(delay)


def closed_sessions(now: datetime | None = None) -> dict[str, date]:
    from core.market import last_closed_session_date, is_us_holiday, is_ph_holiday
    result = {}
    for market in ('us','ph'):
        day = last_closed_session_date(market, now)
        holiday = is_us_holiday if market == 'us' else is_ph_holiday
        while day.weekday() >= 5 or holiday(day):
            day -= timedelta(days=1)
        result[market] = day
    return result


def valid_ohlcv(values: Iterable[float]) -> bool:
    o,h,l,c,v = [float(value) for value in values]
    return (all(math.isfinite(value) for value in (o,h,l,c,v)) and min(o,h,l,c)>0
            and v>=0 and h>=max(o,l,c) and l<=min(o,h,c))


def audit_database(start: date, ends: dict[str,date]) -> dict:
    """One read-only ordered scan; list potential holes without asserting no-trade days are missing."""
    from learn.kronos_weekly_dataset import is_session
    calendars = {m: np.array([d.toordinal() for d in (start+timedelta(days=i) for i in range((end-start).days+1))
                             if is_session(d,m)], dtype=np.int32) for m,end in ends.items()}
    with db.get_conn() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        with conn.cursor() as cur:
            cur.execute("SELECT symbol,market FROM symbols WHERE market IN ('us','ph') ORDER BY symbol")
            symbols = {s:{'symbol':s,'market':m,'bars':0,'first_date':None,'last_date':None,
                          'invalid_rows':0,'duplicate_sessions':0,'potential_gap_count':0,
                          'potential_gap_dates':[],'invalid_dates':[],'stale':True} for s,m in cur.fetchall()}
        with conn.cursor(name='daily_bar_completeness') as cur:
            cur.itersize=50000
            cur.execute("SELECT b.symbol,b.bar_date,b.open,b.high,b.low,b.close,b.volume "
                        "FROM daily_bars b JOIN symbols s ON s.symbol=b.symbol "
                        "WHERE b.bar_date >= %s AND ((s.market='us' AND b.bar_date<=%s) OR "
                        "(s.market='ph' AND b.bar_date<=%s)) ORDER BY b.symbol,b.ts",(start,ends['us'],ends['ph']))
            current=None
            rows=[]
            def flush():
                if current is None or not rows: return
                record=symbols[current]
                days=np.array([row[0].toordinal() for row in rows],dtype=np.int32)
                values=np.array([row[1:] for row in rows],dtype=np.float64)
                p=values[:,:4]
                valid=(np.isfinite(values).all(axis=1)&(p>0).all(axis=1)&(values[:,4]>=0)
                       &(p[:,1]>=p.max(axis=1))&(p[:,2]<=p.min(axis=1)))
                unique=np.unique(days)
                expected=calendars[record['market']]
                expected=expected[(expected>=unique[0])&(expected<=unique[-1])]
                gaps=np.setdiff1d(expected,unique,assume_unique=True)
                record.update(bars=len(rows),first_date=date.fromordinal(int(unique[0])).isoformat(),
                              last_date=date.fromordinal(int(unique[-1])).isoformat(),
                              invalid_rows=int((~valid).sum()),duplicate_sessions=len(days)-len(unique),
                              potential_gap_count=len(gaps),potential_gap_dates=[date.fromordinal(int(d)).isoformat() for d in gaps],
                              invalid_dates=sorted({date.fromordinal(int(d)).isoformat() for d in days[~valid]}),
                              stale=int(unique[-1])<ends[record['market']].toordinal())
            for symbol,day,*values in cur:
                if symbol!=current:
                    flush();current=symbol;rows=[]
                rows.append((day,*values))
            flush()
    return {'start':start.isoformat(),'ends':{m:d.isoformat() for m,d in ends.items()},
            'symbols':symbols,'summary':summarize_audit(symbols)}


def summarize_audit(symbols: dict) -> dict:
    return {m:{'symbols':sum(r['market']==m for r in symbols.values()),
               'bars':sum(r['bars'] for r in symbols.values() if r['market']==m),
               'stale_symbols':sum(r['stale'] for r in symbols.values() if r['market']==m),
               'invalid_rows':sum(r['invalid_rows'] for r in symbols.values() if r['market']==m),
               'duplicate_sessions':sum(r['duplicate_sessions'] for r in symbols.values() if r['market']==m),
               'potential_gap_sessions':sum(r['potential_gap_count'] for r in symbols.values() if r['market']==m)} for m in ('us','ph')}


def fetch_provider(symbol: str, market: str, start: date, end: date) -> tuple[list[tuple],dict]:
    """Bypass caches and preserve source failures, rather than counting them as no trades."""
    from data.tv_client import _yahoo_chart_payload, _candles_from_yahoo_payload
    if market=='us':
        from core.market import yahoo_chart_symbol
        provider_symbol=yahoo_chart_symbol(symbol,screener='america')
        tz=ZoneInfo('America/New_York')
        lower=int(datetime.combine(start,datetime.min.time(),tz).timestamp())
        upper=int(datetime.combine(end+timedelta(days=1),datetime.min.time(),tz).timestamp())
        _yahoo_request_slot()
        payload=_yahoo_chart_payload(provider_symbol,'1d','max',timeout=30,start_ts=lower,end_ts=upper)
        if payload is None:
            raise RuntimeError('Yahoo request failed; history completeness unverified')
        chart=payload.get('chart')
        if not isinstance(chart,dict) or chart.get('error') or not isinstance(chart.get('result'),list):
            raise RuntimeError('Invalid Yahoo chart response; history completeness unverified')
        candles=_candles_from_yahoo_payload(payload,provider_symbol,'1d')
    else:
        from data.pse_edge import resolve_ids,_request,_CHART_URL,_CHART_REFERER,_parse_chart_date,_f
        from data.tv_client import OHLCVCandle
        ids=resolve_ids(symbol)
        if ids is None:
            raise RuntimeError('No PSE Edge security mapping; history completeness unverified')
        company,security=ids
        candles=[]
        for year in range(start.year,end.year+1):
            lower=max(start,date(year,1,1)); upper=min(end,date(year,12,31))
            body=json.dumps({'cmpy_id':str(company),'security_id':str(security),
                             'startDate':lower.strftime('%m-%d-%Y'),'endDate':upper.strftime('%m-%d-%Y')}).encode()
            payload=json.loads(_request(_CHART_URL,data=body,content_type='application/json',
                                       referer=f'{_CHART_REFERER}?cmpy_id={company}&security_id={security}').decode())
            if 'chartData' not in payload or not isinstance(payload['chartData'],list):
                raise RuntimeError('Invalid PSE chart response; history completeness unverified')
            for row in payload['chartData']:
                stamp=_parse_chart_date(str(row.get('CHART_DATE') or ''))
                values=[_f(row.get(key)) for key in ('OPEN','HIGH','LOW','CLOSE')]
                if stamp is None or any(v is None for v in values):
                    raise ValueError('Malformed PSE source bar')
                close=values[3]
                volume=(_f(row.get('VALUE')) or 0.)/close if close else 0.
                candles.append(OHLCVCandle(open=values[0],high=values[1],low=values[2],close=close,volume=volume,timestamp=stamp))
    rows={}
    invalid=[]
    for candle in candles:
        if candle.timestamp is None: continue
        ts=int(candle.timestamp.timestamp()); day=db.bar_date_from_ts(ts,market)
        if not start<=day<=end: continue
        values=(candle.open,candle.high,candle.low,candle.close,candle.volume)
        if not valid_ohlcv(values):
            invalid.append(day.isoformat());continue
        rows[day]=(ts,*map(float,values[:4]),int(float(values[4])))
    return sorted(rows.values()),{'provider':'Yahoo' if market=='us' else 'PSE Edge',
                                 'source_invalid_dates':sorted(set(invalid)),
                                 'source_latest_date':max(rows).isoformat() if rows else None}


def merge_provider_rows(conn, symbol: str, market: str, rows: list[tuple], start: date, end: date) -> dict:
    """Fill every returned session, including interior holes; preserve existing session identities."""
    with conn.cursor() as cur:
        cur.execute('SELECT ts,bar_date,open,high,low,close,volume FROM daily_bars '
                    'WHERE symbol=%s AND bar_date >= %s AND bar_date <= %s ORDER BY ts',(symbol,start,end))
        stored=cur.fetchall()
    by_day={}
    for ts,day,*values in stored: by_day.setdefault(day,[]).append((ts,*values))
    pending=[];inserted=corrected=0
    available=set()
    for row in rows:
        ts,*values=row
        day=db.bar_date_from_ts(ts,market)
        if not start<=day<=end or not valid_ohlcv(values): continue
        available.add(day)
        previous=by_day.get(day,[])
        if not previous:
            pending.append(row); inserted+=1
        else:
            # Update all existing duplicates consistently without introducing another timestamp.
            changed=False
            for old in previous:
                if not valid_ohlcv(old[1:]) or any(not math.isclose(float(a),float(b),rel_tol=1e-7,abs_tol=1e-12)
                                                 for a,b in zip(old[1:],values)):
                    pending.append((old[0],*values));changed=True
            corrected+=int(changed)
    if pending:
        db.upsert_bars(conn,symbol,pending,market=market)
        db.refresh_symbol_meta(conn,symbol)
    return {'inserted_sessions':inserted,'corrected_sessions':corrected,'provider_sessions':len(available),
            'provider_dates_verified':len(available),'status':'provider_verified' if available else 'source_empty'}


def repair_database(start: date, ends: dict[str,date], *, only_issues: bool = False,
                    workers: int = 3, report_path: Path = STATUS_PATH, audit=None, fetcher=fetch_provider, resume: bool = False) -> dict:
    """Verify full requested provider histories and repair their closed-session bars."""
    audit=audit or audit_database(start,ends)
    records=list(audit['symbols'].values())
    if only_issues:
        records=[r for r in records if r['stale'] or r['invalid_rows'] or r['potential_gap_count'] or not r['bars']]
    report={'started_at':datetime.now(ZoneInfo('Asia/Manila')).isoformat(),'audit_before':audit['summary'],
            'start':start.isoformat(),'ends':{m:d.isoformat() for m,d in ends.items()},
            'scope':'issues' if only_issues else 'all_symbols','total':len(records),'completed':0,'results':{}}
    if resume and report_path.exists():
        previous=json.loads(report_path.read_text())
        if any(previous.get(k)!=report[k] for k in ('start','ends','scope','total')):
            raise ValueError('Cannot resume a different repair scope or date range')
        report=previous
        report.pop('finished_at',None)
        records=[r for r in records if report['results'].get(r['symbol'],{}).get('status') not in
                 ('provider_verified','provider_has_invalid_bars','source_empty')]
        report['completed']=len(report['results'])
    records.sort(key=lambda r:(r['market']!='ph',r['symbol']))
    write_json(report_path,report)
    def one(record):
        symbol,market=record['symbol'],record['market']
        rows,source=fetcher(symbol,market,start,ends[market])
        with db.get_conn() as conn:
            result=merge_provider_rows(conn,symbol,market,rows,start,ends[market])
        available={db.bar_date_from_ts(row[0],market).isoformat() for row in rows}
        result['calendar_gap_dates_without_provider_bar']=[d for d in record.get('potential_gap_dates',[]) if d not in available and d not in source['source_invalid_dates']]
        result.update(source,market=market,checked_at=datetime.now(ZoneInfo('Asia/Manila')).isoformat())
        if source['source_invalid_dates']:
            result['status']='provider_has_invalid_bars'
        return result
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(one,r):r for r in records}
        for future in as_completed(futures):
            record=futures[future]
            try: result=future.result()
            except Exception as exc:
                result={'market':record['market'],'status':'source_unverified','error':f'{type(exc).__name__}: {exc}'}
            report['results'][record['symbol']]=result
            report['completed']=len(report['results'])
            if report['completed']%25==0 or report['completed']==report['total']:
                write_json(report_path,report)
                log.info(f"daily-bars | verified {report['completed']}/{report['total']} symbols")
    report['finished_at']=datetime.now(ZoneInfo('Asia/Manila')).isoformat()
    report['audit_after']=audit_database(start,ends)['summary']
    report['totals']={key:sum(r.get(key,0) for r in report['results'].values())
                      for key in ('inserted_sessions','corrected_sessions','provider_sessions')}
    report['statuses']={status:sum(r['status']==status for r in report['results'].values())
                        for status in sorted({r['status'] for r in report['results'].values()})}
    write_json(report_path,report)
    return report


def fetch_secondary_us(symbols: list[str], start: date, end: date, client) -> dict[str,list[tuple]]:
    """Fetch complete, paginated SIP daily history; discard partial failed pages."""
    from data.alpaca_client import _parse_ts
    params={'symbols':','.join(symbols),'timeframe':'1Day','start':start.isoformat()+'T00:00:00Z',
            'end':(end+timedelta(days=1)).isoformat()+'T00:00:00Z','adjustment':'raw','feed':'sip','limit':'10000'}
    result={s:[] for s in symbols}
    tokens=set()
    while True:
        payload=client._request('/v2/stocks/bars',params)
        if payload is None or not isinstance(payload.get('bars'),dict):
            raise RuntimeError('Secondary US source request failed; partial pages are not complete')
        for symbol,bars in payload['bars'].items():
            for bar in bars:
                row=(_parse_ts(bar['t']),*[float(bar[k]) for k in ('o','h','l','c')],int(bar.get('v') or 0))
                day=db.bar_date_from_ts(row[0],'us')
                if start<=day<=end and valid_ohlcv(row[1:]):
                    result.setdefault(symbol,[]).append(row)
        token=payload.get('next_page_token')
        if not token: break
        if token in tokens: raise RuntimeError('Secondary source repeated a pagination token')
        tokens.add(token);params['page_token']=token
    return result


def merge_secondary_rows(conn, symbol: str, rows: list[tuple], start: date, end: date) -> dict:
    """Only fill holes/bad primary bars when nearby raw-price overlap agrees.

    Good primary observations remain authoritative. A split-adjustment mismatch
    or insufficient local overlap cannot silently splice incompatible series.
    """
    with conn.cursor() as cur:
        cur.execute('SELECT ts,bar_date,open,high,low,close,volume FROM daily_bars '
                    'WHERE symbol=%s AND bar_date >= %s AND bar_date <= %s ORDER BY ts',(symbol,start,end))
        stored=cur.fetchall()
    by_day={}
    for ts,day,*values in stored: by_day.setdefault(day,[]).append((ts,*values))
    source={db.bar_date_from_ts(row[0],'us'):row for row in rows
            if start<=db.bar_date_from_ts(row[0],'us')<=end and valid_ohlcv(row[1:])}
    overlaps={day:math.isclose(float(old[-1][4]),float(source[day][4]),rel_tol=.02,abs_tol=1e-8)
              for day,old in by_day.items() if day in source and valid_ohlcv(old[-1][1:])}
    accepted=[];rejected=[]
    for day,row in source.items():
        existing=by_day.get(day,[])
        if existing and all(valid_ohlcv(old[1:]) for old in existing): continue
        nearby=[agrees for other,agrees in overlaps.items() if abs((other-day).days)<=45]
        # A malformed range/volume can still have the exact independently
        # confirmed same-day close. That proves this bar's price scale without
        # splicing an adjusted historical regime from neighboring dates.
        same_day_basis=any(math.isfinite(float(old[4])) and float(old[4])>0
                           and math.isclose(float(old[4]),float(row[4]),rel_tol=1e-7,abs_tol=1e-12)
                           for old in existing)
        if stored and not same_day_basis and (len(nearby)<3 or not all(nearby)):
            rejected.append(day.isoformat());continue
        accepted.append(row)
    result=merge_provider_rows(conn,symbol,'us',accepted,start,end)
    result.update(secondary_provider='Alpaca SIP/raw',source_sessions=len(source),
                  incompatible_or_unverified_dates=sorted(rejected))
    return result


def secondary_source_symbol(symbol: str, assets: dict[str,str]) -> str | None:
    """Resolve source-specific share-class spellings against the asset registry."""
    import re
    symbol=symbol.upper().split(':')[-1]
    candidates=[symbol]
    preferred=re.fullmatch(r'(.+)-P([A-Z]+)',symbol)
    if preferred: candidates.append(preferred[1]+'.PR'+preferred[2])
    for old,new in (('-WT','.WS'),('-UN','.U'),('-RI','.RT')):
        if symbol.endswith(old): candidates.append(symbol[:-len(old)]+new)
    candidates.append(symbol.replace('-','.'))
    for candidate in candidates:
        if candidate in assets: return candidate
    # Plain historical tickers can still exist in the bars service after delisting.
    return symbol if re.fullmatch(r'[A-Z0-9.]+',symbol) else None


def repair_secondary_us(primary: dict, report_path: Path) -> dict:
    """Cross-check unresolved US histories using the already configured SIP feed."""
    from config import settings
    from data.alpaca_client import AlpacaClient,AlpacaBadRequest
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        raise RuntimeError('Secondary source is not configured')
    start=date.fromisoformat(primary['start']);end=date.fromisoformat(primary['ends']['us'])
    from learn.kronos_weekly_dataset import is_session
    targets=sorted(s for s,r in primary['results'].items() if r['market']=='us' and
                   (r['status']!='provider_verified' or any(is_session(date.fromisoformat(day),'us') for day in r.get('calendar_gap_dates_without_provider_bar',[])) or r.get('source_latest_date')!=end.isoformat()))
    report={'started_at':datetime.now(ZoneInfo('Asia/Manila')).isoformat(),'start':str(start),
            'end':str(end),'total':len(targets),'completed':0,'results':{}}
    if report_path.exists():
        previous=json.loads(report_path.read_text())
        if all(previous.get(k)==report[k] for k in ('start','end','total')):
            report=previous
            report.pop('finished_at',None)
    client=AlpacaClient(settings.alpaca_api_key,settings.alpaca_secret_key,feed='sip',paper=settings.alpaca_paper,
                        requests_per_min=settings.alpaca_requests_per_min)
    assets=client.fetch_assets()
    if not assets: raise RuntimeError('Secondary asset registry unavailable; source identities unverified')
    names={s:secondary_source_symbol(s,assets) for s in targets}
    pending=[]
    for symbol in targets:
        if report['results'].get(symbol,{}).get('status') in ('secondary_checked','no_sip_bars'): continue
        if names[symbol] is None:
            report['results'][symbol]={'status':'unsupported_source_symbol','error':'No asset-verified SIP share-class spelling'}
        else: pending.append(symbol)
    report['completed']=len(report['results'])
    def batch(symbols):
        error=None
        try: histories=fetch_secondary_us(list(dict.fromkeys(names[s] for s in symbols)),start,end,client)
        except AlpacaBadRequest as exc:
            if len(symbols)>1:
                half=len(symbols)//2;batch(symbols[:half]);batch(symbols[half:]);return
            histories=None;error=str(exc)
        except Exception as exc:
            histories=None;error=f'{type(exc).__name__}: {exc}'
            log.warning(f'daily-bars | secondary batch failed: {error}')
        for symbol in symbols:
            if histories is None:
                result={'status':'secondary_unverified','error':error}
            elif not histories.get(names[symbol]):
                result={'status':'no_sip_bars'}
            else:
                try:
                    with db.get_conn() as conn:
                        result=merge_secondary_rows(conn,symbol,histories[names[symbol]],start,end)
                    result['status']='secondary_checked'
                except Exception as exc:
                    result={'status':'secondary_unverified','error':f'{type(exc).__name__}: {exc}'}
            result['source_symbol']=names[symbol]
            report['results'][symbol]=result
        report['completed']=len(report['results'])
        write_json(report_path,report)
        log.info(f"daily-bars | secondary checked {report['completed']}/{len(targets)} symbols")
    for pos in range(0,len(pending),50): batch(pending[pos:pos+50])
    report['finished_at']=datetime.now(ZoneInfo('Asia/Manila')).isoformat()
    report['totals']={key:sum(r.get(key,0) for r in report['results'].values())
                      for key in ('inserted_sessions','corrected_sessions')}
    write_json(report_path,report)
    return report


def redundant_session_timestamps(stored: list[tuple]) -> list[int]:
    """Remove only identical valid copies; preserve conflicting observations."""
    by_day={}
    for ts,day,*values in stored:
        by_day.setdefault(day,[]).append((int(ts),tuple(values)))
    redundant=[]
    for copies in by_day.values():
        if len(copies)<2: continue
        if (not all(valid_ohlcv(values) for _,values in copies)
                or len({values for _,values in copies})!=1): continue
        keep=max(ts for ts,_ in copies)
        redundant.extend(ts for ts,_ in copies if ts!=keep)
    return redundant


def deduplicate_identical_sessions(audit: dict) -> dict:
    """Collapse byte-equivalent OHLCV copies without resolving source conflicts."""
    start=date.fromisoformat(audit['start'])
    ends={m:date.fromisoformat(d) for m,d in audit['ends'].items()}
    removed={};conflicts={}
    for symbol,record in audit['symbols'].items():
        if not record['duplicate_sessions']: continue
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT ts,bar_date,open,high,low,close,volume FROM daily_bars '
                            'WHERE symbol=%s AND bar_date >= %s AND bar_date <= %s ORDER BY ts',
                            (symbol,start,ends[record['market']]))
                redundant=redundant_session_timestamps(cur.fetchall())
                if redundant:
                    cur.execute('DELETE FROM daily_bars WHERE symbol=%s AND ts=ANY(%s)',(symbol,redundant))
                    removed[symbol]=cur.rowcount
            if redundant: db.refresh_symbol_meta(conn,symbol)
        if record['duplicate_sessions']>len(redundant):
            conflicts[symbol]=record['duplicate_sessions']-len(redundant)
    return {'removed_rows':sum(removed.values()),'symbols':removed,'remaining_conflicting_copies':conflicts}


def apply_authoritative_corrections(corrections: list[dict], ends: dict[str,date]) -> list[dict]:
    """Apply reviewed exchange-report bars idempotently, retaining timestamps."""
    results=[]
    for correction in corrections:
        symbol,market=correction['symbol'],correction['market']
        day=date.fromisoformat(correction['date'])
        values=tuple(correction['bar'][key] for key in ('open','high','low','close','volume'))
        expected=tuple(correction['expected'][key] for key in ('open','high','low','close','volume'))
        if day>ends[market] or not valid_ohlcv(values) or not correction.get('source_url'):
            raise ValueError('Correction requires a valid closed bar and source evidence')
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT ts,open,high,low,close,volume FROM daily_bars '
                            'WHERE symbol=%s AND bar_date=%s ORDER BY ts FOR UPDATE',(symbol,day))
                stored=cur.fetchall()
            if not stored: raise ValueError(f'{symbol} {day}: expected existing bar for reviewed correction')
            for row in stored:
                if tuple(row[1:]) not in (expected,values):
                    raise ValueError(f'{symbol} {day}: stored bar changed; re-review source correction')
            result=merge_provider_rows(conn,symbol,market,[(stored[-1][0],*values)],day,day)
        results.append({'symbol':symbol,'date':str(day),'source_url':correction['source_url'],**result})
    return results


def closed_session_placeholders(rows: list[tuple]) -> list[tuple]:
    """Identify malformed US records on closed cash sessions; keep valid OTC prints."""
    from learn.kronos_weekly_dataset import is_session
    return [row for row in rows if row[1]=='us' and not is_session(row[3],'us') and not valid_ohlcv(row[4:])]


def quarantine_closed_placeholders(audit: dict, backup_path: Path) -> dict:
    """Preserve bad non-session placeholders before removing them from model input."""
    from learn.kronos_weekly_dataset import is_session
    if backup_path.exists(): raise ValueError('Quarantine backup already exists; do not overwrite preserved evidence')
    selected=[]
    with db.get_conn() as conn:
        for symbol,record in audit['symbols'].items():
            if record['market']!='us': continue
            days=[date.fromisoformat(d) for d in record['invalid_dates'] if not is_session(date.fromisoformat(d),'us')]
            if not days: continue
            with conn.cursor() as cur:
                cur.execute('SELECT ts,bar_date,open,high,low,close,volume FROM daily_bars '
                            'WHERE symbol=%s AND bar_date=ANY(%s) ORDER BY ts',(symbol,days))
                selected.extend(closed_session_placeholders([(symbol,'us',*row) for row in cur.fetchall()]))
        backup={'reason':'Malformed OHLCV placeholder on a closed US cash session; valid holiday OTC prints are retained',
                'created_at':datetime.now(ZoneInfo('Asia/Manila')).isoformat(),
                'columns':['symbol','market','ts','bar_date','open','high','low','close','volume'],
                'rows':[[*row[:3],str(row[3]),*row[4:]] for row in selected]}
        # Durable evidence precedes deletion; exact old values guard against concurrent changes.
        write_json(backup_path,backup)
        removed=0
        with conn.cursor() as cur:
            for symbol,market,ts,day,*values in selected:
                cur.execute('DELETE FROM daily_bars WHERE symbol=%s AND ts=%s AND '
                            'open IS NOT DISTINCT FROM %s AND high IS NOT DISTINCT FROM %s AND '
                            'low IS NOT DISTINCT FROM %s AND close IS NOT DISTINCT FROM %s AND volume=%s',
                            (symbol,ts,*values))
                removed+=cur.rowcount
        for symbol in sorted({row[0] for row in selected}): db.refresh_symbol_meta(conn,symbol)
    return {'removed_placeholders':removed,'preserved_rows':len(selected),'backup_path':str(backup_path)}
