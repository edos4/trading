"""Explicit immutable pattern loading. Applied source executes only in a sandbox."""
from __future__ import annotations

import importlib

from core.pattern_edit_store import EditError, open_pattern_store, uid
from core.pattern_versions import PatternVersions
from core.pattern_provenance import rules
from patterns.base_pattern import BasePattern, TradeSignal


def active_versions(store=None):
    return PatternVersions(store).resolve()


def trusted_baseline(store, version, *, check_runtime=True):
    """Private snapshot modules for trusted baselines; shared interface identity.

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

    module = load(version['source_path'][:-3].replace('/','.'))
    classes = [c for c in vars(module).values() if isinstance(c,type) and c is not BasePattern
               and issubclass(c,BasePattern) and c.__module__ == module.__name__]
    if len(classes) != 1:
        raise ValueError('Expected exactly one detector class')
    instance = classes[0]()
    weakref.finalize(instance, lambda: [sys.modules.pop(m.__name__, None) for m in modules.values()])
    return instance, modules


class VersionPattern(BasePattern):
    def __init__(self, version_id, store=None, *, runtime=True):
        self.store = store or open_pattern_store()
        self.version = PatternVersions(self.store).verify(version_id,runtime=runtime)
        self.pattern_version_id = version_id
        self.metadata = self.version.get('metadata')
        if self.version.get('provenance') in ('file-import', 'trusted-snapshot'):
            # Baseline metadata is inspected from unchanged, trusted repository
            # source. Baselines are not activated edits, and use ordinary imports.
            if self.version['parent_version_id'] is not None:
                raise ValueError('Validated version metadata is missing')
            self._baseline, self._baseline_modules = trusted_baseline(
                self.store, self.version, check_runtime=runtime)
            self.metadata = {'name':self._baseline.name,'timeframes':self._baseline.timeframes,
                             'skipped':self._baseline.skipped, 'chart_description':self._baseline.chart_description,
                             **{k:getattr(self._baseline,k,d) for k,d in
                                [('MIN_BARS',2),('HORIZON_BARS',5),('MAX_OPEN_PER_SYMBOL',None)]}}
        else:
            if self.metadata is None:
                raise EditError('Validated version metadata is missing')
            self._baseline = None
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

    def analyze(self, snapshot, store):
        if self._baseline is not None:
            from patterns import _dedup
            private = self._baseline_modules.get('patterns._dedup')
            if private is not None:
                private._used = set(_dedup._used)
                private._current = _dedup._current
                private._gen = _dedup._gen
            signal = self._baseline.analyze(snapshot,store)
        else:
            from core.pattern_edit_validation import Validator, candle_rows
            from patterns import _dedup
            frame = store.get_df(snapshot.symbol,snapshot.timeframe,min_bars=2)
            if frame is None:
                return None
            dataset = {'symbol':snapshot.symbol,'timeframe':snapshot.timeframe,'market':'us',
                       'session_timezone':getattr(store,'_session_tz','America/New_York'),
                       'candles':candle_rows(frame,getattr(store,'_session_tz','America/New_York')),
                       'operation':'analyze','current_bar':_dedup._current,'used_pivots':list(_dedup._used)}
            result = Validator(self.store).execute(self.version,{},dataset)
            signal = TradeSignal(**result['signals'][0]) if result['signals'] else None
        if signal:
            for annotation in signal.chart_annotations:
                annotation['pattern_version_id'] = self.pattern_version_id
            signal.pattern_version_id = self.pattern_version_id
            signal.signal_id = signal.signal_id or uid()
            signal.provenance = 'versioned'
            signal.requested_rules = rules(signal,max_open_per_symbol=self.MAX_OPEN_PER_SYMBOL,entry_mode='signal_close')
        return signal


def discover(disabled=(), version_set=None, *, store=None):
    """Explicit version_set is an already pinned internal execution request."""
    store = store or open_pattern_store()
    versions = PatternVersions(store).resolve(disabled=disabled) if version_set is None else dict(version_set)
    found = []
    for pattern_id, version_id in versions.items():
        if pattern_id in disabled:
            continue
        instance = VersionPattern(version_id, store)
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
