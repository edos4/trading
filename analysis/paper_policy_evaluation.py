"""Compare complete frozen paper runs; never infer rejected or later price paths.

Run ``python -m analysis.paper_policy_evaluation baseline.json candidate.json
--plan plan.json --output comparison.json``. A plan records operator-declared
registration time, window, source/universe identities and intended controls.
Eligibility is evidence validation, not independent certification of profit.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path

ALLOWED_EXPERIMENTS = frozenset({
    'active_controls.kronos_entry', 'active_controls.kronos_exit', 'active_controls.dii_mode',
    'resolved_controls.target_max_r', 'resolved_controls.target_risk_basis',
    'resolved_controls.paper_v2_min_stop_atr', 'resolved_controls.disabled_patterns_v2',
    'resolved_controls.kronos_gate_min_agree', 'resolved_controls.kronos_exit_min_agree',
    'execution.kronos_vote_policy.entry_min_agree', 'execution.kronos_vote_policy.exit_min_agree',
})


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _flat(value: dict, prefix: str = '') -> dict:
    result = {}
    for key, item in value.items():
        name = f'{prefix}.{key}' if prefix else key
        if isinstance(item, dict):
            result.update(_flat(item, name))
        else:
            result[name] = item
    return result


def create_plan(baseline: dict, candidate: dict, *, start: str, end: str, changes: list[str],
                model_identity: str | None = None, registered_at: str | None = None) -> dict:
    """Freeze intended policies before a future window; no trading or inference."""
    stamp = registered_at or datetime.now(timezone.utc).isoformat()
    registered = datetime.fromisoformat(stamp)
    if registered.tzinfo is None or not registered.date() < date.fromisoformat(start) <= date.fromisoformat(end):
        raise ValueError('Register the plan before its evaluation window')
    if not changes or not set(changes) <= ALLOWED_EXPERIMENTS:
        raise ValueError('Declare supported strategy control changes')
    manifests = [(b.get('execution_context') or {}).get('run_manifest') or {} for b in (baseline, candidate)]
    if not manifests[0].get('source_sha256') or manifests[0].get('source_sha256') != manifests[1].get('source_sha256'):
        raise ValueError('Both planned arms require identical source manifests')
    universes = [m.get('entry_universe') or {} for m in manifests]
    if not universes[0].get('sha256') or universes[0] != universes[1]:
        raise ValueError('Both planned arms require the same entry universe')
    controls = [_flat({key: m.get(key, {}) for key in ('active_controls', 'resolved_controls', 'execution')})
                for m in manifests]
    fixed = [{key: value for key, value in control.items() if key not in changes} for control in controls]
    if fixed[0] != fixed[1] or any(key not in control for key in changes for control in controls):
        raise ValueError('Planned arms differ outside the declared controls')
    return {'schema_version': 1, 'registered_at': stamp, 'evaluation_start': start, 'evaluation_end': end,
            'allowed_changes': sorted(set(changes)), 'source_sha256': manifests[0]['source_sha256'],
            'entry_universe_sha256': universes[0]['sha256'], 'fixed_controls_sha256': _digest(fixed[0]),
            'evaluation_policy_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'model_identity': model_identity,
            'arms': {label: {key: control[key] for key in changes}
                     for label, control in zip(('baseline', 'candidate'), controls)}}


def model_holdout_evidence(book: dict, expected_identity: str | None = None) -> dict:
    """Assess all recorded entry/position forecasts, not just winning fills."""
    manifest = (book.get('execution_context') or {}).get('run_manifest') or {}
    active = manifest.get('active_controls') or {
        'kronos_entry': (book.get('settings') or {}).get('kronos_gate_mode', 'off'),
        'kronos_exit': (book.get('settings') or {}).get('kronos_exit_mode', 'off')}
    model_required = any(active.get(key, 'off') != 'off' for key in ('kronos_entry', 'kronos_exit'))
    evidence = []
    for row in book.get('signal_logs', []):
        if row.get('kronos_evidence'):
            evidence.append(row['kronos_evidence'])
    trades = book.get('closed_trades', []) + book.get('open_positions', [])
    for trade in trades:
        if trade.get('kronos_evidence'):
            evidence.append(trade['kronos_evidence'])
        history = trade.get('kronos_exit_history')
        if history is not None:
            evidence.extend(row['evidence'] for row in history if row.get('evidence'))
        elif trade.get('kronos_exit_evidence'):
            evidence.append(trade['kronos_exit_evidence'])
    unique = {_digest(row): row for row in evidence}.values()
    checked, ineligible = [], []
    for row in unique:
        # Retries with no forecast are not model votes, and neither is a symbol
        # whose own series is too short to model; retained run health still
        # determines whether the complete experiment finished.
        if row.get('failure_kind') in ('capacity', 'infrastructure_or_context', 'symbol_history'):
            continue
        provenance = row.get('model_provenance') or {}
        reasons = []
        if provenance.get('identity_verified') is not True:
            reasons.append('unverified checkpoint identity')
        if provenance.get('out_of_sample_eligible') is not True:
            reasons.append('not out-of-sample eligible')
        if expected_identity is not None and row.get('model_identity') != expected_identity:
            reasons.append('checkpoint differs from frozen plan')
        try:
            anchor = date.fromisoformat(row['anchor_date'])
            if not (date.fromisoformat(provenance['selection_data_end'])
                    < date.fromisoformat(provenance['eligible_from']) <= anchor):
                reasons.append('anchor precedes model selection boundary')
        except (KeyError, TypeError, ValueError):
            reasons.append('missing selection boundary')
        checked.append(row)
        if reasons:
            ineligible.append({'symbol': row.get('symbol'), 'anchor_date': row.get('anchor_date'),
                               'scope': row.get('scope'), 'reasons': reasons})
    return {'model_required': model_required, 'forecasts_checked': len(checked),
            'ineligible_forecasts': ineligible,
            'eligible': not ineligible and (not model_required or bool(checked)),
            'exit_history_complete': manifest.get('exit_journal_policy') == 'all-decisions-v1',
            'scope': 'declared selection boundaries and saved identities; not independent training certification'}


def compare_books(baseline: dict, candidate: dict, plan: dict) -> dict:
    """A strategy comparison requires full, independently executed account runs."""
    from analysis.paper_trade_review import replay_trade, signal_evidence, summarize
    from analysis.paper_cohorts import cohort_review

    errors = []
    if (plan.get('schema_version') != 1 or plan.get('evaluation_policy_sha256')
            != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()):
        errors.append('evaluation policy is missing or differs from the frozen plan')
    start = plan.get('evaluation_start')
    end = plan.get('evaluation_end')
    try:
        registered = datetime.fromisoformat(plan['registered_at'])
        if registered.tzinfo is None or not registered.date() < date.fromisoformat(start) <= date.fromisoformat(end):
            errors.append('plan must be registered before the evaluation window')
    except (KeyError, TypeError, ValueError):
        errors.append('missing or invalid preregistered evaluation window')
    allowed = set(plan.get('allowed_changes', []))
    if not allowed or not allowed <= ALLOWED_EXPERIMENTS:
        errors.append('plan must declare supported strategy controls; costs/funding cannot vary')
    runs = []
    manifests = []
    for label, book in (('baseline', baseline), ('candidate', candidate)):
        context = book.get('execution_context') or {}
        manifest = context.get('run_manifest') or {}
        manifests.append(manifest)
        stats = book.get('scan_stats') or {}
        trades = book.get('closed_trades', []) + book.get('open_positions', [])
        if not manifest or not manifest.get('source_sha256'):
            errors.append(f'{label}: missing source manifest')
        if manifest.get('source_sha256') != plan.get('source_sha256'):
            errors.append(f'{label}: source differs from frozen plan')
        if manifest.get('control_changes') != []:
            errors.append(f'{label}: controls changed during run or drift tracking is absent')
        if (manifest.get('execution') or {}).get('patterns_v2') is not True or any(
                t.get('provenance') != 'patternsv2' for t in trades):
            errors.append(f'{label}: comparison requires Patternsv2 entries')
        if book.get('account_model') != 'funded':
            errors.append(f'{label}: a funded portfolio run is required')
        if (book.get('running') or stats.get('scan_complete') is not True
                or stats.get('termination_reason') not in ('end_of_tape', 'stream_end_reached')
                or (context.get('execution_coverage') or {}).get('verified') is not True):
            errors.append(f'{label}: incomplete run or unresolved execution coverage')
        observations = manifest.get('observation_fingerprints') or {}
        if not observations or any(r.get('context_bars', 0) < 1 for r in observations.values()):
            errors.append(f'{label}: complete initial-context/feed fingerprints are required')
        if observations:
            first = min(r['first'][:10] for r in observations.values())
            last = max(r['last'][:10] for r in observations.values())
            if (first, last) != (start, end) or (context.get('sim_as_of') or '')[:10] != end:
                errors.append(f'{label}: observed tape differs from declared window')
        universe = manifest.get('entry_universe') or {}
        if not universe.get('symbols') or universe.get('sha256') != plan.get('entry_universe_sha256'):
            errors.append(f'{label}: entry universe differs from frozen plan')
        elif hashlib.sha256(json.dumps(sorted(set(universe['symbols']))).encode()).hexdigest() != universe['sha256']:
            errors.append(f'{label}: entry universe identity does not reproduce')
        controls = _flat({key: manifest.get(key, {}) for key in ('active_controls', 'resolved_controls', 'execution')})
        fixed = {key: value for key, value in controls.items() if key not in allowed}
        if _digest(fixed) != plan.get('fixed_controls_sha256'):
            errors.append(f'{label}: fixed controls differ from frozen plan')
        for key in allowed:
            arm = (plan.get('arms') or {}).get(label) or {}
            if key not in arm or controls.get(key) != arm[key]:
                errors.append(f'{label}: {key} differs from preregistered arm')
        model = model_holdout_evidence(book, plan.get('model_identity'))
        if model['model_required'] and (not plan.get('model_identity') or not model['eligible']
                                        or not model['exit_history_complete']):
            errors.append(f'{label}: model holdout or complete decision history is unverified')
        journal = signal_evidence(book)
        if journal['warnings']:
            errors.append(f'{label}: incomplete journal identity linkage')
        if journal['journal']['run_ids'] != [manifest.get('run_id')]:
            errors.append(f'{label}: journal is not tied to this run manifest')
        observed = {row.get('signal_id') for row in book.get('signal_logs', [])
                    if row.get('status') != 'exited' and row.get('signal_id')}
        if stats.get('run_totals', {}).get('patterns_found') != len(observed):
            errors.append(f'{label}: journal does not cover every detected opportunity')
        if not observed:
            errors.append(f'{label}: no observed entry opportunities')
        try:
            rows = [replay_trade(trade) for trade in trades]
            costs = manifest.get('execution') or {}
            if any(not math.isclose(row['txn_cost_pct_per_side'], costs.get('txn_cost_pct', -1), abs_tol=1e-10)
                   or not math.isclose(row['slippage_pct_per_side'], costs.get('slippage_pct', -1), abs_tol=1e-10)
                   for row in rows):
                raise ValueError('actual fill costs differ from the frozen execution policy')
            closed = [r for r in rows if r['exit_reason'] != 'open']
            if abs(sum(r['pnl'] for r in closed)-book['metrics']['realized_pnl_dollars']) > .02:
                raise ValueError('realized total does not reconcile')
            outcome = {'closed': summarize(closed), 'liquidation_pnl': sum(r['pnl'] for r in rows),
                       'open_positions': sum(r['exit_reason'] == 'open' for r in rows),
                       'cohorts': cohort_review(book, rows)}
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f'{label}: saved execution does not reproduce: {exc}')
            outcome = None
        runs.append({'arm': label, 'model_evidence': model, 'outcome': outcome})
    if manifests[0].get('observation_fingerprints') != manifests[1].get('observation_fingerprints'):
        errors.append('runs use different price histories or observed coverage')
    if baseline.get('market') != candidate.get('market') or baseline.get('currency') != candidate.get('currency'):
        errors.append('market/currency differ; never combine books')
    eligible = not errors
    return {'eligible': eligible, 'issues': errors, 'runs': runs,
            'liquidation_pnl_difference': runs[1]['outcome']['liquidation_pnl']-runs[0]['outcome']['liquidation_pnl']
            if eligible else None,
            'scope': 'complete saved funded runs; registration is operator-declared, not independent holdout certification'}


def main() -> None:
    from utils.logger import set_console_level
    set_console_level('ERROR')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--register', action='store_true', help='Write a plan for a future window instead of scoring')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--change', action='append', choices=sorted(ALLOWED_EXPERIMENTS), default=[])
    parser.add_argument('--model-identity')
    parser.add_argument('--market', default='us', choices=('us', 'ph'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    books = [next(b for b in json.loads(path.read_text())['books'] if b['market'] == args.market)
             for path in (args.baseline, args.candidate)]
    if args.register:
        if not args.start or not args.end:
            parser.error('--register requires --start and --end')
        result = create_plan(*books, start=args.start, end=args.end, changes=args.change,
                             model_identity=args.model_identity)
        if args.plan.exists():
            parser.error('Plan already exists; preserve the registered experiment')
        args.plan.write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
        return
    result = compare_books(*books, json.loads(args.plan.read_text()))
    output = json.dumps(result, indent=2, allow_nan=False)+'\n'
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
