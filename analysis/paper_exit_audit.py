"""Replay saved paper exits against local OHLC tapes, without rescanning.

Run ``.venv/bin/python -m analysis.paper_exit_audit pt.json``. No network or
ledger writes. Baseline verification must pass before counterfactuals print.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median

from core.backtester import _check_exit, _close_trade, _open_trade, trade_risk_dollars
from core.paper_trader import exit_state
from core.pattern_provenance import RULE_FIELDS, engine_hash
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


def summarize(rows: list[dict]) -> dict:
    """Net money metrics, kept separate for each currency/book."""
    wins = [r['pnl'] for r in rows if r['pnl'] > 0]
    losses = [r['pnl'] for r in rows if r['pnl'] < 0]
    avg_win, avg_loss = mean(wins) if wins else 0, mean(losses) if losses else 0
    return {
        'count': len(rows), 'wins': len(wins), 'losses': len(losses),
        'net_pnl': sum(r['pnl'] for r in rows),
        'win_rate_pct': len(wins) / len(rows) * 100 if rows else None,
        'profit_factor': sum(wins) / -sum(losses) if losses else None,
        'average_win': avg_win, 'average_loss': avg_loss,
        'breakeven_win_rate_pct': -avg_loss / (avg_win - avg_loss) * 100
        if avg_win > 0 and avg_loss < 0 else None,
        'exit_reasons': dict(Counter(r['exit_reason'] for r in rows)),
    }


def replay_trade(trade: dict, candles: list[OHLCVCandle], cutoff: datetime, *,
                 txn_cost: float = .001, slippage: float = 0,
                 scenario: str = 'saved_rules') -> dict:
    """Use snapshotted fill rules, quantity and next-open entry, including day 1.

    The open remainder is valued at the last available close, with hypothetical
    liquidation fees/slippage, so a realized gain cannot conceal open losses.
    """
    if scenario not in ('saved_rules', 'no_trail', 'no_target', 'requested_target'):
        raise ValueError('Unknown exit scenario')
    rules = trade.get('resolved_rules') or {}
    if rules.get('engine_sha256') != engine_hash():
        raise ValueError('Saved exit runtime differs; restore it before auditing')
    if trade.get('execution_model') != 'causal-v1':
        raise ValueError('This audit requires causal-v1 positions')
    start = datetime.fromisoformat(trade['sim_opened'])
    bars = sorted((c for c in candles if start <= c.timestamp <= cutoff),
                  key=lambda c: c.timestamp)
    if not bars or bars[0].timestamp != start:
        raise ValueError('Tape has no entry bar')
    if len({c.timestamp for c in bars}) != len(bars):
        raise ValueError('Tape has duplicate bars')
    short = trade['action'] == 'SELL'
    entry = rules['price']
    if not math.isclose(bars[0].open * (1 - slippage if short else 1 + slippage),
                        entry, abs_tol=.0001, rel_tol=0):
        raise ValueError('Tape does not match the entry fill')
    signal = TradeSignal(
        symbol=trade['symbol'], timeframe=trade['timeframe'], pattern=trade['pattern'],
        action=trade['action'], confidence=1, price=entry, qty=rules['qty'],
        provenance=trade['provenance'],
        **{k: rules.get(k) for k in RULE_FIELDS if k != 'execution_model'},
    )
    if scenario == 'no_trail':
        signal.trailing_stop_pct = None
    if scenario == 'no_target':
        signal.take_profit = None
    if scenario == 'requested_target':
        requested = trade.get('requested_rules') or {}
        if 'take_profit' not in requested:
            raise ValueError('No saved pre-cap target to restore')
        signal.take_profit = requested['take_profit']
    point = replace(bars[0], open=entry, high=entry, low=entry, close=entry)
    position = _open_trade(signal, point, 0, execution_model='causal-v1')
    risk = trade_risk_dollars(position)
    risk_frac = risk / (entry * position.qty) if risk else None
    marks = {m['sim_bar']: m for m in trade.get('daily_marks', []) if m.get('sim_bar')}
    ambiguous, armed, best_close = [], False, 0.0
    for index, bar in enumerate(bars, 1):
        state = exit_state(position)
        armed = armed or state['trailing_stop'] is not None
        stops = [state[k] for k, close_flag in (
            ('hard_stop', 'hard_stop_on_close'), ('trailing_stop', 'trailing_stop_on_close'))
            if state[k] is not None and not state[close_flag]]
        stop = (min(stops) if short else max(stops)) if stops else None
        target = state['target'] if not state['target_on_close'] else None
        stop_touch = stop is not None and (bar.high >= stop if short else bar.low <= stop)
        target_touch = target is not None and (bar.low <= target if short else bar.high >= target)
        open_order = ((stop is not None and (bar.open >= stop if short else bar.open <= stop))
                      or (target is not None and (bar.open <= target if short else bar.open >= target)))
        if stop_touch and target_touch and not open_order and not state['pending_exit_reason']:
            ambiguous.append(bar.timestamp.isoformat())
        price, reason = _check_exit(bar, position, index)
        if price is not None:
            fill = price * (1 + slippage if short else 1 - slippage)
            _close_trade(position, fill, reason, bar, txn_cost)
            break
        # Final marks are execution fills; only survivor marks are closes.
        mark = marks.get(bar.timestamp.isoformat())
        if scenario == 'saved_rules' and mark and mark.get('status') in ('OPEN', 'TRAILING'):
            if not math.isclose(mark['close'], bar.close, abs_tol=.00006, rel_tol=0):
                raise ValueError('Tape disagrees with a saved session close')
        best_close = max(best_close, position._best_pnl_pct or 0)
        armed = armed or position._trailing_activated
    closed = bool(position.exit_reason)
    last = bar
    if not closed:
        fill = last.close * (1 + slippage if short else 1 - slippage)
        move = entry - fill if short else fill - entry
        net_pnl = (move - (entry + fill) * txn_cost) * position.qty
    else:
        net_pnl = position.pnl_usd
    return {
        'trade_id': trade.get('trade_id'), 'symbol': trade['symbol'], 'action': trade['action'],
        'pattern': trade['pattern'], 'exit_reason': position.exit_reason or 'open',
        'exit_date': last.timestamp.isoformat() if closed else None,
        'last_bar': last.timestamp.isoformat(),
        'fill': position.exit_price if closed else None,
        'pnl': net_pnl, 'realized_pnl': net_pnl if closed else 0,
        'open_liquidation_pnl': net_pnl if not closed else 0,
        'net_r': net_pnl / risk if risk else None,
        'risk_pct': risk_frac * 100 if risk_frac is not None else None,
        'target_r': abs(signal.take_profit - entry) * position.qty / risk
        if signal.take_profit is not None and risk else None,
        'activation_r': signal.trailing_activation_pct / risk_frac
        if signal.trailing_activation_pct is not None and risk_frac else None,
        'trailing_armed': armed,
        'best_survivor_close_pct': best_close * 100,
        'ambiguous_bars': ambiguous,
        'bars': index, 'same_session_exit': closed and last.timestamp == start,
        'entry_range_r': (bars[0].high - bars[0].low) * position.qty / risk if risk else None,
        'gap_fill': closed and not state['pending_exit_reason']
        and (not math.isclose(price, target, abs_tol=.000001) if reason == 'take_profit'
             and target is not None else not math.isclose(price, stop, abs_tol=.000001)
             if stop is not None and reason in ('stop_loss', 'trailing_stop') else False),
    }


def verify_close(trade: dict, replay: dict) -> None:
    """Refuse to publish a counterfactual if baseline date/reason/money differs."""
    if (replay['exit_date'] != trade.get('sim_closed')
            or replay['exit_reason'] != trade['exit_reason']
            or not math.isclose(replay['pnl'], trade['pnl'], abs_tol=.01, rel_tol=0)
            or not math.isclose(replay['fill'] or 0, trade['exit'], abs_tol=.00006, rel_tol=0)
            or replay['bars'] != trade['hold_bars']):
        raise ValueError(f"Baseline does not reproduce {trade['symbol']} {trade['sim_opened']}")


def audit_export(path: Path, tape_root: Path, *, txn_cost: float = .001,
                 slippage: float = 0) -> dict:
    """Match tape revisions using entry, recorded closes and the original exit."""
    source = path.read_bytes()
    export = json.loads(source)
    cutoff = datetime.fromisoformat(export['exported_at'])
    result = {'exported_at': export['exported_at'],
              'export_sha256': hashlib.sha256(source).hexdigest(),
              'engine_sha256': engine_hash(), 'txn_cost_per_side': txn_cost,
              'slippage_per_side': slippage, 'books': []}
    for book in export['books']:
        trades = book['closed_trades']
        if book.get('open_positions'):
            raise ValueError('Open baseline positions are not yet supported by this audit')
        baseline, scenarios, manifest, tapes = [], {
            'no_trail': [], 'no_target': [], 'requested_target': []}, {}, {}
        for trade in trades:
            symbol = trade['symbol']
            if symbol not in tapes:
                tapes[symbol] = []
                for tape_path in sorted((tape_root / book['market']).glob(f'{symbol}_*.json')):
                    raw = tape_path.read_bytes()
                    bars = [OHLCVCandle(**{**r, 'timestamp': datetime.fromtimestamp(r['timestamp'], timezone.utc)})
                            for r in json.loads(raw)['rows']]
                    tapes[symbol].append((tape_path, raw, bars))
            matches = []
            for tape_path, raw, bars in tapes[symbol]:
                try:
                    replay = replay_trade(trade, bars, cutoff, txn_cost=txn_cost, slippage=slippage)
                    verify_close(trade, replay)
                except ValueError:
                    continue
                relevant = sorted((b for b in bars if datetime.fromisoformat(trade['sim_opened']) <= b.timestamp <= cutoff),
                                  key=lambda b: b.timestamp)
                matches.append((relevant[-1].timestamp, len(relevant), tape_path, raw, relevant, replay))
            if not matches:
                raise ValueError(f'No verified tape for {symbol} {trade["sim_opened"]}')
            # Prefer the fullest observed coverage, and reject contradictory
            # OHLC revisions instead of choosing whichever improves results.
            matches.sort(key=lambda m: (m[0], m[1], str(m[2])))
            _, _, tape_path, raw, bars, replay = matches[-1]
            reference = {b.timestamp: (b.open, b.high, b.low, b.close) for b in bars}
            for match in matches[:-1]:
                if any(b.timestamp in reference and reference[b.timestamp] != (b.open, b.high, b.low, b.close)
                       for b in match[4]):
                    raise ValueError(f'Conflicting verified OHLC revisions for {symbol}')
            manifest[str(tape_path)] = hashlib.sha256(raw).hexdigest()
            baseline.append(replay)
            for name, rows in scenarios.items():
                rows.append(replay_trade(trade, bars, cutoff, txn_cost=txn_cost,
                                         slippage=slippage, scenario=name))
        ranges = [r['entry_range_r'] for r in baseline if r['entry_range_r'] is not None]
        result['books'].append({
            'market': book['market'], 'currency': book['currency'], 'baseline_verified': True,
            'summary': summarize(baseline),
            'by_side': {side: summarize([r for r in baseline if r['action'] == side])
                        for side in ('BUY', 'SELL')},
            'by_pattern': {name: summarize([r for r in baseline if r['pattern'] == name])
                           for name in sorted({r['pattern'] for r in baseline})},
            'trailing_armed_count': sum(r['trailing_armed'] for r in baseline),
            'same_session_exit_count': sum(r['same_session_exit'] for r in baseline),
            'ambiguous_exit_count': sum(bool(r['ambiguous_bars']) for r in baseline),
            'median_entry_range_r': median(ranges) if ranges else None,
            'baseline': baseline, 'tape_sha256': manifest,
            'scenarios': {name: {'summary': summarize(rows),
                                'realized_pnl': sum(r['realized_pnl'] for r in rows),
                                'open_liquidation_pnl': sum(r['open_liquidation_pnl'] for r in rows),
                                'trades': rows} for name, rows in scenarios.items()},
        })
    return result


def main() -> None:
    from utils.logger import set_console_level

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('export', type=Path)
    parser.add_argument('--tape-root', type=Path, default=Path('data/barcache/stream_tapes'))
    parser.add_argument('--txn-cost', type=float, default=.001)
    parser.add_argument('--slippage', type=float, default=0)
    args = parser.parse_args()
    for value in (args.txn_cost, args.slippage):
        if not math.isfinite(value) or not 0 <= value < 1:
            parser.error('Costs and slippage must be finite fractions in [0, 1)')
    set_console_level('ERROR')
    print(json.dumps(audit_export(args.export, args.tape_root,
                                 txn_cost=args.txn_cost, slippage=args.slippage), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
