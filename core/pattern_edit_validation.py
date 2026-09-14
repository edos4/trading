"""Trusted validation coordinator; candidate imports belong only to the worker."""
from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path
import re

from core.pattern_edit_store import EditError, canonical, digest, now, uid
from core.pattern_edit_worker import CandidateRunner, SandboxUnavailable
from patterns.base_pattern import TradeSignal


def check(name, outcome, detail, required=True):
    return {'name':name,'outcome':outcome,'detail':detail,'required':required}


def candle_rows(frame, session_timezone):
    rows = []
    for ts,row in frame.iterrows():
        import pandas as pd
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(session_timezone)
        rows.append({'timestamp':stamp.isoformat(),'time':str(stamp.date()),
                     **{key:float(row[key]) for key in ('open','high','low','close','volume')}})
    return rows


def validate_candles(rows):
    """Reject unusable frozen data without over-constraining vendor OHLC.

    The P0 contract requires finite values and ordered, unique, timezone-aware
    timestamps. It does not require low <= open/close <= high: real feeds
    contain split-rounded and stale sub-penny bars that violate intra-bar
    ordering, and rejecting them would make the editor unusable on real charts.
    """
    from datetime import datetime
    import math
    previous = None
    for row in rows:
        ts = datetime.fromisoformat(row['timestamp'])
        if ts.tzinfo is None or (previous and ts<=previous):
            raise EditError('Dataset timestamps must be unique, ordered and timezone aware')
        previous = ts
        values = [row[key] for key in ('open','high','low','close','volume')]
        if any(not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) for value in values):
            raise EditError('Dataset contains a non-finite OHLCV value')
        if row['low']<=0 or row['high']<row['low'] or row['volume']<0:
            raise EditError('Invalid OHLCV candle')
    canonical(rows)  # rejects non-finite numbers


