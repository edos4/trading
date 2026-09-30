"""Explicit immutable pattern loading. Applied source executes only in a sandbox."""
from __future__ import annotations

import importlib

from core.pattern_edit_store import EditError, open_pattern_store, uid
from core.pattern_versions import PatternVersions
from core.pattern_provenance import rules
from patterns.base_pattern import BasePattern, TradeSignal
from utils.logger import log

#: Provenances that are the repository's own detector source, so they execute in
#: this process against the working tree. Anything else is an applied edit and
#: belongs in the candidate sandbox unless it has been preloaded here.
BASELINE_PROVENANCE = ('file-import', 'trusted-snapshot')


def active_versions(store=None):
    return PatternVersions(store).resolve()


def load_version_source(store, version, *, check_runtime=True):
    """Private snapshot modules for one version; shared interface identity.

    check_runtime verifies execution dependencies; chart presentation skips it.
    Application settings remain live in both cases.
    """
    import builtins
    import types
    import sys
    import weakref
    namespace = "_pattern_baseline_" + version['version_id'] + "_" + uid()
    modules = {}
    original_import = builtins.__import__

    def load(name):
        if name in modules:
            return modules[name]
        if name == "patterns.base_pattern":
            return importlib.import_module(name)
        if name == "patterns":
            module = types.ModuleType(namespace + ".patterns")
            module.__path__ = []
            modules[name] = module
            module.__package__ = module.__name__
            module.__builtins__ = {**vars(builtins), '__import__': importing}
            if 'patterns/__init__.py' in version['files']:
                exec(compile(store.read_blob(version['files']['patterns/__init__.py']),
                             'patterns/__init__.py', 'exec'), module.__dict__)
            return module
        relative = name.replace('.', '/') + '.py'
        if relative not in version['files']:
            raise ImportError("Dependency absent from version: " + name)
        module = types.ModuleType(namespace + '.' + name)
        module.__package__ = namespace + '.' + name.rpartition('.')[0]
        module.__builtins__ = {**vars(builtins), "__import__": importing}
        modules[name] = module
        # dataclasses resolves the defining module through sys.modules.
        sys.modules[module.__name__] = module
        exec(compile(store.read_blob(version['files'][relative]), relative, 'exec'), module.__dict__)
        return module

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            package = (globals or {}).get('__package__', '').removeprefix(namespace + '.')
            name = importlib.util.resolve_name('.' * level + name, package)
            level = 0
        if name == 'importlib':
            proxy = types.ModuleType('importlib')
            proxy.__dict__.update(vars(importlib))
            proxy.import_module = lambda target, package=None: (
                load(target) if target.startswith('patterns.') else importlib.import_module(target, package))
            return proxy
        if name == 'patterns.base_pattern':
            return original_import(name,globals,locals,fromlist,0)
        if name == 'patterns' or name.startswith('patterns.'):
            module = load(name)
            for child in fromlist or ():
                if child != '*' and not hasattr(module,child):
                    setattr(module,child,load(name+'.'+child))
            if fromlist:
                return module
            package = load('patterns')
            if name != 'patterns':
                setattr(package,name.split('.')[1],module)
            return package
        # Shared runtime dependencies are checked, never silently substituted.
        relative = name.replace('.', '/') + '.py'
        # Settings remain live, just like their environment-variable values.
        if check_runtime and relative != 'config.py' and relative in version['files']:
            from core.pattern_edit_store import digest
            current = store.root / relative
            if not current.exists() or digest(current.read_bytes()) != version['files'][relative]['sha256']:
                raise ValueError('Baseline runtime dependency changed: ' + relative)
        return original_import(name,globals,locals,fromlist,level)

    try:
        module = load(version['source_path'][:-3].replace('/','.'))
        classes = [c for c in vars(module).values() if isinstance(c,type) and c is not BasePattern
                   and issubclass(c,BasePattern) and c.__module__ == module.__name__]
        if len(classes) != 1:
            raise ValueError('Expected exactly one detector class')
        instance = classes[0]()
    except BaseException:
        for loaded in modules.values():
            sys.modules.pop(loaded.__name__, None)
        raise
    weakref.finalize(instance, lambda: [sys.modules.pop(m.__name__, None) for m in modules.values()])
    return instance, modules


