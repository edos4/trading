"""Weekly Kronos release lifecycle, independent of torch and database access."""
from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
from zoneinfo import ZoneInfo

PHT = ZoneInfo('Asia/Manila')
LEGACY_RELEASE_NAMES = ('finetuned_campaign', 'finetuned_expA_champion', 'finetuned_run1')
# scripts/kronos_campaign.py refuses the same model below this free-memory floor at batch 16.
CAMPAIGN_VRAM_BYTES = 10_000 * 1024 * 1024
_EVAL_RESUME = frozenset({'evaluating', 'evaluation_interrupted'})
_EVAL_REVIEW = frozenset({'rejected', 'evaluation_failed', 'validated', 'rehearsed', *_EVAL_RESUME})


def required_vram_bytes(batch_size: int) -> int:
    """Free VRAM required before a weekly training step can fit.

    6,000 MiB stays reserved for weights, optimizer state, and the CUDA context.
    The rest of the 10,000 MiB campaign floor scales with the batch.
    """
    if batch_size < 1:
        raise ValueError('Batch size must be positive')
    fixed = 6_000 * 1024 * 1024
    return fixed + ((CAMPAIGN_VRAM_BYTES - fixed) * batch_size) // 16


def ensure_vram(free_bytes: int, batch_size: int) -> int:
    """Raise when the measured free memory cannot hold this batch."""
    required = required_vram_bytes(batch_size)
    if free_bytes < required:
        raise RuntimeError(
            f'Only {free_bytes // (1024 * 1024)} MiB VRAM free; '
            f'batch {batch_size} needs {required // (1024 * 1024)} MiB')
    return required


def training_complete(release: Path) -> bool:
    """Both stages wrote a best checkpoint and their metrics."""
    return all((release / name / 'best_model').is_dir() and (release / name / 'training_metrics.json').is_file()
               for name in ('tokenizer', 'predictor'))


def snapshot_bounds(run_date: date) -> tuple[date, date]:
    """Return five calendar years ending exclusively on the named Saturday."""
    if run_date.weekday() != 5:
        raise ValueError('Run date must be a Saturday')
    try:
        start = run_date.replace(year=run_date.year - 5)
    except ValueError:
        start = run_date.replace(year=run_date.year - 5, day=28)
    return start, run_date


def atomic_text(path: Path, text: str) -> None:
    """Durably replace a file without exposing partial contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f'.{path.stem}.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def record_completion(checkout: Path, release: Path, *, now: datetime | None = None) -> str:
    """Persist completion metadata before updating the last-success marker."""
    completed = now or datetime.now(PHT)
    if completed.tzinfo is None:
        raise ValueError('Completion time must be timezone aware')
    stamp = completed.astimezone(PHT).isoformat(timespec='seconds')
    manifest_path = release / 'run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['completed_at'] = stamp
    atomic_text(manifest_path, json.dumps(manifest, indent=2) + '\n')
    atomic_text(checkout / 'kronos-finetuned-at.txt', stamp + '\n')
    return stamp


def remove_superseded(runs: Path, active: Path) -> None:
    """Remove managed releases only; reject symlink traversal and unsafe roots."""
    if runs.is_symlink():
        raise ValueError('Runs directory must not be a symlink')
    root = runs.resolve(strict=True)
    current = active.resolve(strict=True)
    if current.parent != root:
        raise ValueError('Active release must be a direct child of runs')
    children = list(root.iterdir())
    for child in children:
        if child.is_symlink() or not child.is_dir():
            raise ValueError(f'Unexpected managed release entry: {child.name}')
    for child in children:
        if child.resolve() != current:
            shutil.rmtree(child)


# No torch import here: dry-run, recovery, installation, and tests use the app venv.
from contextlib import contextmanager
from datetime import timedelta
import fcntl
import hashlib
import time
import uuid


@contextmanager
def exclusive_lock(path: Path, *, timeout: float = 0):
    """A bounded interprocess lock; held file descriptors are never unlinked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'Lock busy: {path.name}')
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def write_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')