class Validator:
    def __init__(self, store, runner=None):
        self.store = store
        self.runner = runner or CandidateRunner()

    def inputs(self, version, files):
        inputs = {name:self.store.read_blob(ref) for name,ref in version['files'].items()}
        inputs.update(files)
        # Fixed trusted harness is never part of the provider file allowlist.
        for name in ('core/pattern_edit_evaluator.py',):
            inputs[name] = (Path(__file__).resolve().parents[1]/name).read_bytes()
        return inputs

    def execute(self, version, files, dataset, cancel=None):
        validate_candles(dataset['candles'])
        request = {**dataset,'source_path':version['source_path'],'pattern_id':version['pattern_id']}
        result = self.runner.run(self.inputs(version,files),request,cancel)
        if not isinstance(result,dict) or not isinstance(result.get('signals'),list):
            raise EditError('Worker returned malformed preview')
        if len(result['signals'])>8:
            raise EditError('Worker returned too many signals')
        allowed = {f.name for f in fields(TradeSignal)}
        for signal in result['signals']:
            if not isinstance(signal,dict) or set(signal)-allowed or signal.get('pattern')!=version['pattern_id']:
                raise EditError('Worker returned an incompatible signal')
            canonical(signal)
        return result

    def validate(self, version, revision, dataset, anchors, cancel=None):
        files = {name:self.store.read_blob(ref) for name,ref in revision['files'].items()}
        checks = []
        source = files[version['source_path']].decode()
        try:
            tree = ast.parse(source)
            checks.append(check('syntax','passed','Python source parses'))
            denied = any(isinstance(n,(ast.Import,ast.ImportFrom)) and (
                (isinstance(n,ast.Import) and any(a.name.split('.')[0] in {'os','subprocess','socket','ctypes','sys'} for a in n.names)) or
                (isinstance(n,ast.ImportFrom) and (n.module or '').split('.')[0] in {'os','subprocess','socket','ctypes','sys','core'})
            ) for n in ast.walk(tree))
            checks.append(check('scope','failed' if denied else 'passed','Executable engine/process imports are not permitted' if denied else 'No disallowed direct imports'))
            hardcoded = dataset['symbol'] in source or bool(re.search(r'20\d\d-\d\d-\d\d',source))
            checks.append(check('example-hardcoding','failed' if hardcoded else 'passed','Scoped symbol/date check; does not prove general semantics'))
        except SyntaxError as exc:
            checks.append(check('syntax','failed',f'Invalid Python at line {exc.lineno}'))
        if revision.get('unresolved_questions'):
            checks.append(check('questions','failed','Resolve provider questions before Apply'))
        baseline = candidate = None
        if all(c['outcome']=='passed' for c in checks):
            try:
                baseline = self.execute(version,{},dataset,cancel)
                candidate = self.execute(version,files,dataset,cancel)
                repeat = self.execute(version,files,dataset,cancel)
                checks.append(check('execution','passed','Baseline and candidate ran on identical causal prefixes'))
                checks.append(check('determinism','passed' if canonical(candidate)==canonical(repeat) else 'failed','Repeated identical frozen input'))
                markers = [a for s in candidate['signals'] for a in s.get('chart_annotations',[]) if a.get('type')=='marker']
                for anchor in anchors:
                    found = any(a.get('date')==anchor['candle']['session_date'] and
                                abs(float(a.get('price',0))-anchor['price'])<1e-7 and
                                str(a.get('label','')).lower()==anchor['role'].lower() for a in markers)
                    checks.append(check('anchor:'+anchor['anchor_id'],'passed' if found else 'failed','Expected semantic marker on selected candle/price'))
            except SandboxUnavailable as exc:
                checks.append(check('sandbox','unavailable',str(exc)))
            except EditError as exc:
                checks.append(check('execution','failed',str(exc)))
        tests_hash = None
        if candidate is not None:
            from core.pattern_versions import collect_sources
            trusted_root=Path(__file__).resolve().parents[1]
            test_paths=['core/test_execution_accounting.py','analysis/test_pattern_overlays.py']
            test_files={}
            for name in test_paths:
                test_files.update(collect_sources(trusted_root,name,paired=False))
            tests_hash=digest(canonical({n:digest(v) for n,v in test_files.items()}))
            try:
                inputs=self.inputs(version,files)
                # Trusted test dependencies cannot overwrite selected draft files.
                inputs.update({n:v for n,v in test_files.items() if n not in inputs})
                tests=self.runner.run(inputs,{'operation':'tests','test_paths':['/input/'+n for n in test_paths]},cancel)
                checks.append(check('trusted-regressions','passed' if tests.get('tests_passed') else 'failed',
                                    'Trusted execution-accounting and chart-overlay suites'))
            except SandboxUnavailable as exc:
                checks.append(check('trusted-regressions','unavailable',str(exc)))
            except EditError as exc:
                checks.append(check('trusted-regressions','failed',str(exc)))
            document=files[str(Path(version['source_path']).with_suffix('.md'))].decode().strip()
            agreement=bool(document and revision.get('explanation'))
            for signal in candidate['signals']:
                for annotation in signal.get('chart_annotations',[]):
                    if annotation.get('type')=='hline':
                        label=annotation.get('label','').lower()
                        field='take_profit' if label in ('target','take profit') else 'stop_loss' if label in ('stop','stop loss') else None
                        if field and signal.get(field) is not None:
                            agreement &= abs(float(annotation['price'])-signal[field])<1e-4
            checks.append(check('rule-document-agreement','passed' if agreement else 'failed',
                                'Paired rules/explanation present; labeled stop/target annotations match signal fields. Semantic prose correctness still requires review.'))
        else:
            checks.append(check('trusted-regressions','unavailable','Executable candidate is required before regression validation'))
            checks.append(check('rule-document-agreement','unavailable','Executable signals are required for annotation/rule checks'))
        report = {'schema_version':1,'report_id':uid(),'revision_id':revision['revision_id'],
                  'candidate_sha256':revision['candidate_sha256'],'dataset_sha256':digest(canonical(dataset)),
                  'dependency_sha256':digest(canonical(version['files'])),
                  'validator_sha256':digest(Path(__file__).read_bytes()),
                  'tests_sha256':tests_hash,'checks':checks,'created_at':now(),
                  'preview':{'baseline':baseline,'candidate':candidate},'comparison':None}
        report['ready'] = bool(candidate is not None) and all(c['outcome']=='passed' for c in checks if c['required'])
        return report