def detector_metadata(detector):
    """Metadata as the detector source declares it."""
    return {'name':detector.name,'timeframes':detector.timeframes,
            'skipped':detector.skipped, 'chart_description':detector.chart_description,
            **{k:getattr(detector,k,d) for k,d in
               [('MIN_BARS',2),('HORIZON_BARS',5),('MAX_OPEN_PER_SYMBOL',None)]}}


class VersionPattern(BasePattern):
    def __init__(self, version_id, store=None, *, runtime=True, in_process=False):
        self.store = store or open_pattern_store()
        self.version = PatternVersions(self.store).verify(version_id,runtime=runtime)
        self.pattern_version_id = version_id
        self.metadata = self.version.get('metadata')
        self._baseline = None
        self._baseline_modules = None
        baseline = self.version.get('provenance') in BASELINE_PROVENANCE
        if baseline:
            # Baseline metadata is inspected from unchanged, trusted repository
            # source. Baselines are not activated edits, and use ordinary imports.
            if self.version['parent_version_id'] is not None:
                raise ValueError('Validated version metadata is missing')
            self._baseline, self._baseline_modules = load_version_source(
                self.store, self.version, check_runtime=runtime)
        elif in_process:
            # An approved edit runs its own source here, once, exactly like a
            # baseline -- not in a fresh sandbox process for every symbol. A
            # version that will not load here falls back to the sandbox.
            try:
                self._baseline, self._baseline_modules = load_version_source(
                    self.store, self.version, check_runtime=False)
            except Exception:
                log.exception(
                    f"Pattern | version {version_id} did not preload in-process; "
                    "every analysis will use the candidate sandbox"
                )
        if self._baseline is not None:
            self.metadata = detector_metadata(self._baseline)
        elif self.metadata is None:
            raise EditError('Validated version metadata is missing')
        for key in ('MIN_BARS','HORIZON_BARS','MAX_OPEN_PER_SYMBOL'):
            setattr(self,key,self.metadata[key])

    @property
    def name(self):
        return self.version['pattern_id']

    @property
    def timeframes(self):
        return self.metadata['timeframes']

    @property
    def skipped(self):
        return self.metadata.get('skipped', False)

    @property
    def chart_description(self):
        return self.metadata.get('chart_description', super().chart_description)

    def _sync_dedup(self):
        from patterns import _dedup
        private = self._baseline_modules.get('patterns._dedup')
        if private is not None:
            private._used = set(_dedup._used)
            private._current = _dedup._current
            private._gen = _dedup._gen

    def _finalize(self, signal):
        if signal:
            for annotation in signal.chart_annotations:
                annotation['pattern_version_id'] = self.pattern_version_id
            signal.pattern_version_id = self.pattern_version_id
            signal.signal_id = signal.signal_id or uid()
            signal.provenance = 'versioned'
            signal.requested_rules = rules(signal,max_open_per_symbol=self.MAX_OPEN_PER_SYMBOL,entry_mode='signal_close')
        return signal

    def _sandbox_dataset(self, snapshot, store, session_tz):
        from core.pattern_edit_validation import candle_rows
        from patterns import _dedup
        frame = store.get_df(snapshot.symbol,snapshot.timeframe,min_bars=2)
        if frame is None:
            return None
        return {'symbol':snapshot.symbol,'timeframe':snapshot.timeframe,'market':'us',
                'session_timezone':session_tz,'candles':candle_rows(frame,session_tz),
                'operation':'analyze','current_bar':_dedup._current,'used_pivots':list(_dedup._used)}

    def analyze(self, snapshot, store):
        if self._baseline is not None:
            self._sync_dedup()
            return self._finalize(self._baseline.analyze(snapshot,store))
        from core.pattern_edit_validation import Validator
        dataset = self._sandbox_dataset(
            snapshot, store, getattr(store,'_session_tz','America/New_York'))
        if dataset is None:
            return None
        result = Validator(self.store).execute(self.version,{},dataset)
        return self._finalize(TradeSignal(**result['signals'][0]) if result['signals'] else None)

    def analyze_many(self, snapshots, store):
        """Signals aligned to `snapshots`, with one failure never ending the batch.

        A preloaded version evaluates in this process; otherwise the whole batch
        goes through one sandbox process instead of one per snapshot.
        """
        snapshots = list(snapshots)
        if self._baseline is not None:
            signals = []
            for snapshot in snapshots:
                try:
                    self._sync_dedup()
                    signals.append(self._finalize(self._baseline.analyze(snapshot,store)))
                except Exception:
                    log.exception(f"analyze | {self.name} {snapshot.symbol} {snapshot.timeframe}")
                    signals.append(None)
            return signals
        return self._sandbox_many(snapshots, store)

    def _sandbox_many(self, snapshots, store):
        from core.pattern_edit_validation import Validator
        session_tz = getattr(store,'_session_tz','America/New_York')
        signals: list[TradeSignal | None] = [None]*len(snapshots)
        pending: list[int] = []
        datasets: list[dict] = []
        for index, snapshot in enumerate(snapshots):
            try:
                dataset = self._sandbox_dataset(snapshot, store, session_tz)
            except Exception:
                log.exception(f"analyze | {self.name} {snapshot.symbol} {snapshot.timeframe}")
                continue
            if dataset is None:
                continue
            pending.append(index)
            datasets.append(dataset)
        if not datasets:
            return signals
        try:
            results = Validator(self.store).execute_many(self.version, {}, datasets)
        except Exception:
            # A whole-batch failure must not cost every symbol its analysis.
            log.exception(f"analyze | {self.name} batched sandbox failed — per-symbol retry")
            for index in pending:
                snapshot = snapshots[index]
                try:
                    signals[index] = self.analyze(snapshot, store)
                except Exception:
                    log.exception(f"analyze | {self.name} {snapshot.symbol} {snapshot.timeframe}")
            return signals
        for index, result in zip(pending, results):
            rejected = result.get('error')
            if rejected:
                snapshot = snapshots[index]
                log.error(
                    f"analyze | {self.name} {snapshot.symbol} {snapshot.timeframe} — {rejected}"
                )
                continue
            signals[index] = self._finalize(
                TradeSignal(**result['signals'][0]) if result['signals'] else None)
        return signals


