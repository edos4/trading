"""Explicit immutable pattern loading. Applied source executes only in a sandbox."""
from __future__ import annotations

import importlib
import pkgutil

from core.pattern_edit_store import EditStore, ROOT, uid
from core.pattern_versions import PatternVersions
from core.pattern_provenance import rules
from patterns.base_pattern import BasePattern, TradeSignal, skip_pattern_module


def active_versions():
    if not (ROOT/'data/pattern_edit/registry.sqlite3').exists():
        return {}
    return EditStore().active_set()


def trusted_baseline(store, version):
    """Private snapshot modules for trusted baselines; shared interface identity."""
    import builtins
    import types
    import sys
    namespace = "_pattern_baseline_" + version['version_id'] + "_" + uid()
    modules = {}
    original_import = builtins.__import__

    def load(name):
        if name in modules:
            return modules[name]
        if name == "patterns":
            module = types.ModuleType(namespace)
            module.__path__ = []
            modules[name] = module
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
            raise ImportError("Relative imports require a versioned import adapter")
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
        if relative in version['files']:
            from core.pattern_edit_store import digest
            current = store.root / relative
            if not current.exists() or digest(current.read_bytes()) != version['files'][relative]['sha256']:
                raise ValueError('Baseline runtime dependency changed: ' + relative)
        return original_import(name,globals,locals,fromlist,level)

    module = load(version['source_path'][:-3].replace('/','.'))
    instance = next(c() for c in vars(module).values() if isinstance(c,type) and c is not BasePattern
                    and issubclass(c,BasePattern) and c.__module__==module.__name__)
    return instance, modules


class VersionPattern(BasePattern):
    def __init__(self, version_id, store=None):
        self.store = store or EditStore()
        self.version = PatternVersions(self.store).verify(version_id,runtime=True)
        self.pattern_version_id = version_id
        self.metadata = self.version.get('metadata')
        if self.metadata is None:
            # Baseline metadata is inspected from unchanged, trusted repository
            # source. Baselines are not activated edits, and use ordinary imports.
            if self.version['parent_version_id'] is not None:
                raise ValueError('Validated version metadata is missing')
            self._baseline, self._baseline_modules = trusted_baseline(self.store,self.version)
            self.metadata = {'name':self._baseline.name,'timeframes':self._baseline.timeframes,
                             **{k:getattr(self._baseline,k,d) for k,d in
                                [('MIN_BARS',2),('HORIZON_BARS',5),('MAX_OPEN_PER_SYMBOL',None)]}}
        else:
            self._baseline = None
        for key in ('MIN_BARS','HORIZON_BARS','MAX_OPEN_PER_SYMBOL'):
            setattr(self,key,self.metadata[key])

    @property
    def name(self):
        return self.version['pattern_id']

    @property
    def timeframes(self):
        return self.metadata['timeframes']

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
            signal.pattern_version_id = self.pattern_version_id
            signal.signal_id = signal.signal_id or uid()
            signal.provenance = 'versioned'
            signal.requested_rules = rules(signal,max_open_per_symbol=self.MAX_OPEN_PER_SYMBOL,entry_mode='signal_close')
        return signal


def discover(disabled=(), version_set=None):
    import patterns
    versions = active_versions() if version_set is None else version_set
    found = []
    for module_info in pkgutil.iter_modules(patterns.__path__):
        if skip_pattern_module(module_info.name):
            continue
        pattern_id = 'pattern_'+module_info.name
        if pattern_id in disabled:
            continue
        if pattern_id in versions:
            instance = VersionPattern(versions[pattern_id])
            found.append(instance)
            continue
        module = importlib.import_module('patterns.'+module_info.name)
        for cls in vars(module).values():
            if isinstance(cls,type) and cls is not BasePattern and issubclass(cls,BasePattern) and cls.__module__==module.__name__:
                instance = cls()
                if not instance.skipped and instance.name not in disabled:
                    found.append(instance)
    return found


def worker_spec(pattern):
    if isinstance(pattern,VersionPattern):
        return ('version:'+pattern.pattern_version_id,pattern.name)
    return (type(pattern).__module__,type(pattern).__qualname__)


def acknowledge_worker(worker, stopped=False):
    """Publish scan-boundary version set; stopped workers cannot hold activation."""
    if not (ROOT/'data/pattern_edit/registry.sqlite3').exists():
        return
    import os
    from core.pattern_edit_store import canonical, now
    worker_id=getattr(worker,'_pattern_worker_id',None) or uid()
    worker._pattern_worker_id=worker_id
    payload={'worker_id':worker_id,'pid':os.getpid(),'stopped':stopped,
             'versions':getattr(worker,'_version_set',{}),'heartbeat':now()}
    with EditStore().transaction() as con:
        con.execute('INSERT OR REPLACE INTO workers VALUES(?,?,?)',(worker_id,now(),canonical(payload).decode()))
