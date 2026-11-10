"""Descriptive V2 cohorts with explicit journal linkage and open-position marks."""
from __future__ import annotations

from collections import defaultdict


def cohort_review(book: dict, verified_rows: list[dict]) -> dict:
    """Attribute observed losses without selecting a new filter from outcomes."""
    from analysis.paper_trade_review import summarize

    trades = book.get('closed_trades', []) + book.get('open_positions', [])
    by_id = {t['trade_id']: t for t in trades}
    if len(by_id) != len(trades) or len({r['trade_id'] for r in verified_rows}) != len(verified_rows):
        raise ValueError('Cohort attribution requires unique trade identities')
    if set(by_id) != {r['trade_id'] for r in verified_rows}:
        raise ValueError('Cohort rows must cover the same verified ledger')
    logs = book.get('signal_logs') or []
    rows = [r for r in verified_rows if by_id[r['trade_id']].get('provenance') == 'patternsv2']
    def dii_group(row):
        trade = by_id[row['trade_id']]
        fills = [log for log in logs if log.get('status') == 'filled'
                 and log.get('trade_id') == trade['trade_id']]
        if not fills and trade.get('signal_id'):
            fills = [log for log in logs if log.get('status') == 'filled'
                     and log.get('signal_id') == trade['signal_id']]
        runs = {log.get('run_id') for log in fills}
        if len(runs) != 1 or not fills or not trade.get('signal_id'):
            return 'unknown_journal_linkage'
        if any(log.get('status') == 'shadow' and log.get('reason', '').startswith('DII')
               and log.get('signal_id') == trade.get('signal_id') and log.get('run_id') in runs
               for log in logs):
            return 'shadow_would_veto'
        return 'no_shadow_veto_recorded'
    total = sum(r['pnl'] for r in rows if r['exit_reason'] != 'open')
    def summary(group):
        closed = [r for r in group if r['exit_reason'] != 'open']
        stats = summarize(closed)
        return {'closed': stats, 'open_count': sum(r['exit_reason'] == 'open' for r in group),
                'open_marked_pnl': sum(r.get('marked_open_pnl', 0) for r in group),
                'total_liquidation_pnl': sum(r['pnl'] for r in group),
                'signed_share_of_realized_pnl_pct': 100*stats['net_pnl']/total if total else None}
    def groups(key):
        grouped = defaultdict(list)
        for row in rows:
            grouped[key(row)].append(row)
        return {label: summary(group) for label, group in sorted(grouped.items())}
    decisions = defaultdict(list)
    for log in logs:
        if log.get('provenance') != 'patternsv2' or log.get('status') == 'exited':
            continue
        identity = log.get('signal_id') or tuple(log.get(k) for k in ('symbol', 'timeframe', 'pattern', 'action', 'sim_bar'))
        decisions[(log.get('run_id'), identity)].append(log)
    opportunities = defaultdict(lambda: {'detected': 0, 'filled': 0, 'rejected': 0, 'pending': 0, 'dii_shadow_veto': 0})
    for journal in decisions.values():
        first = journal[0]
        key = f"{first.get('pattern')}|{first.get('action')}"
        stats = opportunities[key]
        stats['detected'] += 1
        statuses = {log.get('status') for log in journal}
        terminal = 'filled' if 'filled' in statuses else 'rejected' if 'rejected' in statuses else 'pending'
        stats[terminal] += 1
        stats['dii_shadow_veto'] += any(log.get('status') == 'shadow' and log.get('reason', '').startswith('DII')
                                        for log in journal)
    return {'scope': 'Patternsv2 observed fill cohorts; no counterfactual funding or rejected paths inferred',
            'validation': 'descriptive_inspected_sample', 'automatic_filter_change': False,
            'included_trades': len(rows), 'excluded_other_detectors': len(verified_rows)-len(rows),
            'by_family_and_side': groups(lambda r: f"{r['pattern']}|{r['action']}"),
            'by_dii_shadow': groups(dii_group),
            'by_entry_month': groups(lambda r: r['entry_date'][:7]),
            'opportunities_by_family_and_side': dict(sorted(opportunities.items()))}