def discover(disabled=(), version_set=None, *, store=None, in_process=False):
    """Explicit version_set is an already pinned internal execution request."""
    store = store or open_pattern_store()
    versions = PatternVersions(store).resolve(disabled=disabled) if version_set is None else dict(version_set)
    found = []
    for pattern_id, version_id in versions.items():
        if pattern_id in disabled:
            continue
        instance = VersionPattern(version_id, store, in_process=in_process)
        if instance.name != pattern_id or instance.skipped:
            raise EditError('Pinned pattern identity or availability mismatch')
        found.append(instance)
    return found


def worker_spec(pattern):
    if isinstance(pattern,VersionPattern):
        return ('version:'+pattern.pattern_version_id,pattern.name)
    raise EditError('Runtime workers require a pinned PostgreSQL version')


def acknowledge_worker(worker, stopped=False):
    """Publish scan-boundary version set; stopped workers cannot hold activation."""
    import os
    from core.pattern_edit_store import now
    worker_id=getattr(worker,'_pattern_worker_id',None) or uid()
    worker._pattern_worker_id=worker_id
    payload={'worker_id':worker_id,'pid':os.getpid(),'stopped':stopped,
             'versions':getattr(worker,'_version_set',{}),'heartbeat':now()}
    open_pattern_store().save_worker(payload)
