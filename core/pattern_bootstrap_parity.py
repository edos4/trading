"""Clean spawned-worker file/DB parity for trusted imports, not AI execution."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import importlib
import json
import multiprocessing
from pathlib import Path
import tempfile

from core.pattern_edit_store import EditError, canonical, digest

CASES = {
    'pattern_002_double_top': 'tests/fixtures/barcache/us/TXN.json',
    'pattern_003_double_bottom': 'tests/fixtures/barcache/us/ADBE.json',
    'pattern_004_rounding_bottom': 'tests/fixtures/barcache/us/ON.json',
    'pattern_005_rounding_top': 'tests/fixtures/pattern_versions/rounding_top_control.csv',
    'pattern_006_upward_channel': 'tests/fixtures/barcache/us/C.json',
    'pattern_010_pennant': 'tests/fixtures/pattern_versions/pennant_control.csv',
}
CONFIG = dict(market='us', session_tz='America/New_York', position_notional=10000.,
              txn_cost_pct=0.001, lot_round=False)


def normalize(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items() if k not in ('pattern_version_id', 'signal_id', 'trade_id')}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, 'item'):
        return value.item()
    return value


def _worker(mode, root, files, version, dsn, schema, fixture, negative):
    import io
    import sys
    import pandas as pd
    from core.pattern_bootstrap import metadata
    from core.pattern_loader import VersionPattern
    from core.pattern_edit_store import EditStore
    from core.backtester import _core_backtest_symbol
    from core.pattern_provenance import rules
    from data.tv_client import OHLCVCandle
    from patterns.base_pattern import BasePattern
    from patterns import _dedup

    with tempfile.TemporaryDirectory(prefix='pattern-import-parity-') as directory:
        if mode == 'file':
            # Standard Python imports from a disposable exact-byte snapshot.
            # Retain the trusted interface and engine dedup identity.
            import patterns
            for name in list(sys.modules):
                if name.startswith('patterns.') and name not in ('patterns.base_pattern', 'patterns._dedup'):
                    del sys.modules[name]
            for path, data in files.items():
                if path.startswith('patterns/'):
                    target = Path(directory) / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            patterns.__path__ = [str(Path(directory) / 'patterns')]
            for name in list(vars(patterns)):
                if name.startswith('_') and name not in ('_dedup',) and not name.startswith('__'):
                    delattr(patterns, name)
            module = importlib.import_module(version['source_path'][:-3].replace('/', '.'))
            cls = next(c for c in vars(module).values() if isinstance(c, type) and c is not BasePattern
                       and issubclass(c, BasePattern) and c.__module__ == module.__name__)
            pattern = cls()
            modules = {n: m for n, m in sys.modules.items() if n.startswith('patterns.')}
        else:
            pattern = VersionPattern(version['version_id'], EditStore(root, dsn=dsn, schema=schema))
            modules = pattern._baseline_modules
        observed = metadata(pattern)
        if pattern.skipped:
            return dict(metadata=observed, excluded=True)
        # Frozen empty earnings calendar, represented as a nonempty map so the
        # existing helper never falls through to EDGAR. No detector gates mocked.
        if 'patterns._rules' in modules:
            modules['patterns._rules']._EARNINGS_CACHE = {'FIXTURE': []}
        if fixture.lstrip().startswith(b'{'):
            rows = json.loads(fixture)['bars']
            bars = [OHLCVCandle(open=r['o'], high=r['h'], low=r['l'], close=r['c'], volume=r['v'],
                               timestamp=pd.Timestamp(r['t'], unit='s', tz='UTC').to_pydatetime()) for r in rows]
        else:
            frame = pd.read_csv(io.BytesIO(fixture), index_col='date', parse_dates=True)
            bars = [OHLCVCandle(**r.to_dict(), timestamp=t.to_pydatetime()) for t, r in frame.iterrows()]
        if negative:
            bars = [OHLCVCandle(open=100, high=101, low=99, close=100, volume=1000000,
                               timestamp=b.timestamp) for b in bars]
        signals = []
        original = pattern.analyze

        def analyze(snapshot, store):
            signal = original(snapshot, store)
            if signal:
                # Apply identical provenance decoration to the ordinary file load.
                signal.provenance = 'versioned'
                signal.requested_rules = rules(signal, max_open_per_symbol=getattr(pattern, 'MAX_OPEN_PER_SYMBOL', None),
                                               entry_mode='signal_close')
                signals.append(normalize(signal))
            return signal
        pattern.analyze = analyze
        result = _core_backtest_symbol('FIXTURE', '1d', bars, [pattern], CONFIG)
        trades, count, blocked, filtered = result
        return dict(metadata=observed, signals=signals, trades=normalize(trades), signals_count=count,
                    blocked=normalize(blocked), filtered=normalize(filtered),
                    accounting=dict(total_pnl_usd=sum(t.pnl_usd for t in trades),
                                    total_pnl_pct=sum(t.pnl_pct for t in trades)))


def _send_worker_result(pipe, args):
    try:
        pipe.send((True, _worker(*args)))
    except Exception as exc:
        pipe.send((False, str(exc)))
    finally:
        pipe.close()


def clean_worker(args, timeout=120):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_send_worker_result, args=(child, args))
    process.start()
    child.close()
    try:
        if not parent.poll(timeout):
            raise EditError('Bootstrap parity worker timed out')
        try:
            ok, result = parent.recv()
        except EOFError:
            raise EditError('Bootstrap parity worker exited without a result') from None
        if not ok:
            raise EditError('Bootstrap parity worker failed: ' + result)
        return result
    finally:
        parent.close()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join()
        process.close()


def verify_parity(store, version, snapshot):
    skipped = version['metadata']['skipped']
    fixture_path = CASES.get(version['pattern_id'])
    if not skipped and fixture_path is None:
        raise EditError('Missing behavioral parity fixture')
    fixture = b'' if skipped else (Path(__file__).resolve().parents[1] / fixture_path).read_bytes()
    evidence = dict(version_id=version['version_id'], fixture=fixture_path,
                    fixture_blob=store.blob(fixture, 'application/octet-stream'),
                    config=CONFIG, earnings={'FIXTURE': []}, tolerance=0, cases=[])
    for negative in ([False] if skipped else [False, True]):
        outputs = []
        for mode in ('file', 'database'):
            # A fresh process for every side/case prevents cached module/dedup leakage.
            outputs.append(clean_worker((mode, str(store.root), snapshot.files, version,
                                         store._dsn, store.schema, fixture, negative)))
        if outputs[0] != outputs[1]:
            raise EditError('File/database parity mismatch: ' + version['pattern_id'] + ' fields=' +
                            ','.join(k for k in outputs[0] if outputs[0][k] != outputs[1][k]))
        if outputs[0]['metadata'] != version['metadata']:
            raise EditError('Imported metadata mismatch')
        if not skipped:
            count = len(outputs[0]['signals'])
            if (negative and count) or (not negative and (not count or not outputs[0]['trades'])):
                raise EditError('Fixture does not exercise expected behavior: ' + version['pattern_id'])
        evidence['cases'].append(dict(negative=negative, result=outputs[0],
                                     result_sha256=digest(canonical(outputs[0]))))
    return evidence
