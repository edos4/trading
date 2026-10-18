"""Offline smoke: real cached candles and repository detectors, isolated paper account.
Run from repository root: PYTHONPATH=. .venv/bin/python docs/verification/backtest-paper-smoke-2026-10-17/run.py
"""
import asyncio
import hashlib
import importlib
import inspect
import json
from pathlib import Path
from core.backtest_params import settings_from_values
from core.backtester import _make_snapshot
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from core.stream_backtest import StreamReplay
from data.barcache import load
from patterns.base_pattern import BasePattern
from utils.logger import log

OUT = Path(__file__).parent
ROOT = Path('tests/fixtures/barcache')
SYMBOLS = ['CDNS','NET','ON','TXN','SWKS','QCOM','PCAR','PYPL']
MODULES = ['002_double_top','003_double_bottom','004_rounding_bottom','005_rounding_top','008_head_and_shoulders']
# Repository detectors are imported directly; the database/version API is outside this offline check.
def patterns():
    result=[]
    for name in MODULES:
        if not Path('patterns', name+'.py').exists():
            continue
        module=importlib.import_module('patterns.'+name)
        result.extend(cls() for _,cls in inspect.getmembers(module,inspect.isclass)
                      if cls.__module__==module.__name__ and issubclass(cls,BasePattern))
    return result

async def run(gated):
    bars={s:load('us',s,root=ROOT)[-260:] for s in SYMBOLS}
    assert all(len(v)==260 for v in bars.values())
    dates=[b.timestamp for b in bars[SYMBOLS[0]]]
    assert all([b.timestamp for b in v]==dates for v in bars.values())
    dataset={s:[[b.timestamp.isoformat(),b.open,b.high,b.low,b.close,b.volume] for b in v]
             for s,v in bars.items()}
    settings=settings_from_values(dict(mode='historical-stream',market='us',symbols=SYMBOLS,
        start_date=dates[30].date().isoformat(),end_date=dates[-1].date().isoformat(),warmup_bars=30,
        txn_cost_pct=.001,slippage_pct=.001,initial_capital=100000,position_notional=10000,
        volume_gate=gated,collect_first=4 if gated else 0,end_policy='keep-open'))
    replay=StreamReplay(dataset=dataset,patterns=patterns(),settings=settings,session_tz='America/New_York')
    result=replay.run()
    account=PaperAccount(market='us',initial_capital=100000,txn_cost_pct=.001,slippage_pct=.001)
    account.assume_session_open=True
    scanner=MarketScanner(symbols=SYMBOLS,paper_account=account,market='us',volume_gate=gated,
                          collect_first=gated,collect_first_top_n=4,pattern_only=True)
    scanner._patterns=patterns()
    scanner._record_detection=lambda s:None
    events=[]
    scanner._append_signal_log=lambda s,**kw:events.append(dict(symbol=s.symbol,pattern=s.pattern,**kw))
    equity=[]
    for i,stamp in enumerate(dates):
        scanner._scan_snapshot_symbols=set()
        scanner._scan_daily_dates=set()
        snapshots=[]
        for symbol in SYMBOLS:
            bar=bars[symbol][i]
            scanner._store.apply_candle(symbol,'1d',bar)
            _,snapshot=scanner._apply_bar(symbol,'1d',_make_snapshot(symbol,'1d',bar))
            if snapshot is not None and i>=30:
                snapshots.append(snapshot)
        await scanner._run_analyzes(snapshots)
        await scanner._flush_collect_first()
        equity.append(account.mark_to_market(stamp))
    def norm(t,paper=False):
        return (t.symbol,t.pattern,t.action,round(t.entry_price,8),round(t.qty,8),
                round(t.exit_price or 0,8),t.exit_reason, (t.sim_entry_date if paper else t.entry_date),
                ((t.sim_exit_date if paper else t.exit_date) if t.exit_reason else None))
    # Compare actual execution objects as exports intentionally have different layouts.
    backclosed=[norm(t) for t in replay._closed_order]
    paperclosed=[norm(t,True) for t in account.closed]
    backopen=sorted(norm(t) for ss in replay.series.values() for t in ss.open_positions)
    paperopen=sorted(norm(t,True) for _,t in account.positions_snapshot())
    assert sorted(backclosed)==sorted(paperclosed),(backclosed,paperclosed)
    assert backopen==paperopen,(backopen,paperopen)
    assert len(equity)==len(result['equity'])
    error=max(abs(a-b['equity']) for a,b in zip(equity,result['equity']))
    assert error<1e-6,error
    assert abs(account.snapshot_metrics()['fees']-result['metrics']['fees'])<1e-6
    assert not any('Signal handling failed:' in e.get('reason','') for e in events)
    assert len(backclosed)+len(backopen)>0,'Smoke must exercise real fills'
    summary=dict(gated_top4=gated,patterns=[p.name for p in scanner._patterns],symbols=SYMBOLS,
        warmup_bars=30,start=dates[30].isoformat(),end=dates[-1].isoformat(),sessions=len(equity),
        commission_per_leg=.001,slippage_per_fill=.001,initial_capital=100000,position_notional=10000,
        metrics=result['metrics'],paper_metrics=account.snapshot_metrics(),final_equity=account.equity(),
        max_equity_difference=error,closed_and_open_fills_equal=True,paper_events=len(events),
        source_sha256={s:hashlib.sha256((ROOT/'us'/f'{s}.json').read_bytes()).hexdigest() for s in SYMBOLS})
    label='gated' if gated else 'baseline'
    (OUT/(label+'-backtest.json')).write_text(json.dumps(result,indent=2,default=str))
    (OUT/(label+'-summary.json')).write_text(json.dumps(summary,indent=2,default=str))
    print(json.dumps(summary,default=str),flush=True)

if __name__=='__main__':
    log.remove()
    asyncio.run(run(False))
    asyncio.run(run(True))
