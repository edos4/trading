"""Framework-independent, durable editing workflow shared by Tk and HTTP."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import difflib
import json
from pathlib import Path
import threading

from analysis.pattern_edit_ai import DeepSeekEditor, image_bytes
from core.pattern_edit_store import EditStore, EditError, Conflict, canonical, digest, now, uid, atomic_write
from core.pattern_versions import PatternVersions
from core.pattern_edit_validation import Validator, validate_candles
from core.pattern_edit_worker import Cancelled

HOW_TO_EDIT = '''Double-click an open or closed paper trade, then right-click a real candle and choose Edit Pattern.
Describe a general change, for example “Prefer this later peak as the right shoulder” or “Place the stop above its high”.
Select a semantic role and OHLC snap; select more candles or drag a labeled point. Generate Preview and review the actual
baseline/candidate results, explanation, code/document diff and required checks. Continue chatting to revise.
Broader Comparison is optional. Apply activates only the exact validated revision. Discard leaves active code unchanged.
Version History can create a rollback draft for preview and Apply. Existing trades keep their original rules.'''


class PatternEditService:
    def __init__(self, store=None, provider=None, validator=None):
        self.store = store or EditStore()
        self.versions = PatternVersions(self.store)
        self.provider = provider or DeepSeekEditor()
        self.validator = validator or Validator(self.store)
        self.executor = ThreadPoolExecutor(max_workers=2,thread_name_prefix='pattern-edit')
        self.owner = uid()
        self._cancels = {}
        self.versions.recover()

    def create(self, chart, actor='desktop'):
        context = chart.get('edit_context')
        if not context or not context.get('trade_id'):
            raise EditError('This chart has no unambiguous stored trade identity')
        version = self.versions.baseline(context['pattern_id'])
        dataset = deepcopy(context['dataset'])
        validate_candles(dataset['candles'])
        if not dataset['candles'] or len(dataset['candles'])>5000:
            raise EditError('Editing requires 1–5000 frozen candles')
        session = {'schema_version':1,'session_id':uid(),'pattern_id':context['pattern_id'],
                   'trade':{k:v for k,v in context.items() if k!='dataset'}, 'base_version_id':version['version_id'],
                   'source_mirror_sha256':version['content_sha256'],'dataset':self.store.blob(dataset),
                   'historical_chart':self.store.blob(chart),'messages':[], 'state':'draft',
                   'current_revision_id':None,'report_id':None,'job_ids':[], 'generation':0,
                   'actor':actor,'created_at':now(),'updated_at':now(),'error':None,'comparison':None}
        return self.store.save_session(session)

    def read(self, session_id):
        session = self.store.get('sessions',session_id)
        result = {**session,'instructions':HOW_TO_EDIT,'active_version_id':self.store.active_set().get(session['pattern_id'])}
        result['revision'] = self.store.get('revisions',session['current_revision_id']) if session.get('current_revision_id') else None
        result['report'] = self.store.get('reports',session['report_id']) if session.get('report_id') else None
        result['jobs'] = [self.store.get('jobs',j) for j in session['job_ids']]
        if result['revision']:
            result['diff'] = self.store.read_blob(result['revision']['patch']).decode()
        return result

    def sessions(self, trade_id):
        return [s for s in self.store.list('sessions') if s['trade']['trade_id']==trade_id and s['state']!='discarded']

    def select(self, session, selections):
        dataset = json.loads(self.store.read_blob(session['dataset']))
        anchors = []
        if len(selections)>32:
            raise EditError('Select at most 32 anchors per message')
        for selection in selections:
            index = selection.get('dataset_index')
            if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(dataset['candles']):
                raise EditError('Selected candle is outside the frozen historical dataset')
            row = dataset['candles'][index]
            snap = selection.get('snap','close')
            role = selection.get('role','')
            if snap not in ('open','high','low','close','price') or not isinstance(role,str) or not 0<len(role)<=60:
                raise EditError('Choose an anchor role and an OHLC/price snap')
            price = float(selection['price']) if snap=='price' else row[snap]
            canonical(price)
            anchors.append({'anchor_id':selection.get('anchor_id') or role, 'role':role,'snap':snap,'price':price,
                'origin':'user','candle':{'dataset_sha256':session['dataset']['sha256'],
                'symbol':dataset['symbol'],'market':dataset['market'],'timeframe':dataset['timeframe'],
                'dataset_index':index,'timestamp':row['timestamp'],'session_date':row['time']}})
        return anchors

    def message(self, session_id, text, selections=None, image=None):
        if not isinstance(text,str) or not text.strip() or len(text)>20000:
            raise EditError('Enter an instruction of 1–20000 characters')
        image_ref = self.store.blob(image_bytes(image),'image/png') if image else None
        with self.store.transaction() as con:
            session = self.store.get('sessions',session_id,con)
            if session['state'] in ('applied','applying','activation-pending','discarded'):
                raise Conflict('Create a new session to edit this state')
            anchors = self.select(session,selections or [])
            session['messages'].append({'message_id':uid(),'role':'user','text':text,'created_at':now(),
                                        'anchors':anchors,'images':[image_ref] if image_ref else []})
            old = session['generation']
            session.update(generation=old+1,state='draft',report_id=None,error=None,updated_at=now(),comparison=None)
            self.store.save_session(session,old,con)
        return self.read(session_id)

    def _job_update(self, job_id, **changes):
        with self.store.transaction() as con:
            job = self.store.get('jobs',job_id,con)
            job.update(changes,heartbeat=now())
            con.execute('UPDATE jobs SET payload=? WHERE id=?',(canonical(job).decode(),job_id))
        return job

    def submit(self, session_id, operation='preview', idempotency_key=None, datasets=None):
        if operation not in ('preview','compare'):
            raise EditError('Unknown editing operation')
        key = session_id+':'+operation+':'+(idempotency_key or uid())
        with self.store.transaction() as con:
            prior = con.execute('SELECT payload FROM jobs WHERE idempotency_key=?',(key,)).fetchone()
            if prior:
                return json.loads(prior[0])
            session = self.store.get('sessions',session_id,con)
            if session['state'] in ('applied','discarded','applying','activation-pending'):
                raise Conflict('Session cannot start another job')
            if any(self.store.get('jobs',j,con)['state'] in ('queued','running') for j in session['job_ids']):
                raise Conflict('A job is already running; cancel or wait')
            if operation=='preview' and not session['messages']:
                raise EditError('Describe the requested change first')
            if operation=='compare' and not session['current_revision_id']:
                raise EditError('Generate a candidate before comparing')
            job = {'job_id':uid(),'session_id':session_id,'operation':operation,'state':'queued',
                   'generation':session['generation'],'revision_id':session['current_revision_id'],
                   'progress':'Queued','error':None,'created_at':now(),'heartbeat':now(),'owner':self.owner,
                   'idempotency_key':key}
            con.execute('INSERT INTO jobs VALUES(?,?,?,?)',(job['job_id'],session_id,key,canonical(job).decode()))
            session['job_ids'].append(job['job_id'])
            session['state'] = 'generating' if operation=='preview' else session['state']
            self.store.save_session(session,session['generation'],con)
        event = threading.Event()
        self._cancels[job['job_id']] = event
        self.executor.submit(self._run,job,session,event,datasets)
        return job

    def _context(self, session, version):
        dataset = json.loads(self.store.read_blob(session['dataset']))
        if session['current_revision_id']:
            revision = self.store.get('revisions',session['current_revision_id'])
        else:
            revision = None
        context = {'pattern_id':version['pattern_id'],'base_version_id':version['version_id'],
                   'allowed_paths':version['mirror_paths'],'dataset':dataset,'original_trade':session['trade'],
                   'conversation':session['messages'],'source':{n:self.store.read_blob(r).decode() for n,r in version['files'].items()},
                   'previous_candidate':{n:self.store.read_blob(r).decode() for n,r in revision['files'].items()} if revision else None,
                   'previous_report':self.store.get('reports',session['report_id']) if session.get('report_id') else None}
        images = [self.store.read_blob(ref) for m in session['messages'][-4:] for ref in m['images']][-2:]
        return context,images,dataset

    def _run(self, job, session, cancel, datasets):
        try:
            self._job_update(job['job_id'],state='running',progress='Preparing frozen context')
            version = self.versions.verify(session['base_version_id'])
            if job['operation']=='compare':
                self._compare(job,session,version,cancel,datasets)
                return
            context,images,dataset = self._context(session,version)
            proposal = self.provider.generate(context,images,version['mirror_paths'])
            if cancel.is_set():
                raise Cancelled('Job cancelled')
            files = {name:self.store.blob(text.encode(),'text/plain') for name,text in proposal['files'].items()}
            patch = '\n'.join(''.join(difflib.unified_diff(self.store.read_blob(version['files'][name]).decode().splitlines(True),
                                proposal['files'][name].splitlines(True),fromfile=name,tofile=name)) for name in files)
            revision = {'schema_version':1,'revision_id':uid(),'session_id':session['session_id'],
                        'base_version_id':version['version_id'],'parent_revision_id':session['current_revision_id'],
                        'files':files,'patch':self.store.blob(patch.encode(),'text/x-diff'),
                        **{key:proposal[key] for key in ('description','explanation','unresolved_questions','provider',
                                                         'requested_model','returned_model','provider_request_id')},
                        'context_sha256':digest(canonical(context)),'context':self.store.blob(context),
                        'candidate_sha256':digest(canonical({'base':version['version_id'],'files':files})),
                        'created_at':now()}
            with self.store.transaction() as con:
                latest = self.store.get('sessions',session['session_id'],con)
                if latest['generation']!=job['generation'] or cancel.is_set():
                    raise Conflict('A newer instruction superseded this response')
                self.store.insert_revision(revision,con)
                latest.update(current_revision_id=revision['revision_id'],state='validating',report_id=None)
                self.store.save_session(latest,latest['generation'],con)
            self._job_update(job['job_id'],revision_id=revision['revision_id'],progress='Executing and validating preview')
            # Latest selection of each role wins; older messages remain in history.
            anchors = {a['anchor_id']:a for m in session['messages'] for a in m['anchors']}
            report = self.validator.validate(version,revision,dataset,list(anchors.values()),cancel)
            with self.store.transaction() as con:
                self.store.insert_report(report,con)
                latest = self.store.get('sessions',session['session_id'],con)
                if latest['generation']!=job['generation'] or cancel.is_set():
                    raise Conflict('A newer instruction superseded this validation')
                latest.update(report_id=report['report_id'],state='ready' if report['ready'] else 'needs-revision',updated_at=now())
                self.store.save_session(latest,latest['generation'],con)
            self._job_update(job['job_id'],state='complete',progress='Preview available')
        except Exception as exc:
            safe = str(exc) if isinstance(exc,EditError) else 'Editing job failed; draft preserved. Check Python service logs.'
            self._job_update(job['job_id'],state='cancelled' if isinstance(exc,Cancelled) else 'failed',error=safe,progress=safe)
            with self.store.transaction() as con:
                latest = self.store.get('sessions',session['session_id'],con)
                if latest['generation']==job['generation'] and latest['state'] not in ('discarded','applied'):
                    latest.update(state='cancelled' if isinstance(exc,Cancelled) else 'failed',error=safe)
                    self.store.save_session(latest,latest['generation'],con)
        finally:
            self._cancels.pop(job['job_id'],None)

    def cancel(self, session_id, discard=False):
        with self.store.transaction() as con:
            session = self.store.get('sessions',session_id,con)
            if session['state'] in ('applied','applying','activation-pending'):
                raise Conflict('Activation cannot be discarded')
            old = session['generation']
            session.update(generation=old+1,state='discarded' if discard else 'cancelled',report_id=None)
            for job_id in session['job_ids']:
                event = self._cancels.get(job_id)
                if event:
                    event.set()
                job = self.store.get('jobs',job_id,con)
                if job['state'] in ('queued','running'):
                    job.update(state='cancelled',error='Cancelled by user')
                    con.execute('UPDATE jobs SET payload=? WHERE id=?',(canonical(job).decode(),job_id))
            self.store.save_session(session,old,con)
        return self.read(session_id)

    def _compare(self, job, session, version, cancel, datasets):
        if not datasets or len(datasets)>20:
            raise EditError('Choose 1–20 frozen comparison datasets')
        revision = self.store.get('revisions',session['current_revision_id'])
        files = {n:self.store.read_blob(r) for n,r in revision['files'].items()}
        rows = []
        for dataset in datasets:
            if cancel.is_set():
                raise Cancelled('Comparison cancelled')
            before = self.validator.execute(version,{},dataset,cancel)
            after = self.validator.execute(version,files,dataset,cancel)
            rows.append({'dataset':self.store.blob(dataset),'symbol':dataset['symbol'],
                         'baseline':before,'candidate':after,'changed':canonical(before)!=canonical(after),
                         'added':max(0,len(after['signals'])-len(before['signals'])),
                         'removed':max(0,len(before['signals'])-len(after['signals']))})
            self._job_update(job['job_id'],progress=f'Compared {len(rows)}/{len(datasets)} datasets')
        with self.store.transaction() as con:
            latest = self.store.get('sessions',session['session_id'],con)
            if latest['generation']!=job['generation'] or cancel.is_set():
                raise Conflict('Comparison superseded by newer instructions')
            latest['comparison'] = self.store.blob({'revision_id':revision['revision_id'],'rows':rows})
            self.store.save_session(latest,latest['generation'],con)
        self._job_update(job['job_id'],state='complete',progress='Comparison available')

    def preview_chart(self, session_id, kind):
        session = self.read(session_id)
        if kind not in ('baseline','candidate'):
            raise EditError('Choose baseline or candidate')
        report = session.get('report') or {}
        result = (report.get('preview') or {}).get(kind)
        if result is None:
            raise EditError('Executable preview is unavailable; inspect validation checks')
        import pandas as pd
        from analysis.chart_renderer import build_trade_viewer_payload
        dataset = json.loads(self.store.read_blob(session['dataset']))
        frame = pd.DataFrame(dataset['candles'])
        frame.index = pd.to_datetime(frame.pop('timestamp'),utc=True)
        signal = result['signals'][0] if result['signals'] else {}
        payload = build_trade_viewer_payload(frame,symbol=dataset['symbol'],timeframe=dataset['timeframe'],
                    session_tz=dataset['session_timezone'],pattern=session['pattern_id'],
                    annotations=signal.get('chart_annotations',[]),action=signal.get('action'),
                    entry=signal.get('price'),stop=signal.get('stop_loss'),target=signal.get('take_profit'))
        payload['title'] = kind.title()+(' — no pattern detected' if not signal else ' — executed detector')
        return payload

    def compare_symbols(self, session_id, symbols, start=None, end=None):
        from data.history import load_daily_ohlcv_df
        from core.pattern_edit_validation import candle_rows
        from core.market import get_market
        if not 1<=len(symbols)<=20:
            raise EditError('Choose 1–20 symbols')
        session = self.store.get('sessions',session_id)
        market = session['trade']['market']
        timezone = get_market(market).session_tz
        datasets = []
        for symbol in symbols:
            symbol = str(symbol).strip().upper()
            frame = load_daily_ohlcv_df(symbol,tv_fallback=False,market=market)
            if frame is None:
                raise EditError('No local dataset for '+symbol)
            frame = frame.loc[start or frame.index[0]:end or frame.index[-1]]
            datasets.append({'symbol':symbol,'market':market,'timeframe':'1d',
                             'session_timezone':timezone,'candles':candle_rows(frame,timezone)})
        return self.submit(session_id,'compare',datasets=datasets)

    def history(self, pattern_id):
        return sorted(self.store.list('versions','pattern',pattern_id),key=lambda v:v['version_number'])

    def diff(self, left, right):
        a,b = self.versions.verify(left),self.versions.verify(right)
        if a['pattern_id']!=b['pattern_id']:
            raise EditError('Select versions of the same pattern')
        return '\n'.join(''.join(difflib.unified_diff(self.store.read_blob(a['files'][n]).decode().splitlines(True),
                       self.store.read_blob(b['files'][n]).decode().splitlines(True),fromfile=left+'/'+n,tofile=right+'/'+n)) for n in a['mirror_paths'])

    def rollback(self, session_id, version_id, reason):
        if not isinstance(reason,str) or not reason.strip():
            raise EditError('Describe the reason for restoring this version')
        current = self.store.get('sessions',session_id)
        restored = self.versions.verify(version_id,runtime=True)
        if restored['pattern_id']!=current['pattern_id']:
            raise EditError('Rollback must select the same pattern')
        chart = json.loads(self.store.read_blob(current['historical_chart']))
        draft = self.create(chart,current['actor'])
        base = self.versions.verify(draft['base_version_id'])
        files = {name:restored['files'][name] for name in restored['mirror_paths']}
        patch = self.diff(base['version_id'],restored['version_id'])
        revision = {'schema_version':1,'revision_id':uid(),'session_id':draft['session_id'],
                    'base_version_id':base['version_id'],'parent_revision_id':None,'files':files,
                    'dependencies':restored['files'],'restored_from_version_id':version_id,
                    'description':'Restore v'+str(restored['version_number'])+': '+reason,
                    'explanation':reason,'unresolved_questions':[],'provider':None,
                    'requested_model':None,'returned_model':None,'provider_request_id':None,
                    'patch':self.store.blob(patch.encode(),'text/x-diff'),'created_at':now(),
                    'candidate_sha256':digest(canonical({'base':base['version_id'],'files':files,'dependencies':restored['files']}))}
        with self.store.transaction() as con:
            self.store.insert_revision(revision,con)
            draft.update(current_revision_id=revision['revision_id'],state='draft')
            self.store.save_session(draft,0,con)
        return self.read(draft['session_id'])

    def apply(self, session_id, revision_id, report_id, candidate_sha256, idempotency_key):
        if not idempotency_key:
            raise EditError('Apply requires an idempotency key')
        key=session_id+':apply:'+idempotency_key
        with self.store.transaction() as con:
            prior=con.execute('SELECT payload FROM activations WHERE idempotency_key=?',(key,)).fetchone()
            if prior:
                journal=json.loads(prior[0])
                if journal['revision_id']!=revision_id or journal['validation_report_id']!=report_id:
                    raise Conflict('Idempotency key already belongs to a different revision')
            else:
                session=self.store.get('sessions',session_id,con)
                if session['current_revision_id']!=revision_id or session.get('report_id')!=report_id:
                    raise Conflict('The displayed revision/report is stale')
                revision=self.store.get('revisions',revision_id,con)
                report=self.store.get('reports',report_id,con)
                if session['state']!='ready' or not report.get('ready') or report['candidate_sha256']!=candidate_sha256 or revision['candidate_sha256']!=candidate_sha256:
                    raise EditError('Required validation has not passed; Apply is unavailable')
                if not report.get('checks') or any(c['required'] and c['outcome']!='passed' for c in report['checks']):
                    raise EditError('A required validation check did not pass')
                from core import pattern_edit_validation
                if report['validator_sha256']!=digest(Path(pattern_edit_validation.__file__).read_bytes()):
                    raise Conflict('Validator changed; regenerate validation')
                base=self.versions.verify(session['base_version_id'],runtime=True)
                if self.store.active_set(con).get(session['pattern_id'])!=base['version_id'] or not self.versions.mirror_matches(base):
                    raise Conflict('Active version/source changed; refresh the draft and preview')
                if report['dataset_sha256']!=session['dataset']['sha256'] or report['dependency_sha256']!=digest(canonical(base['files'])):
                    raise Conflict('Validation input hashes no longer match')
                pending=con.execute('SELECT payload FROM activations WHERE pattern=?',(session['pattern_id'],)).fetchall()
                if any(json.loads(r[0])['state'] in ('staged','registered') for r in pending):
                    raise Conflict('An activation is awaiting recovery')
                number=con.execute('SELECT MAX(number)+1 FROM versions WHERE pattern=?',(session['pattern_id'],)).fetchone()[0]
                version={**base,'version_id':uid(),'version_number':number,'parent_version_id':base['version_id'],
                         'restored_from_version_id':revision.get('restored_from_version_id'),
                         'created_at':now(),'actor':session['actor'],'description':revision['description'],
                         'explanation':revision['explanation'],'files':{**revision.get('dependencies',base['files']),**revision['files']},
                         'candidate_sha256':candidate_sha256,'session_id':session_id,'validation_report_id':report_id,
                         'metadata':report['preview']['candidate']['metadata']}
                version['content_sha256']=digest(canonical(version['files']))
                for ref in version['files'].values():self.store.read_blob(ref)
                atomic_write(self.store.directory/session['pattern_id']/version['version_id']/'manifest.json',canonical(version))
                con.execute('INSERT INTO versions VALUES(?,?,?,?)',(version['version_id'],session['pattern_id'],number,canonical(version).decode()))
                workers=[r['id'] for r in con.execute('SELECT id FROM workers')]
                journal={'activation_id':uid(),'pattern_id':session['pattern_id'],'previous_version_id':base['version_id'],
                         'registered_version_id':version['version_id'],'revision_id':revision_id,'validation_report_id':report_id,
                         'idempotency_key':key,'state':'staged','required_worker_ids':workers,'acknowledged_worker_ids':[],
                         'stopped_worker_ids':[],'error':None,'session_id':session_id}
                con.execute('INSERT INTO activations VALUES(?,?,?,?)',(journal['activation_id'],session['pattern_id'],key,canonical(journal).decode()))
                session['state']='applying';self.store.save_session(session,session['generation'],con)
        self.versions.recover()
        return self.activation_status(journal['activation_id'])

    def activation_status(self, activation_id):
        with self.store.transaction() as con:
            journal=self.store.get('activations',activation_id,con)
            workers={r['id']:json.loads(r['payload']) for r in con.execute('SELECT id,payload FROM workers')}
            acknowledged=[];stopped=[]
            import os
            for worker_id in journal['required_worker_ids']:
                worker=workers.get(worker_id)
                if not worker or worker.get('stopped'):
                    stopped.append(worker_id);continue
                try:os.kill(worker['pid'],0)
                except ProcessLookupError:stopped.append(worker_id);continue
                if worker.get('versions',{}).get(journal['pattern_id'])==journal['registered_version_id']:
                    acknowledged.append(worker_id)
            journal.update(acknowledged_worker_ids=acknowledged,stopped_worker_ids=stopped)
            journal['state']='active' if len(acknowledged)+len(stopped)==len(journal['required_worker_ids']) else 'worker-pending'
            con.execute('UPDATE activations SET payload=? WHERE id=?',(canonical(journal).decode(),activation_id))
            session=self.store.get('sessions',journal['session_id'],con)
            session['state']='applied' if journal['state']=='active' else 'activation-pending'
            self.store.save_session(session,session['generation'],con)
        return {**self.read(journal['session_id']),'activation':journal}


_service = None
_service_lock = threading.Lock()


def get_pattern_edit_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = PatternEditService()
        return _service
