"""Aggregate already frozen test folds without fitting or reusing test outcomes."""


def stitch_test_folds(results):
    """Accept service.result payloads; reject overlaps and open boundary trades."""
    if not results:
        raise ValueError('At least one test fold is required')
    for result in results:
        settings = result['inputs'].request.preset.settings
        if settings.mode != 'historical-stream' or settings.window is None:
            raise ValueError('Only historical-stream test folds may be stitched')
    if len(results) > 1 and any(r['inputs'].request.preset.settings.evaluation is not None
                                and r['inputs'].request.preset.settings.evaluation.purpose == 'final-holdout'
                                for r in results):
        raise ValueError('Report the reserved final holdout separately')
    ordered = sorted(results, key=lambda r: r['inputs'].request.preset.settings.window.start_date)
    last_end = None
    points = []
    pnl = 0.0
    fees = 0.0
    initial = ordered[0]['inputs'].request.preset.settings.execution.initial_capital
    market = ordered[0]['inputs'].request.preset.settings.market
    for result in ordered:
        settings = result['inputs'].request.preset.settings
        protocol = settings.evaluation
        if settings.mode != 'historical-stream' or protocol is None or protocol.purpose == 'research':
            raise ValueError('Only declared holdout folds may be stitched')
        if result['result'].metrics.open_position_count or settings.execution.end_policy != 'force-close':
            raise ValueError('Test folds must close all positions at their boundary')
        if settings.market != market or settings.execution.initial_capital != initial:
            raise ValueError('Fold markets and initial capital must match')
        if last_end is not None and settings.window.start_date <= last_end:
            raise ValueError('Test folds must not overlap')
        for point in result['equity']:
            if settings.window.start_date.isoformat() <= point['date'] <= settings.window.end_date.isoformat():
                points.append({'date': point['date'], 'equity': pnl+point['equity']})
        pnl += result['result'].metrics.net_pnl
        fees += result['result'].metrics.fees
        last_end = settings.window.end_date
    peak = initial
    drawdown = 0.0
    for point in points:
        peak = max(peak, point['equity'])
        drawdown = max(drawdown, peak-point['equity'])
    return {'account_model': 'independent-fixed-notional', 'holdout_status': 'operator-declared',
            'net_pnl': pnl, 'fees': fees, 'equity': points,
            'drawdown_pct_of_initial': 100*drawdown/initial,
            'run_ids': [r['result'].run_id for r in ordered]}
