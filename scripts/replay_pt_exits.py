#!/usr/bin/env python3
"""Offline exit-only audit of pt.json against frozen stream tapes.

Run: .venv/bin/python scripts/replay_pt_exits.py
Keeps exported entries/quantities; does not rescan or model portfolio gates.
Costs are 0.1% each side, no slippage (validated against each exported close).
Requires exactly one cached tape per symbol; never fetches or edits a ledger.
"""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.backtester import _check_exit, _close_trade, _open_trade
from core.pattern_provenance import RULE_FIELDS
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal
from utils.logger import set_console_level


def replay(trade, candles, *, trail, activation):
    rules = trade['resolved_rules']
    date = datetime.fromisoformat(trade['sim_opened']).date()
    idx = next(i for i, c in enumerate(candles) if c.timestamp.date() == date)
    if not math.isclose(candles[idx].close, rules['price'], abs_tol=.0001):
        raise ValueError(f"Entry candle mismatch: {trade['symbol']} {date}")
    signal = TradeSignal(
        symbol=trade['symbol'], timeframe='1d', pattern=trade['pattern'],
        action=trade['action'], confidence=1, price=rules['price'], qty=trade['qty'],
        provenance='patternsv2', **{k: rules[k] for k in RULE_FIELDS},
    )
    signal.trailing_stop_pct = trail
    signal.trailing_activation_pct = activation
    position = _open_trade(signal, candles[idx], idx)
    position.entry_price = rules['price']
    for j in range(idx + 1, len(candles)):
        price, reason = _check_exit(candles[j], position, j)
        if price is not None:
            _close_trade(position, price, reason, candles[j], .001)
            return {'symbol': trade['symbol'], 'entry_date': str(date),
                    'exit_date': str(candles[j].timestamp.date()), 'reason': reason,
                    'pnl': position.pnl_usd, 'pnl_pct': position.pnl_pct}
    return {'symbol': trade['symbol'], 'entry_date': str(date),
            'exit_date': None, 'reason': 'open', 'pnl': None, 'pnl_pct': None}


def main():
    set_console_level('WARNING')
    export = json.loads((ROOT / 'pt.json').read_text())
    book = next(b for b in export['books'] if b['market'] == 'us')
    trades = book['closed_trades'] + book['open_positions']
    cutoff = datetime.fromisoformat(export['exported_at'])
    tapes = {}
    for trade in trades:
        symbol = trade['symbol']
        if symbol in tapes:
            continue
        paths = list((ROOT / 'data/barcache/stream_tapes/us').glob(f'{symbol}_*.json'))
        if len(paths) != 1:
            raise ValueError(f'Expected one frozen tape for {symbol}, found {len(paths)}')
        rows = json.loads(paths[0].read_text())['rows']
        candles = [OHLCVCandle(**{**r, 'timestamp': datetime.fromtimestamp(
            r['timestamp'], timezone.utc)}) for r in rows]
        tapes[symbol] = sorted((c for c in candles if c.timestamp <= cutoff),
                               key=lambda c: c.timestamp)
    results = {}
    for name, trail, activation in [('export_rules', .05, .05),
                                     ('later_activation', .05, .10),
                                     ('no_trail', None, .05),
                                     ('half_width_trail', .025, .05)]:
        rows = [replay(t, tapes[t['symbol']], trail=trail, activation=activation)
                for t in trades]
        if name == 'export_rules':
            for trade, row in zip(trades, rows):
                if trade in book['closed_trades']:
                    expected_date = str(datetime.fromisoformat(trade['sim_closed']).date())
                    if (row['exit_date'] != expected_date
                            or row['reason'] != trade['exit_reason']
                            or row['pnl'] is None
                            or not math.isclose(row['pnl'], trade['pnl'], abs_tol=.01)):
                        raise ValueError(f'Baseline does not reproduce export: {row}')
                elif row['exit_date'] is not None:
                    raise ValueError(f'Baseline unexpectedly closes open trade: {row}')
        closed = [r for r in rows if r['pnl'] is not None]
        results[name] = {'closed_count': len(closed),
                         'realized_pnl': sum(r['pnl'] for r in closed), 'trades': rows}
    print(json.dumps({'exported_at': export['exported_at'],
                      'baseline_verified': True, 'scenarios': results}, indent=2))


if __name__ == '__main__':
    main()
