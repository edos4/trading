"""The model must consume the exact dated tape used by the scanner."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import pandas as pd
import pytest

from core.kronos_context import context_frame,frame_context
from core.kronos_confirm import KronosConfirmGate
from core.test_scanner_kronos_confirm import _candle,_signal


def test_context_is_causal_and_detects_changed_earlier_history():
    frame=pd.DataFrame({k:[100.]*70 for k in ['open','high','low','close','volume']},
                       index=pd.bdate_range(end='2026-11-03',periods=70))
    context=frame_context(frame,'2026-10-30','1d')
    assert context['rows'][-1]['date']=='2026-10-30'
    assert len(context_frame(context,'2026-10-30'))==68
    changed=deepcopy(context); changed['rows'][0]['close']=99
    with pytest.raises(ValueError,match='identity'):
        context_frame(changed,'2026-10-30')
    with pytest.raises(ValueError,match='future'):
        context_frame(context,'2026-10-29')


def test_worker_receives_exact_context_and_budget_order_is_deterministic(monkeypatch):
    seen=[]
    frame=pd.DataFrame({k:[100.]*60 for k in ['open','high','low','close','volume']},
                       index=pd.bdate_range(end='2026-10-30',periods=60))
    def worker(script,args,**kw):
        source=json.loads(Path(args[args.index('--contexts')+1]).read_text())
        symbol=args[args.index('--symbols')+1]; seen.append(symbol)
        return {'market':'us','model':'finetuned','samples':3,'results':[
            {'symbol':symbol,'anchor_date':'2026-10-30','anchor_close':100,'atr_pct':0,
             'returns':[.05]*3,'context_sha256':source[symbol]['sha256']}]}
    monkeypatch.setattr('core.kronos_confirm.run_worker',worker)
    for order in [('ZZZ','AAA'),('AAA','ZZZ')]:
        gate=KronosConfirmGate(mode='confirm',max_checks_per_scan=1)
        for symbol in order:
            gate.queue(_signal(symbol=symbol),_candle(),frame)
        rows=gate.flush()
        verdicts={s.symbol:(v.decision,e['failure_kind']) for s,c,v,e in rows}
        assert verdicts=={'AAA':('confirm','model_vote'),'ZZZ':('no_opinion','capacity')}
    assert seen==['AAA','AAA']
