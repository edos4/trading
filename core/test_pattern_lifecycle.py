"""P04 lifecycle, consumer pinning and PostgreSQL-only execution gates."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from psycopg.types.json import Jsonb

from core.pattern_edit_store import Conflict, EditError, uid
from core.pattern_versions import PatternVersions
from core.pattern_loader import discover, VersionPattern

PATTERN = 'pattern_002_double_top'


def candidate(store, base, *, validated=True, backtested=True):
    version = deepcopy(store.get('versions', base))
    version.update(version_id=uid(), parent_version_id=base, provenance='generated', import_batch_id=None)
    version['files'][version['source_path']] = store.blob(
        store.read_blob(version['files'][version['source_path']]) + b'\n# candidate\n', 'text/plain')
    version = store.insert_version(version)
    vid = version['version_id']
    with store.transaction() as con:
        if validated:
            report = dict(report_id=uid(), version_id=vid, status='passed')
            store.insert_report(report, con)
            con.execute("UPDATE version_lifecycle SET validation='passed',validation_report_id=%s WHERE version=%s",
                        (report['report_id'], vid))
        if backtested:
            job, run = uid(), uid()
            con.execute("INSERT INTO jobs(id,kind,idempotency_key,request_sha256,payload,state) VALUES(%s,'backtest',%s,%s,%s,'completed')",
                        (job, job, '0'*64, Jsonb({})))
            con.execute('INSERT INTO backtest_runs(id,job,inputs_sha256,payload) VALUES(%s,%s,%s,%s)',
                        (run, job, '0'*64, Jsonb(dict(run_id=run))))
            con.execute('INSERT INTO run_versions(run,pattern,version) VALUES(%s,%s,%s)', (run, PATTERN, vid))
            con.execute('INSERT INTO results(id,run,payload) VALUES(%s,%s,%s)', (uid(), run, Jsonb(dict(trades=[]))))
            con.execute('UPDATE version_lifecycle SET successful_run_id=%s WHERE version=%s', (run, vid))
    return vid


def request(service, vid, **extra):
    pattern = next(p for p in service.catalog() if p['id'] == PATTERN)
    return dict(pattern_id=PATTERN, version_id=vid, expected_generation=pattern['generation'], idempotency_key=uid(), **extra)


def test_lifecycle_evidence_archive_and_history(published_pattern_catalog):
    store = published_pattern_catalog
    service = PatternVersions(store)
    pinned = service.resolve()
    base = pinned[PATTERN]
    failed = candidate(store, base, validated=False)
    with pytest.raises(EditError, match='validation'):
        service.set_default(request(service, failed))
    pending = candidate(store, base, backtested=False)
    with pytest.raises(EditError, match='backtest'):
        service.set_default(request(service, pending))
    good = candidate(store, base)
    change = request(service, good)
    result = service.set_default(change)
    assert service.set_default(change) == result
    with pytest.raises(Conflict, match='Idempotency'):
        service.set_default({**change, 'version_id': base})
    with pytest.raises(Conflict, match='changed'):
        service.set_default({**change, 'idempotency_key': uid()})
    assert service.resolve()[PATTERN] == good
    assert [v['payload']['version_number'] for v in service.list_versions(PATTERN)] == [1, 2, 3, 4]
    assert service.detail(good)['version']['parent_version_id'] == base
    assert service.detail(good)['backtests'][0]['result'] == {'trades': []}
    assert '+# candidate' in service.diff(good)[store.get('versions', good)['source_path']]
    with pytest.raises(EditError, match='replacement'):
        service.archive(request(service, good, replacement_default_version_id=None))
    archive = request(service, good, replacement_default_version_id=base)
    service.archive(archive)
    service.archive(archive)
    with pytest.raises(EditError, match='Archived'):
        service.resolve({PATTERN: good})
    assert service.detail(good)['lifecycle']['archived_at'] is not None
    assert len(service.list_versions(PATTERN)) == 3
    assert len(service.list_versions(PATTERN, include_archived=True)) == 4
    with pytest.raises(EditError, match='replacement'):
        service.archive(request(service, base, replacement_default_version_id=None))
    assert discover(version_set={PATTERN: good}, store=store)[0].pattern_version_id == good
    assert discover(version_set={PATTERN: base}, store=store)[0].pattern_version_id == base
    assert len(store.list('events')) == 2


def test_concurrent_default_archive_create(published_pattern_catalog):
    store = published_pattern_catalog
    service = PatternVersions(store)
    base = service.resolve()[PATTERN]
    good = candidate(store, base)
    change = request(service, good)
    archive = {**change, 'idempotency_key': uid(), 'replacement_default_version_id': None}

    def attempt(action):
        try:
            return action()
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(attempt, lambda: service.set_default(change)),
                   pool.submit(attempt, lambda: service.archive(archive)),
                   pool.submit(candidate, store, base)]
        outcomes = [f.result() for f in futures]
    assert outcomes[:2].count('conflict') == 1
    assert service.resolve()[PATTERN] in (base, good)
    assert len({v['payload']['version_number'] for v in service.list_versions(PATTERN, include_archived=True)}) == 3


def test_empty_outage_and_no_file_fallback(editor_store_factory, monkeypatch):
    store = editor_store_factory()
    with pytest.raises(EditError, match='bootstrap|import_pattern_baselines'):
        discover(store=store)
    def unavailable(*args, **kwargs):
        raise EditError('PostgreSQL unavailable')
    monkeypatch.setattr(store, 'connect', unavailable)
    with pytest.raises(EditError, match='unavailable'):
        discover(store=store)


def test_workers_keep_archived_pin(published_pattern_catalog):
    from core import pattern_jobs as jobs
    from core.backtester import _load_patterns
    from core.pattern_loader import worker_spec, acknowledge_worker
    from types import SimpleNamespace
    store = published_pattern_catalog
    service = PatternVersions(store)
    base = service.resolve()[PATTERN]
    pinned = {PATTERN: base}
    inline = discover(version_set=pinned, store=store)[0]
    good = candidate(store, base)
    service.archive(request(service, base, replacement_default_version_id=good))
    assert service.resolve()[PATTERN] == good
    assert _load_patterns([worker_spec(inline)])[0].pattern_version_id == base
    pool = jobs.make_analyze_pool(disabled=[], session_tz='America/New_York', skip_edgar=True,
                                 window=512, workers=2, version_set=pinned, store=store)
    try:
        assert pool.submit(worker_versions).result(timeout=60) == pinned
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    worker = SimpleNamespace(_version_set=pinned)
    acknowledge_worker(worker)
    acknowledge_worker(worker, stopped=True)
    assert store.list('workers')[0]['versions'] == pinned
    assert store.list('workers')[0]['stopped'] is True


def worker_versions():
    from core.pattern_jobs import _worker_patterns
    return {p.name: p.pattern_version_id for p in _worker_patterns}


def test_scanner_backtest_and_frontend_discovery(published_pattern_catalog, monkeypatch):
    from core.scanner import MarketScanner
    from core.backtester import Backtester, discover_pattern_names
    from web.services import discover_patterns
    import core.pattern_jobs as jobs
    store = published_pattern_catalog
    service = PatternVersions(store)
    scanner = MarketScanner.__new__(MarketScanner)
    scanner._disabled_patterns = set()
    scanner._analyze_pool = None
    scanner._market = 'us'
    scanner._discover_patterns()
    backtest = Backtester(['FIXTURE'])
    original = dict(scanner._version_set)
    good = candidate(store, original[PATTERN])
    service.set_default(request(service, good))
    calls = []
    monkeypatch.setattr(jobs, 'make_analyze_pool', lambda **kw: calls.append(kw))
    scanner._open_analyze_pool()
    assert calls[0]['version_set'] == original
    assert backtest._version_set == original
    assert len(discover_pattern_names()) == len(discover_patterns()) == 6
    scanner._discover_patterns()  # explicit next session boundary
    assert scanner._version_set[PATTERN] == good


def test_discovery_without_original_sources(published_pattern_catalog, tmp_path):
    from core.test_pattern_bootstrap import copy_snapshot
    from core.pattern_bootstrap import freeze
    from core.pattern_edit_store import EditStore
    store = published_pattern_catalog
    root = copy_snapshot(tmp_path, freeze(store.root, disabled=[]))
    for path in (root / 'patterns').glob('[0-9]*.*'):
        path.unlink()
    isolated = EditStore(root, dsn=store._dsn, schema=store.schema)
    assert len(discover(store=isolated)) == 6
    with pytest.raises(EditError, match='unavailable'):
        PatternVersions(store).resolve({'pattern_009_flag_pattern': next(
            v['version_id'] for v in store.list('versions') if v['pattern_id'] == 'pattern_009_flag_pattern')})


def test_archived_execution_and_provenance_without_sources(published_pattern_catalog, tmp_path):
    import json
    import pandas as pd
    from core.pattern_bootstrap import freeze
    from core.test_pattern_bootstrap import copy_snapshot
    from core.pattern_edit_store import EditStore
    from core.pattern_bootstrap_parity import CONFIG
    from core.backtester import _core_backtest_symbol
    from core.paper_trader import _trade_to_dict, _trade_from_dict
    from data.tv_client import OHLCVCandle
    store = published_pattern_catalog
    service = PatternVersions(store)
    base = service.resolve()[PATTERN]
    root = copy_snapshot(tmp_path, freeze(store.root, disabled=[]))
    for path in (root / 'patterns').glob('[0-9]*.*'):
        path.unlink()
    good = candidate(store, base)
    service.archive(request(service, base, replacement_default_version_id=good))
    isolated = EditStore(root, dsn=store._dsn, schema=store.schema)
    pattern = discover(version_set={PATTERN: base}, store=isolated)[0]
    pattern._baseline_modules['patterns._rules']._EARNINGS_CACHE = {'FIXTURE': []}
    data = json.loads((store.root / 'tests/fixtures/barcache/us/TXN.json').read_bytes())['bars']
    bars = [OHLCVCandle(open=r['o'], high=r['h'], low=r['l'], close=r['c'], volume=r['v'],
                       timestamp=pd.Timestamp(r['t'], unit='s', tz='UTC').to_pydatetime()) for r in data]
    trades, signals, _, _ = _core_backtest_symbol('FIXTURE', '1d', bars, [pattern], CONFIG)
    assert signals and trades
    trade = _trade_from_dict(_trade_to_dict(trades[0]))
    assert trade.pattern_version_id == base
    assert trade.signal_id and trade.trade_id and trade.provenance == 'versioned'
    assert trade.requested_rules and trade.resolved_rules
    assert all(a['pattern_version_id'] == base for a in trade.chart_annotations)