def write_report(weekly: Path, manifest: dict) -> None:
    """Bound audit retention for unsuccessful runs as well as successful ones."""
    reports = weekly / 'reports'
    if reports.is_symlink():
        raise ValueError('Reports directory cannot be a symlink')
    reports.mkdir(exist_ok=True)
    run_id = manifest['run_id']
    if Path(run_id).name != run_id or run_id in ('.', '..'):
        raise ValueError('Invalid report identifier')
    write_json(reports / f'{run_id}.json', manifest)
    for path in sorted(reports.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[52:]:
        path.unlink()


def component_hashes(root: Path) -> dict:
    from learn.kronos_weekly_dataset import file_hash
    result = {}
    for component in ('tokenizer', 'predictor'):
        directory = root / component / 'best_model'
        files = sorted(p for p in directory.rglob('*') if p.is_file())
        if not files or any(p.is_symlink() for p in directory.rglob('*')):
            raise ValueError(f'Missing or linked {component} checkpoint')
        result[component] = {str(p.relative_to(directory)): file_hash(p) for p in files}
    return result


def create_selection_manifest(release: Path, corpus_hash: str, end: date) -> dict:
    # Model libraries close their files but do not guarantee durable writes.
    for file in root_files(release):
        with file.open('rb') as stream:
            os.fsync(stream.fileno())
    value = {'schema_version': 1, 'corpus_sha256': corpus_hash,
             'selection_data_end': (end - timedelta(days=1)).isoformat(),
             'eligible_from': end.isoformat(), 'components': component_hashes(release)}
    write_json(release / 'selection_manifest.json', value)
    from core.kronos_provenance import checkpoint_provenance
    checkpoint_provenance(release, None, required=True)
    return value


def root_files(release: Path) -> list[Path]:
    """Checkpoint files only; avoid fsyncing a multi-gigabyte temporary corpus."""
    return [file for component in ('tokenizer', 'predictor')
            for file in (release / component / 'best_model').rglob('*') if file.is_file()]


def replace_pointer(active: Path, target: Path) -> None:
    """Atomically replace the one pointer binding both checkpoint components."""
    temporary = active.with_name(f'.{active.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.symlink_to(target.resolve(), target_is_directory=True)
        os.replace(temporary, active)
        descriptor = os.open(active.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _managed_release(runs: Path, name: str) -> Path:
    if not name or Path(name).name != name or name in ('.', '..'):
        raise ValueError('Invalid release identifier')
    path = runs / name
    if path.is_symlink() or path.resolve().parent != runs.resolve():
        raise ValueError('Release must be a direct, unlinked managed directory')
    return path


def _finish_cleanup(home: Path, checkout: Path, journal: dict) -> None:
    """Idempotent cleanup and timestamp recovery; never roll back after commit."""
    weekly = home / 'weekly'
    release = _managed_release(weekly / 'runs', journal['candidate'])
    active = home / 'finetuned'
    if not active.is_symlink() or active.resolve() != release.resolve():
        raise ValueError('Recovery journal does not match the active release')
    from core.kronos_provenance import checkpoint_provenance
    checkpoint_provenance(release, None, required=True)
    remove_superseded(weekly / 'runs', release)
    for name in journal.get('legacy_inventory', []):
        if name not in LEGACY_RELEASE_NAMES:
            raise ValueError('Unrecognized legacy release in cleanup journal')
        old = home / name
        if old.is_symlink():
            raise ValueError('Refusing linked legacy release deletion')
        if old.exists():
            if not old.is_dir() or old.resolve().parent != home.resolve():
                raise ValueError('Unsafe legacy release deletion')
            shutil.rmtree(old)
    dataset = release / 'dataset'
    if dataset.is_symlink():
        raise ValueError('Unexpected linked dataset')
    if dataset.exists():
        shutil.rmtree(dataset)
    for component in ('tokenizer', 'predictor'):
        (release / component / 'last.pt').unlink(missing_ok=True)
        for temporary in (release / component).glob('.last.pt.*.tmp'):
            temporary.unlink()
    manifest_path = release / 'run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    # Reuse a durable completion time if only the final marker write failed.
    stamp = manifest.get('completed_at') or datetime.now(PHT).isoformat(timespec='seconds')
    manifest.pop('error', None)
    manifest.update(status='complete', completed_at=stamp)
    write_json(manifest_path, manifest)
    write_report(weekly, manifest)
    journal['phase'] = 'timestamp'
    write_json(weekly / 'activation.json', journal)
    atomic_text(checkout / 'kronos-finetuned-at.txt', stamp + '\n')
    journal['phase'] = 'complete'
    write_json(weekly / 'activation.json', journal)


def finish_cleanup(home: Path, checkout: Path, journal: dict) -> None:
    """Keep failed cleanup/timestamp status truthful during activation and retries."""
    try:
        _finish_cleanup(home, checkout, journal)
    except Exception as exc:
        release = _managed_release(home / 'weekly' / 'runs', journal['candidate'])
        manifest_path = release / 'run_manifest.json'
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            manifest.update(status='timestamp_write_failed' if journal['phase'] == 'timestamp' else 'cleanup_failed',
                            error=f'{type(exc).__name__}: {exc}')
            write_json(manifest_path, manifest)
            write_report(home / 'weekly', manifest)
        raise


def recover(home: Path, checkout: Path) -> bool:
    """Restore pre-commit activation or finish a committed replacement."""
    if (home / 'weekly').is_symlink() or (home / 'weekly' / 'runs').is_symlink():
        raise ValueError('Recovery refuses linked managed roots')
    path = home / 'weekly' / 'activation.json'
    if not path.exists():
        return False
    journal = json.loads(path.read_text())
    if journal['phase'] in ('complete', 'rolled_back'):
        return False
    if journal['phase'] in ('cleanup', 'timestamp'):
        finish_cleanup(home, checkout, journal)
        return True
    if journal['phase'] != 'prepared':
        raise ValueError('Unrecognized activation recovery phase')
    runs, active = home / 'weekly' / 'runs', home / 'finetuned'
    candidate = _managed_release(runs, journal['candidate'])
    previous = _managed_release(runs, journal['previous']) if journal.get('previous') else None
    if journal.get('legacy'):
        if previous.exists():
            if active.is_symlink():
                active.unlink()
            elif active.exists():
                raise ValueError('Ambiguous legacy recovery layout')
            os.replace(previous, active)
        elif not active.is_dir() or active.is_symlink():
            raise ValueError('Legacy release disappeared during migration')
    elif previous is not None:
        if not previous.is_dir():
            raise ValueError('Previous release disappeared before commit')
        replace_pointer(active, previous)
    elif active.is_symlink() and active.resolve() == candidate.resolve():
        active.unlink()
    journal['phase'] = 'rolled_back'
    write_json(path, journal)
    return False


def activate(home: Path, checkout: Path, release: Path, smoke) -> None:
    """Commit a checked pair, then delete every superseded managed release."""
    runs = home / 'weekly' / 'runs'
    if _managed_release(runs, release.name).resolve() != release.resolve():
        raise ValueError('Candidate is outside the managed release directory')
    active = home / 'finetuned'
    inventory = []
    for old in home.glob('finetuned_*'):
        if old.name not in LEGACY_RELEASE_NAMES or old.is_symlink() or not old.is_dir():
            raise ValueError(f'Unrecognized legacy fine-tuned output requires inventory review: {old.name}')
        if not all((old / component).is_dir() for component in ('tokenizer', 'predictor')):
            raise ValueError(f'Legacy output lacks expected components: {old.name}')
        inventory.append(old.name)
    legacy = active.exists() and not active.is_symlink()
    previous = None
    if legacy:
        previous = runs / f'legacy-{uuid.uuid4().hex}'
    elif active.is_symlink():
        previous = active.resolve(strict=True)
        if previous.parent != runs.resolve():
            raise ValueError('Active pointer references an unmanaged release')
    journal = {'phase': 'prepared', 'candidate': release.name,
               'previous': previous.name if previous else None, 'legacy': legacy, 'legacy_inventory': sorted(inventory)}
    write_json(home / 'weekly' / 'activation.json', journal)
    try:
        if legacy:
            os.replace(active, previous)
        replace_pointer(active, release)
        smoke(active.resolve(strict=True))
    except BaseException:
        recover(home, checkout)
        raise
    journal['phase'] = 'cleanup'
    write_json(home / 'weekly' / 'activation.json', journal)
    finish_cleanup(home, checkout, journal)


def freshness_counts(rows: list[tuple], no_trade: set[str] | None = None) -> dict:
    """Split recent names into the prior-session denominator and earlier stoppages.

    ``rows`` are ``(symbol, traded_prior_session, has_expected_session)``. A name
    that last traded before the prior session is already suspended or delisted,
    so it stays out of the freshness fraction. A name the exchange report shows
    did not trade the expected session is also outside that fraction: a missing
    bar counts only when the exchange traded and the import has no bar. Older
    bars remain trainable.
    """
    quiet = no_trade or set()
    reference, inactive, skipped = [], [], []
    for row in rows:
        if not row[1]:
            inactive.append(row)
        elif not row[2] and row[0] in quiet:
            skipped.append(row)
        else:
            reference.append(row)
    fresh = [row for row in reference if row[2]]
    return {'reference_symbols': [row[0] for row in reference], 'reference_count': len(reference),
            'fresh_count': len(fresh), 'fresh_fraction': len(fresh) / len(reference) if reference else 0,
            'excluded_inactive_symbols': [row[0] for row in inactive], 'excluded_inactive_count': len(inactive),
            'excluded_no_trade_symbols': [row[0] for row in skipped], 'excluded_no_trade_count': len(skipped)}


def readiness(end: date, *, connection_factory=None, no_trade_loader=None) -> dict:
    """Compare prior-session participants with the expected latest session."""
    from learn.kronos_weekly_dataset import expected_session
    if connection_factory is None:
        import psycopg
        from data.db import build_dsn
        connection_factory = lambda: psycopg.connect(build_dsn())
    if no_trade_loader is None:
        from data.pse_quotation import no_trade_symbols
        no_trade_loader = no_trade_symbols
    result = {}
    with connection_factory() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        for market in ('us', 'ph'):
            expected = expected_session(end, market)
            prior = expected_session(expected, market)
            with conn.cursor() as cur:
                cur.execute(
                    "WITH recent AS (SELECT DISTINCT b.symbol FROM daily_bars b JOIN symbols s ON s.symbol=b.symbol "
                    "WHERE s.market=%s AND b.bar_date >= %s AND b.bar_date < %s) "
                    "SELECT r.symbol, EXISTS(SELECT 1 FROM daily_bars b WHERE b.symbol=r.symbol AND b.bar_date=%s), "
                    "EXISTS(SELECT 1 FROM daily_bars b WHERE b.symbol=r.symbol AND b.bar_date=%s) "
                    "FROM recent r ORDER BY r.symbol",
                    (market, expected - timedelta(days=14), expected, prior, expected))
                rows = cur.fetchall()
            no_trade, report_state = set(), 'not_applicable'
            if market == 'ph':
                try:
                    no_trade = set(no_trade_loader(expected))
                    report_state = 'matched'
                except Exception as exc:
                    report_state = f'unavailable: {type(exc).__name__}: {exc}'
            result[market] = {'expected_session': expected.isoformat(), 'prior_session': prior.isoformat(),
                              'no_trade_report': report_state, **freshness_counts(rows, no_trade)}
    return result


def wait_ready(end: date, *, threshold: float = .95, deadline: datetime | None = None,
               probe=readiness, sleep=time.sleep) -> dict:
    """Wait for both imports, retaining the intended Saturday's fixed bounds."""
    while True:
        result = probe(end)
        if all(result[m]['fresh_fraction'] >= threshold for m in ('us', 'ph')):
            return result
        now = datetime.now(PHT)
        if deadline is None or now >= deadline:
            hidden = {'reference_symbols', 'excluded_inactive_symbols', 'excluded_no_trade_symbols'}
            summary = {m: {k: v for k, v in row.items() if k not in hidden} for m, row in result.items()}
            raise RuntimeError(f'Stale weekly history: {json.dumps(summary)}')
        sleep(min(300, max(0, (deadline - now).total_seconds())))


def source_fingerprint(checkout: Path) -> str:
    from learn.kronos_weekly_dataset import file_hash
    names = ('core/kronos_weekly.py', 'core/kronos_weekly_eval.py', 'core/kronos_finetune.py',
             'core/kronos_eval.py', 'learn/kronos_weekly_dataset.py', 'learn/kronos_dataset.py',
             'scripts/kronos_weekly_finetune.py', 'scripts/install_kronos_weekly.py',
             'core/kronos_provenance.py', 'core/kronos_process.py', 'core/kronos_confirm.py',
             'core/market.py', 'core/kronos_input.py', 'scripts/kronos_confirm_worker.py',
             'data/pse_quotation.py', 'requirements-kronos.txt')
    identities = {name: file_hash(checkout / name) for name in names}
    return hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()


def train_release(release: Path, config: dict, *, resume: bool = False) -> None:
    """Run both stages from base, with resume confined to this exact candidate."""
    import torch
    from core.kronos_finetune import _train_tokenizer, _train_predictor
    from learn.kronos_weekly_dataset import file_hash
    manifest_path = release / 'run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    common = dict(epochs=config['epochs'], batch_size=config['batch_size'], lookback=config['lookback'],
                  predict_len=config['predict_len'], clip=5., seed=config['seed'], output_root=release,
                  weekly=True, train_steps=config['train_steps'], val_steps=config['val_steps'])
    tokenizer = release / 'tokenizer' / 'best_model'
    if not manifest.get('tokenizer_files'):
        tokenizer = _train_tokenizer(release / 'dataset', torch.device('cuda'), lr=2e-4, resume=resume, **common)
        manifest['tokenizer_files'] = {str(p.relative_to(tokenizer)): file_hash(p) for p in tokenizer.rglob('*') if p.is_file()}
        write_json(manifest_path, manifest)
        torch.cuda.empty_cache()
    actual = {str(p.relative_to(tokenizer)): file_hash(p) for p in tokenizer.rglob('*') if p.is_file()}
    if actual != manifest['tokenizer_files']:
        raise ValueError('Resume tokenizer identity differs')
    _train_predictor(release / 'dataset', tokenizer, torch.device('cuda'), lr=4e-5, resume=resume, **common)


def smoke_anchors(release: Path, dataset: Path, config: dict) -> list[dict]:
    """Reload checks use viable evaluated contexts; full evaluation owns quality gates."""
    evaluation_path = release / 'evaluation.json'
    if evaluation_path.exists():
        rows = json.loads(evaluation_path.read_text())['candidate']['rows']
        one_day = {(row['market'], row['symbol'], row['target_start'])
                   for row in rows if row['horizon'] == 1 and row['valid']}
        anchors = [row for row in rows if row['horizon'] == 3 and row['valid']
                   and (row['market'], row['symbol'], row['target_start']) in one_day]
    else:
        from core.kronos_weekly_eval import select_anchors
        anchors = select_anchors(dataset, symbols_per_market=config.get('eval_symbols', 50),
                                 lookback=config['lookback'])
    sample = []
    for market in ('us', 'ph'):
        choices = [row for row in anchors if row['market'] == market]
        if not choices:
            raise ValueError(f'No usable evaluated {market} smoke context')
        row = choices[len(choices) // 2]
        sample.append({key: row[key] for key in ('market', 'symbol', 'context_start', 'target_start', 'horizon')})
    return sample


def smoke_release(release: Path, dataset: Path, config: dict) -> None:
    """Use the production loader for one frozen context in each market."""
    import torch
    from core.kronos_weekly_eval import evaluate_model
    from core.kronos_eval import _load_predictor
    from core.kronos_provenance import checkpoint_provenance
    checkpoint_provenance(release, None, required=True)
    sample = smoke_anchors(release, dataset, config)
    predictor = _load_predictor(device='cuda', use_finetuned=True, model_root=release)
    predictor.model.eval()
    predictor.tokenizer.eval()
    try:
        with torch.no_grad():
            result = evaluate_model(predictor, dataset, sample, seed=config['seed'])
        if any(not r['valid'] for r in result['rows']):
            raise ValueError('Activated model failed US/PH smoke forecasts')
    finally:
        del predictor
        torch.cuda.empty_cache()


def run_weekly(home: Path, checkout: Path, run_date: date, config: dict, *, rehearsal: bool = False,
               resume_id: str | None = None, ready_probe=wait_ready, builder=None,
               trainer=train_release, evaluator=None, smoker=smoke_release, gpu_check=None) -> Path:
    """Complete weekly transaction. Call only with the weekly and GPU locks held."""
    from learn.kronos_weekly_dataset import build_snapshot, file_hash
    from core.kronos_weekly_eval import compare
    start, end = snapshot_bounds(run_date)
    home = home.resolve()
    weekly, runs = home / 'weekly', home / 'weekly' / 'runs'
    if weekly.is_symlink() or runs.is_symlink():
        raise ValueError('Weekly roots cannot be symlinks')
    runs.mkdir(parents=True, exist_ok=True)
    recover(home, checkout)
    active = home / 'finetuned'
    if active.is_symlink() and (active / 'run_manifest.json').exists():
        previous = json.loads((active / 'run_manifest.json').read_text())
        if previous.get('run_date', '') > end.isoformat():
            raise ValueError('Cannot replace a newer scheduled release')
        if previous.get('run_date') == end.isoformat() and not rehearsal:
            return active.resolve()
    identity = source_fingerprint(checkout)
    from core.kronos_finetune import TOKENIZER_PATH, MODEL_PATH
    base_identity = {}
    for label, root in (('tokenizer', TOKENIZER_PATH), ('predictor', MODEL_PATH)):
        files = {str(p.relative_to(root)): file_hash(p) for p in root.rglob('*') if p.is_file()}
        if not files:
            raise ValueError(f'Missing original {label} weights at {root}')
        base_identity[label] = files
    upstream = home / 'model'
    base_identity['upstream_source'] = {str(p.relative_to(upstream)): file_hash(p) for p in sorted(upstream.rglob('*.py'))}
    release = _managed_release(runs, resume_id or f'{end.isoformat()}-{uuid.uuid4().hex[:12]}')
    if resume_id:
        manifest = json.loads((release / 'run_manifest.json').read_text())
        if (manifest['config'] != config or manifest['run_date'] != end.isoformat()
                or manifest['source_sha256'] != identity or manifest['base_identity'] != base_identity
                or manifest['rehearsal'] != rehearsal
                or manifest['status'] not in ('training', 'interrupted', *_EVAL_RESUME)):
            raise ValueError('Resume run/config/source/base identity mismatch')
        report = json.loads((release / 'dataset' / kd_report_name()).read_text())
        if any(file_hash(release / 'dataset' / name) != value for name, value in report['files'].items()):
            raise ValueError('Resume corpus identity differs')
    else:
        try:
            fresh = ready_probe(end, threshold=config['freshness'], deadline=datetime.combine(end, datetime.min.time(), PHT).replace(hour=8))
        except Exception as exc:
            write_report(weekly, {'run_id': release.name, 'run_date': end.isoformat(),
                       'status': 'data_not_ready', 'error': str(exc), 'config': config})
            raise
        if shutil.disk_usage(home).free < config['min_free_gb'] * 1024**3:
            raise RuntimeError('Insufficient free disk space for candidate and temporary state')
        release.mkdir()
        manifest = {'run_id': release.name, 'run_date': end.isoformat(), 'config': config,
                    'source_sha256': identity, 'base_identity': base_identity,
                    'rehearsal': rehearsal, 'status': 'building', 'freshness': fresh,
                    'started_at': datetime.now(PHT).isoformat(), 'window_start': start.isoformat(), 'window_end': end.isoformat()}
        write_json(release / 'run_manifest.json', manifest)
        try:
            snapshot = (builder or build_snapshot)(release / 'dataset', start=start, end=end,
                                                 lookback=config['lookback'], predict_len=config['predict_len'],
                                                 market_weights=tuple(config['market_weights']))
            manifest.update(corpus_sha256=snapshot['corpus_sha256'], dataset_report=snapshot['report'], status='training')
            write_json(release / 'run_manifest.json', manifest)
        except BaseException:
            shutil.rmtree(release)
            raise
    if gpu_check is not None:
        gpu_check(config)
    try:
        if manifest['status'] in _EVAL_RESUME:
            if not training_complete(release):
                raise ValueError('Interrupted evaluation has no complete trained pair')
        else:
            trainer(release, config, resume=bool(resume_id))
        manifest = json.loads((release / 'run_manifest.json').read_text())
        manifest['training_metrics'] = {c: json.loads((release / c / 'training_metrics.json').read_text()) for c in ('tokenizer', 'predictor')}
        manifest['status'] = 'evaluating'
        write_json(release / 'run_manifest.json', manifest)
        create_selection_manifest(release, manifest['corpus_sha256'], end)
        result = (evaluator or compare)(release / 'dataset', release, config)
        write_json(release / 'evaluation.json', result)
        manifest = json.loads((release / 'run_manifest.json').read_text())
        manifest['evaluation'] = {k: v['metrics'] for k, v in result.items() if k in ('base', 'candidate')}
        manifest['failures'] = result['failures']
        manifest['status'] = 'rejected' if result['failures'] else 'validated'
        write_json(release / 'run_manifest.json', manifest)
        write_report(weekly, manifest)
        if result['failures']:
            raise ValueError(f"Weekly candidate rejected: {result['failures']}")
        create_selection_manifest(release, manifest['corpus_sha256'], end)
        manifest['training_metrics'] = {c: json.loads((release / c / 'training_metrics.json').read_text()) for c in ('tokenizer', 'predictor')}
        manifest['status'] = 'validated'
        write_json(release / 'run_manifest.json', manifest)
        if rehearsal:
            smoker(release, release / 'dataset', config)
            manifest['status'] = 'rehearsed'
            write_json(release / 'run_manifest.json', manifest)
            return release
        activate(home, checkout, release, lambda root: smoker(root, root / 'dataset', config))
        return release
    except KeyboardInterrupt:
        manifest = json.loads((release / 'run_manifest.json').read_text())
        if manifest['status'] == 'training':
            manifest['status'] = 'interrupted'
            write_json(release / 'run_manifest.json', manifest)
        elif manifest['status'] == 'evaluating':
            manifest['status'] = 'evaluation_interrupted'
            write_json(release / 'run_manifest.json', manifest)
        raise
    except Exception as exc:
        # Once committed, recovery owns this active directory and must finish deletion.
        if not (active.is_symlink() and active.resolve() == release.resolve()):
            manifest = json.loads((release / 'run_manifest.json').read_text())
            retain = manifest['status'] in ('evaluating', 'evaluation_interrupted', 'rejected', 'validated')
            manifest.update(status='rejected' if manifest['status'] == 'rejected' else 'evaluation_failed' if retain else 'failed',
                            error=f'{type(exc).__name__}: {exc}')
            write_report(weekly, manifest)
            if retain:
                write_json(release / 'run_manifest.json', manifest)
            else:
                shutil.rmtree(release)
        raise


def kd_report_name() -> str:
    from learn.kronos_dataset import REPORT_FILE
    return REPORT_FILE


def evaluate_saved(home: Path, checkout: Path, run_id: str, *, rehearsal: bool = False,
                   invalid_policy: str | None = None, max_invalid_rate: float | None = None,
                   evaluator=None, smoker=smoke_release) -> Path:
    """Reevaluate a complete saved pair without retraining or changing its data."""
    from core.kronos_provenance import checkpoint_provenance
    from core.kronos_weekly_eval import compare
    from learn.kronos_weekly_dataset import file_hash
    recover(home, checkout)
    release = _managed_release(home / 'weekly' / 'runs', run_id)
    manifest_path = release / 'run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest['status'] not in _EVAL_REVIEW:
        raise ValueError('Saved release has no complete trained pair to reevaluate')
    if manifest['status'] in _EVAL_RESUME and not training_complete(release):
        raise ValueError('Interrupted evaluation has no complete trained pair')
    if manifest['config'].get('smoke') and not rehearsal:
        raise ValueError('Smoke candidates cannot be activated')
    if not (release / 'selection_manifest.json').exists():
        create_selection_manifest(release, manifest['corpus_sha256'], date.fromisoformat(manifest['window_end']))
    checkpoint_provenance(release, None, required=True)
    report = json.loads((release / 'dataset' / kd_report_name()).read_text())
    if any(file_hash(release / 'dataset' / name) != digest for name, digest in report['files'].items()):
        raise ValueError('Saved corpus identity differs')
    active = home / 'finetuned'
    if active.is_symlink() and (active / 'run_manifest.json').exists():
        active_manifest = json.loads((active / 'run_manifest.json').read_text())
        if active_manifest.get('run_date', '') > manifest['run_date']:
            raise ValueError('Cannot replace a newer release with an older saved candidate')
    config = dict(manifest['config'])
    history = manifest.setdefault('evaluation_history', [])
    if manifest.get('evaluation'):
        history.append({'metrics': manifest['evaluation'], 'failures': manifest.get('failures'),
                        'invalid_policy': config.get('invalid_policy', 'zero'),
                        'max_invalid_rate': config.get('max_invalid_rate', .01)})
    manifest['evaluation_history'] = history[-10:]
    if invalid_policy is not None:
        config['invalid_policy'] = invalid_policy
    if max_invalid_rate is not None:
        config['max_invalid_rate'] = max_invalid_rate
    manifest['config'] = config
    manifest['evaluation_source_sha256'] = source_fingerprint(checkout)
    result = (evaluator or compare)(release / 'dataset', release, config)
    write_json(release / 'evaluation.json', result)
    manifest['evaluation'] = {key: value['metrics'] for key, value in result.items() if key in ('base', 'candidate')}
    manifest['failures'] = result['failures']
    manifest['status'] = 'rejected' if result['failures'] else 'validated'
    manifest.pop('error', None)
    write_json(manifest_path, manifest)
    write_report(home / 'weekly', manifest)
    if result['failures']:
        raise ValueError(f"Saved candidate rejected: {result['failures']}")
    if rehearsal:
        smoker(release, release / 'dataset', config)
        manifest['status'] = 'rehearsed'
        write_json(manifest_path, manifest)
    else:
        activate(home, checkout, release, lambda root: smoker(root, root / 'dataset', config))
    return release
