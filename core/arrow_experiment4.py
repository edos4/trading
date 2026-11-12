"""Frozen Arrow Experiment 4 directions and bounded actual-close collection."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from core.kronos_experiment3 import NY, ROOT, ny_now, positive, provider_bars
from core.kronos_weekly import atomic_text, write_json
from core.market import last_closed_session_date
from data.db import get_conn
from learn.kronos_weekly_dataset import is_session
from utils.logger import log

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS kronos_experiment4;
CREATE TABLE IF NOT EXISTS kronos_experiment4.run (
    id INTEGER PRIMARY KEY CHECK (id=1), metadata JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS kronos_experiment4.forecasts (
    symbol TEXT PRIMARY KEY, rank INTEGER UNIQUE NOT NULL, payload JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS kronos_experiment4.actuals (
    symbol TEXT NOT NULL REFERENCES kronos_experiment4.forecasts(symbol),
    session_date DATE NOT NULL,
    close DOUBLE PRECISION NOT NULL CHECK (close > 0 AND close < 'Infinity'::float8),
    source TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(symbol,session_date)
);
"""
HORIZONS = (1, 3)
WORKER = ROOT / 'scripts/arrow_experiment4_forecast.py'


def adopt_arrow_experiment3() -> dict:
    """Relabel the existing Arrow run transactionally, without regenerating it."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('kronos_experiment3_arrow'))")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('kronos_experiment4'))")
            cur.execute("SELECT to_regnamespace('kronos_experiment3_arrow'), to_regnamespace('kronos_experiment4')")
            source, target = cur.fetchone()
            if source is not None:
                if target is not None:
                    raise ValueError('Experiment 4 already exists; refusing to replace either run')
                cur.execute('SELECT metadata FROM kronos_experiment3_arrow.run WHERE id=1')
                saved = cur.fetchone()
                if saved is None or saved[0].get('engine') != 'Arrow':
                    raise ValueError('The source experiment is not a saved Arrow run')
                cur.execute('SELECT payload FROM kronos_experiment3_arrow.forecasts ORDER BY rank')
                rows = [row[0] for row in cur.fetchall()]
                if len(rows) != 50 or any({p['horizon'] for p in row['forecasts']} != set(HORIZONS) for row in rows):
                    raise ValueError('Expected the complete frozen Arrow universe')
                cur.execute('ALTER SCHEMA kronos_experiment3_arrow RENAME TO kronos_experiment4')
            elif target is None:
                raise ValueError('No saved Arrow run is available to adopt')
    return read_experiment()


def next_session(day: date) -> date:
    day += timedelta(days=1)
    while not is_session(day, 'us'):
        day += timedelta(days=1)
    return day


def schedule(as_of: date) -> dict:
    if not is_session(as_of, 'us'):
        raise ValueError('Experiment anchor must be a completed US session')
    targets, day = {}, as_of
    for horizon in range(1, 4):
        day = next_session(day)
        if horizon in HORIZONS:
            targets[str(horizon)] = day.isoformat()
    deadline = datetime.combine(next_session(day), time(9, 30), NY)
    return {'as_of': as_of.isoformat(), 'horizons': list(HORIZONS), 'target_dates': targets,
            'deadline': deadline.isoformat()}


def validate_universe(snapshot: dict, as_of: date) -> None:
    rows = snapshot['stocks']
    captured = ny_now(datetime.fromisoformat(snapshot['captured_at']))
    first_open = datetime.combine(next_session(as_of), time(9, 30), NY)
    if (snapshot['as_of'] != as_of.isoformat() or len(rows) != 50
            or len({r['symbol'] for r in rows}) != 50
            or not datetime.combine(as_of, time(16, 30), NY) <= captured < first_open):
        raise ValueError('Expected 50 stocks captured after the anchor close and before the next open')
    for index, row in enumerate(rows):
        if (row['rank'] != index + 1 or row['exchange'] != 'NASDAQ' or row['type'] not in ('stock', 'dr')
                or not positive(row['close']) or not positive(row['dollar_volume'])
                or (index and row['dollar_volume'] > rows[index - 1]['dollar_volume'])):
            raise ValueError('Invalid frozen NASDAQ ranking')


def capture_universe(as_of: date, now: datetime | None = None) -> dict:
    local = ny_now(now)
    if not datetime.combine(as_of, time(16, 30), NY) <= local < datetime.combine(next_session(as_of), time(9, 30), NY):
        raise ValueError('Capture the completed session before the next US session opens')
    from tradingview_screener import Query, col
    from data.tv_client import _get_scanner_data
    query = (Query().set_markets('america')
             .select('name', 'description', 'exchange', 'type', 'close', 'volume', 'Value.Traded')
             .where(col('exchange') == 'NASDAQ', col('type').isin(['stock', 'dr']))
             .order_by('Value.Traded', ascending=False).limit(50))
    total, frame = _get_scanner_data(query)
    rows = [{'symbol': str(row['name']), 'rank': i + 1, 'name': str(row['description']),
             'exchange': str(row['exchange']), 'type': str(row['type']), 'close': float(row['close']),
             'volume': int(row['volume']), 'dollar_volume': float(row['Value.Traded'])}
            for i, (_, row) in enumerate(frame.iterrows())]
    result = {'as_of': as_of.isoformat(), 'captured_at': local.isoformat(), 'stocks': rows,
              'source': 'TradingView completed regular session', 'eligible_count': total,
              'ranking': 'NASDAQ stock/dr Value.Traded descending'}
    validate_universe(result, as_of)
    return result


def load_inputs(snapshot: dict, as_of: date) -> list[dict]:
    """Freeze exact dated contexts, never importing or rewriting shared bars."""
    inputs = []
    with get_conn() as conn:
        for stock in snapshot['stocks']:
            with conn.cursor() as cur:
                cur.execute('SELECT DISTINCT ON (bar_date) bar_date,open,high,low,close,volume '
                            'FROM daily_bars WHERE symbol=%s AND bar_date<=%s '
                            'ORDER BY bar_date DESC,ts DESC LIMIT 400', (stock['symbol'], as_of))
                source_rows = cur.fetchall()[::-1]
            bars = [dict(zip(('time', 'open', 'high', 'low', 'close', 'volume'),
                             [row[0].isoformat(), *map(float, row[1:])])) for row in source_rows]
            source = 'daily_bars'
            if not bars or bars[-1]['time'] != as_of.isoformat():
                bars = [row for row in provider_bars(stock['symbol']) if row['time'] <= as_of.isoformat()][-400:]
                source = 'Yahoo chart (missing database history)'
            if (not bars or bars[-1]['time'] != as_of.isoformat()
                    or not math.isclose(bars[-1]['close'], stock['close'], rel_tol=.0001)):
                raise ValueError(f"{stock['symbol']}: missing or mismatched historical anchor")
            if not all(math.isfinite(row[key]) for row in bars for key in ('open','high','low','close','volume')):
                raise ValueError(f"{stock['symbol']}: nonfinite historical input")
            inputs.append({**stock, 'bars': bars, 'input_source': source})
    return inputs


def forecast_inputs(inputs: list[dict], plan: dict, model_dir: Path) -> dict:
    """Run CPU Arrow on frozen inputs; no GPU and no later-history fetches."""
    with tempfile.TemporaryDirectory(prefix='arrow-experiment4-') as folder:
        input_path, output_path = Path(folder) / 'input.json', Path(folder) / 'output.json'
        write_json(input_path, {'inputs': inputs, 'schedule': plan})
        process = subprocess.run([sys.executable, str(WORKER), '--input', str(input_path),
                                  '--out', str(output_path), '--model-dir', str(model_dir)],
                                 cwd=ROOT, text=True, capture_output=True, timeout=300)
        if process.returncode or not output_path.exists():
            log.error(f'Arrow Experiment 4 worker failed: {process.stderr[-1000:]}')
            raise RuntimeError('Arrow could not generate the frozen experiment')
        return json.loads(output_path.read_text())


def validate_predictions(payload: dict, inputs: list[dict], plan: dict) -> None:
    rows = payload.get('results', [])
    if len(rows) != 50 or {r['symbol'] for r in rows} != {r['symbol'] for r in inputs}:
        raise ValueError('Expected Arrow predictions or explicit abstentions for all 50 stocks')
    anchors = {row['symbol']: row['bars'][-1]['close'] for row in inputs}
    usable = {horizon: 0 for horizon in HORIZONS}
    for row in rows:
        if (row['anchor']['time'] != plan['as_of'] or row['anchor']['value'] != anchors[row['symbol']]
                or any(point['time'] > plan['as_of'] for point in row['history'])):
            raise ValueError('Wrong or future-contaminated Arrow context')
        if len(row['forecasts']) != 2 or {p['horizon'] for p in row['forecasts']} != set(HORIZONS):
            raise ValueError('Arrow must use its actual one- and three-session models')
        for prediction in row['forecasts']:
            if prediction['date'] != plan['target_dates'][str(prediction['horizon'])]:
                raise ValueError('Arrow forecast target date differs')
            if prediction['abstained']:
                if prediction['direction'] is not None or not prediction.get('reasons'):
                    raise ValueError('Invalid Arrow abstention')
            else:
                probabilities = prediction['probabilities']
                if (prediction['direction'] not in ('up','down','flat')
                        or set(probabilities) != {'up','down','flat'}
                        or not all(isinstance(p, (int,float)) and math.isfinite(p) and 0 <= p <= 1 for p in probabilities.values())
                        or not math.isclose(sum(probabilities.values()), 1., abs_tol=1e-6)
                        or not positive(prediction['flat_band_return'])):
                    raise ValueError('Invalid Arrow direction, scores or flat band')
                if prediction['direction'] != max(('up','down','flat'), key=lambda label: probabilities[label]):
                    raise ValueError('Arrow direction differs from its model scores')
                usable[prediction['horizon']] += 1
    if not all(usable.values()):
        raise ValueError('No usable Arrow predictions for a required horizon')


def start(now: datetime | None = None, *, reset: bool = False, as_of: date | None = None,
          universe_file: Path | None = None, model_dir: Path | None = None,
          archive_dir: Path | None = None) -> dict:
    """Prepare completely before atomically replacing only this Arrow experiment."""
    from psycopg.types.json import Jsonb
    from core.arrow import DEFAULT_ARROW
    existing = read_experiment(now)
    if existing['status'] != 'not_started' and not reset:
        return existing
    as_of = as_of or last_closed_session_date('us', now)
    plan = schedule(as_of)
    first_open = datetime.combine(next_session(as_of), time(9, 30), NY)
    if ny_now(now) >= first_open:
        raise ValueError('Forecast creation closed before the first target session opened')
    snapshot = json.loads(Path(universe_file).read_text()) if universe_file else capture_universe(as_of, now)
    validate_universe(snapshot, as_of)
    inputs = load_inputs(snapshot, as_of)
    payload = forecast_inputs(inputs, plan, model_dir or DEFAULT_ARROW)
    validate_predictions(payload, inputs, plan)
    generated = ny_now()
    if generated >= first_open:
        raise ValueError('Arrow forecasts finished after the target session opened; nothing saved')
    metadata = {**plan, 'engine': 'Arrow', 'schema_version': 1,
                'run_id': f"{as_of.isoformat()}-{uuid.uuid4().hex[:12]}",
                'generated_at': generated.isoformat(), 'universe': snapshot,
                'model': payload['model'], 'settings': payload['settings'],
                'input_sha256': hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()}
    by_symbol = {row['symbol']: row for row in inputs}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('kronos_experiment4'))")
            cur.execute('SELECT metadata FROM kronos_experiment4.run WHERE id=1')
            previous = cur.fetchone()
            if previous and not reset:
                return read_experiment(now)
            if previous:
                cur.execute('SELECT payload FROM kronos_experiment4.forecasts ORDER BY rank')
                predictions = [r[0] for r in cur.fetchall()]
                cur.execute('SELECT symbol,session_date,close,source,observed_at FROM kronos_experiment4.actuals')
                actuals = [[str(value) if isinstance(value, (date,datetime)) else value for value in row] for row in cur.fetchall()]
                archive = archive_dir or ROOT / 'runs' / 'experiment4-archives'
                previous_id = previous[0]['run_id']
                if Path(previous_id).name != previous_id or previous_id in ('.','..'):
                    raise ValueError('Invalid previous experiment identifier')
                write_json(Path(archive) / f'{previous_id}.json',
                           {'metadata': previous[0], 'forecasts': predictions, 'actuals': actuals})
                for table in ('actuals','forecasts','run'):
                    cur.execute(f'DELETE FROM kronos_experiment4.{table}')
            cur.execute('INSERT INTO kronos_experiment4.run (id,metadata) VALUES (1,%s)', (Jsonb(metadata),))
            for row in payload['results']:
                item = by_symbol[row['symbol']]
                row.update(rank=item['rank'], dollar_volume=item['dollar_volume'], input=item)
                cur.execute('INSERT INTO kronos_experiment4.forecasts (symbol,rank,payload) VALUES (%s,%s,%s)',
                            (row['symbol'], row['rank'], Jsonb(row)))
    return read_experiment(now)


def direction_results(row: dict) -> list[dict]:
    from core.kronos_confirm import classify
    actuals = {point['time']: point['value'] for point in row.get('actuals', [])}
    results = []
    for prediction in row['forecasts']:
        close = actuals.get(prediction['date'])
        band = prediction.get('flat_band_return')
        actual = classify(close / row['anchor']['value'] - 1, band * 100) if close is not None and positive(band) else None
        correct = prediction['direction'] == actual if actual is not None and not prediction['abstained'] else None
        results.append({'horizon': prediction['horizon'], 'date': prediction['date'], 'actual': actual,
                        'predicted': prediction['direction'], 'correct': correct, 'abstained': prediction['abstained']})
    return results


def score(rows: list[dict], metadata: dict) -> dict:
    import numpy as np
    from core.kronos_direction import classification_metrics
    from learn.kronos_direction_dataset import LABELS
    output = []
    for horizon in HORIZONS:
        target = metadata['target_dates'][str(horizon)]
        observed, abstained, actual, predicted = 0, 0, [], []
        for row in rows:
            direction = next(r for r in direction_results(row) if r['horizon'] == horizon)
            observed += int(any(p['time'] == target for p in row.get('actuals', [])))
            abstained += int(direction['abstained'])
            if direction['correct'] is not None:
                actual.append(LABELS.index(direction['actual']))
                predicted.append(LABELS.index(direction['predicted']))
        metrics = classification_metrics(np.asarray(actual), np.asarray(predicted)) if actual else None
        correct = sum(a == p for a, p in zip(actual, predicted))
        output.append({'horizon': horizon, 'date': target, 'expected': len(rows), 'observed': observed,
                       'count': len(actual), 'correct': correct, 'incorrect': len(actual) - correct,
                       'abstained': abstained, 'pending': len(rows) - observed,
                       'accuracy_pct': metrics['accuracy'] * 100 if metrics else None,
                       'balanced_accuracy_pct': metrics['balanced_accuracy'] * 100 if metrics else None,
                       'classes': metrics['classes'] if metrics else None})
    return {'arrow': output}


def read_experiment(now: datetime | None = None) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cur.execute("SELECT to_regclass('kronos_experiment4.run')")
            if cur.fetchone()[0] is None:
                return {'status': 'not_started', 'engine': 'Arrow', 'results': []}
            cur.execute('SELECT metadata FROM kronos_experiment4.run WHERE id=1')
            saved = cur.fetchone()
            if saved is None:
                return {'status': 'not_started', 'engine': 'Arrow', 'results': []}
            metadata = saved[0]
            cur.execute('SELECT payload FROM kronos_experiment4.forecasts ORDER BY rank')
            forecasts = cur.fetchall()
            cur.execute('SELECT symbol,session_date,close,source,observed_at FROM kronos_experiment4.actuals ORDER BY symbol,session_date')
            actuals = cur.fetchall()
    by_symbol = {}
    for symbol, day, close, source, observed in actuals:
        by_symbol.setdefault(symbol, []).append({'time': day.isoformat(), 'value': close,
                                                 'source': source, 'observed_at': observed.isoformat()})
    rows = []
    for (payload,) in forecasts:
        row = {key: value for key, value in payload.items() if key != 'input'}
        row['actuals'] = by_symbol.get(row['symbol'], [])
        row['direction_results'] = direction_results(row)
        rows.append(row)
    expected = len(rows) * len(HORIZONS)
    status = 'complete' if expected and len(actuals) == expected else 'expired_incomplete' if ny_now(now) >= ny_now(datetime.fromisoformat(metadata['deadline'])) else 'collecting'
    scores = score(rows, metadata)
    comparison = read_comparison(metadata, rows)
    return {'retrospective_comparison': comparison, 'status': status, 'engine': 'Arrow', 'metadata': metadata, 'results': rows,
            'actual_count': len(actuals), 'expected_actual_count': expected, 'scores': scores,
            'direction_report': scores['arrow']}


def read_comparison(metadata: dict, rows: list[dict]) -> dict | None:
    """Join an explicitly saved retrospective revision to current observations."""
    path = ROOT / 'runs' / 'experiment4-comparisons' / (metadata['run_id'] + '.json')
    if not path.exists():
        return None
    saved = json.loads(path.read_text())
    if saved['run_id'] != metadata['run_id']:
        raise ValueError('Retrospective comparison belongs to another run')
    from copy import deepcopy
    revised = deepcopy(rows)
    for row in revised:
        prediction = saved['predictions'][row['symbol']]
        row['forecasts'] = prediction
        row['direction_results'] = direction_results(row)
    return {**saved, 'results': revised, 'direction_report': score(revised, metadata)['arrow'],
            'label': 'Retrospective comparison — not official forecast accuracy'}


def due_targets(metadata: dict, now: datetime | None = None) -> list[date]:
    local = ny_now(now)
    if local >= ny_now(datetime.fromisoformat(metadata['deadline'])):
        return []
    return [date.fromisoformat(value) for value in metadata['target_dates'].values()
            if local >= datetime.combine(date.fromisoformat(value), time(16, 30), NY)]


def collect(now: datetime | None = None) -> dict:
    """Only store due closed-session actuals, never recompute Arrow predictions."""
    experiment = read_experiment(now)
    if experiment['status'] != 'collecting':
        return experiment
    due = {day.isoformat() for day in due_targets(experiment['metadata'], now)}
    missing = {row['symbol']: due - {p['time'] for p in row['actuals']} for row in experiment['results']}
    missing = {symbol: dates for symbol, dates in missing.items() if dates}
    if not missing:
        return experiment
    fetched = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for symbol, dates in missing.items():
                cur.execute('SELECT DISTINCT ON (bar_date) bar_date,close FROM daily_bars '
                            'WHERE symbol=%s AND bar_date=ANY(%s::date[]) ORDER BY bar_date,ts DESC',
                            (symbol, sorted(dates)))
                fetched.extend((symbol, day.isoformat(), float(close), 'daily_bars') for day, close in cur.fetchall() if positive(float(close)))
    found = {(symbol, day) for symbol, day, _, _ in fetched}
    need_provider = {symbol: dates - {day for s, day in found if s == symbol} for symbol, dates in missing.items()}
    need_provider = {symbol: dates for symbol, dates in need_provider.items() if dates}
    def fetch(symbol: str) -> tuple[str, list[dict]]:
        try:
            return symbol, provider_bars(symbol)
        except Exception as exc:
            log.warning(f'Arrow Experiment 4 | actual fetch failed for {symbol}: {exc}')
            return symbol, []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for symbol, bars in pool.map(fetch, need_provider):
            fetched.extend((symbol, bar['time'], bar['close'], 'Yahoo chart regular-session close')
                           for bar in bars if bar['time'] in need_provider[symbol] and positive(bar['close']))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('kronos_experiment4'))")
            cur.execute('SELECT metadata FROM kronos_experiment4.run WHERE id=1')
            current = cur.fetchone()
            if current is not None and current[0]['run_id'] == experiment['metadata']['run_id']:
                for symbol, day, close, source in fetched:
                    cur.execute('INSERT INTO kronos_experiment4.actuals (symbol,session_date,close,source) '
                                'VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', (symbol, day, close, source))
    return read_experiment(now)


def render_direction_report(experiment: dict) -> str:
    lines = ['# Experiment 4 — Arrow direction accuracy', '']
    if experiment['status'] == 'not_started':
        return '\n'.join([*lines, 'No saved Arrow forecasts yet.', ''])
    metadata = experiment['metadata']
    lines += [f"Anchor: {metadata['as_of']}. Model: Arrow {metadata['model']['version']}.",
              'Directions use the frozen ATR-based up/down/flat band. Abstentions are excluded from accuracy.', '',
              '| US session | Horizon | Observed | Scored | Correct | Incorrect | Abstained | Pending | Accuracy |',
              '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for row in experiment['direction_report']:
        accuracy = f"{row['accuracy_pct']:.1f}%" if row['accuracy_pct'] is not None else 'Pending'
        lines.append(f"| {row['date']} | +{row['horizon']}D | {row['observed']}/{row['expected']} | {row['count']} | "
                     f"{row['correct']} | {row['incorrect']} | {row['abstained']} | {row['pending']} | {accuracy} |")
    comparison = experiment.get('retrospective_comparison')
    if comparison:
        lines += ['', '## Retrospective comparison — not official forecast accuracy', '',
                  'New Flat rule applied after outcomes were known. Original accuracy above stays official.', '']
        for row in comparison['direction_report']:
            value = f"{row['correct']}/{row['count']} ({row['accuracy_pct']:.1f}%)" if row['count'] else 'Pending'
            lines.append(f"- +{row['horizon']}D: {value}")
    lines += ['', 'Arrow predicts direction, not a price path. Model scores are not calibrated success rates.',
              f"Automatic collection ends at {metadata['deadline']}. Forecasts stay frozen.", '']
    return '\n'.join(lines)


def save_direction_report(experiment: dict, path: Path | None = None) -> Path:
    path = path or ROOT / 'logs/kronos_experiment4_direction_report.md'
    content = render_direction_report(experiment)
    if not path.exists() or path.read_text() != content:
        atomic_text(path, content)
    return path
