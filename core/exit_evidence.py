"""Attribute an executed order without changing its precedence or fill."""
from __future__ import annotations


def trigger_source(state: dict, action: str, candle, reason: str) -> str | None:
    if not reason:
        return None
    if state.get('pending_exit_reason'):
        return 'kronos' if reason=='kronos_exit' else 'completed_close_decision'
    if reason=='take_profit':
        return 'pattern_target'
    short=action=='SELL'
    candidates=[]
    paper=state.get('paper_protective_stop')
    label='trailing_stop' if state.get('paper_peak_net_pct',0)>0 else 'stop_loss'
    if paper is not None and label==reason:
        candidates.append((paper,'paper_protection'))
    for key,label in [('hard_stop','stop_loss'),('trailing_stop','trailing_stop')]:
        if reason==label and state.get(key) is not None and not state.get(key+'_on_close'):
            candidates.append((state[key],'pattern_'+key))
    candidates.sort(key=lambda x:x[0],reverse=not short)
    for price,source in candidates:
        if (max(candle.open,candle.high)>=price if short else min(candle.open,candle.low)<=price):
            return source
    return 'pattern_close_or_timer'
