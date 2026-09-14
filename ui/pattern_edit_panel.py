"""Native editor controls; all service/provider work runs off Tk's event thread."""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import queue
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from PIL import Image

from core.pattern_edit_service import get_pattern_edit_service, HOW_TO_EDIT


class PatternEditPanel(ttk.Frame):
    def __init__(self, parent, chart, payload):
        super().__init__(parent,width=370)
        self.chart, self.original = chart,payload
        self.session = None
        self.selections = []
        self.events = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=1,thread_name_prefix='pattern-edit-ui')
        self.alive = True
        self.busy = False
        self.status = tk.StringVar(value='Right-click a candle to start editing')
        self.role = tk.StringVar(value='RS')
        self.snap = tk.StringVar(value='close')
        ttk.Label(self,text='Edit Pattern',font=('TkDefaultFont',12,'bold')).pack(anchor='w',padx=8,pady=6)
        ttk.Button(self,text='How to edit',command=lambda:messagebox.showinfo('How to edit',HOW_TO_EDIT,parent=self)).pack(fill='x')
        ttk.Label(self,textvariable=self.status,wraplength=350).pack(fill='x',pady=6)
        row = ttk.Frame(self); row.pack(fill='x')
        ttk.Combobox(row,textvariable=self.role,values=['LS','LN','HEAD','RN','RS','entry','stop','target'],width=12).pack(side='left')
        ttk.Combobox(row,textvariable=self.snap,values=['open','high','low','close'],state='readonly',width=10).pack(side='left')
        self.selection_text = tk.StringVar()
        ttk.Label(self,textvariable=self.selection_text,wraplength=350).pack(fill='x')
        self.chat = tk.Text(self,height=4,width=44,wrap='word'); self.chat.pack(fill='x',pady=4)
        buttons = ttk.Frame(self);buttons.pack(fill='x')
        self.send = ttk.Button(buttons,text='Send instruction',command=self.message);self.send.pack(side='left')
        self.generate = ttk.Button(buttons,text='Generate Preview',command=self.preview);self.generate.pack(side='left')
        self.review = tk.Text(self,width=44,height=17,wrap='word');self.review.pack(fill='both',expand=True)
        scroll = ttk.Scrollbar(self,command=self.review.yview);self.review.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y')
        views=ttk.Frame(self);views.pack(fill='x')
        for label,kind in [('Original','historical'),('Baseline','baseline'),('Candidate','candidate')]:
            ttk.Button(views,text=label,command=lambda k=kind:self.show_preview(k)).pack(side='left')
        controls=ttk.Frame(self);controls.pack(fill='x',pady=4)
        self.apply_button=ttk.Button(controls,text='Apply',state='disabled',command=self.apply);self.apply_button.pack(side='left')
        ttk.Button(controls,text='Cancel',command=lambda:self.action('cancel')).pack(side='left')
        ttk.Button(controls,text='Discard',command=lambda:self.action('cancel',True)).pack(side='left')
        ttk.Button(self,text='Version History',command=self.history).pack(fill='x')
        ttk.Button(self,text='Rollback…',command=self.rollback).pack(fill='x')
        ttk.Button(self,text='Reopen saved draft',command=self.reopen).pack(fill='x')
        ttk.Button(self,text='Broader Comparison',command=self.compare).pack(fill='x')
        self.bind('<Destroy>',self._destroy,add='+')
        self.after(150,self.poll)

    def _destroy(self,event):
        if event.widget is self:
            self.alive=False
            self.pool.shutdown(wait=False,cancel_futures=True)

    def work(self,fn,callback=None):
        if self.busy:
            return
        self.busy=True
        self.status.set('Working…')
        def execute():
            try:
                self.events.put((callback,fn(),None))
            except Exception as exc:
                self.events.put((None,None,str(exc)))
        self.pool.submit(execute)

    def select(self,row,role=None):
        dataset=self.original['edit_context']['dataset']['candles']
        index=next((i for i,c in enumerate(dataset) if c['time']==row['time']),None)
        if index is None:
            return
        if role:
            mapping={'Right shoulder':'RS','Left shoulder':'LS','Head':'HEAD','Entry':'entry'}
            self.role.set(mapping.get(role,role))
        selection={'dataset_index':index,'role':self.role.get(),'snap':self.snap.get()}
        self.selections.append(selection)
        self.selection_text.set(f"{selection['role']} → {row['time']} {selection['snap']}={row[selection['snap']]:g}\nO {row['open']:g} H {row['high']:g} L {row['low']:g} C {row['close']:g}")
        self.chart._edit_selected=row['time'];self.chart._redraw()
        if not self.session:
            self.work(lambda:get_pattern_edit_service().create(self.original),self.received)

    def received(self,state):
        if 'session_id' not in state:
            return
        self.session=state
        self.status.set(f"{state['pattern_id']} · historical {state['trade'].get('pattern_version_id') or 'legacy/unknown'}\n{state['state']} · base {state['base_version_id'][:10]}")
        revision=state.get('revision') or {}
        report=state.get('report') or {}
        text='\n\n'.join(m['text'] for m in state['messages'])
        text+='\n\n'+revision.get('description','')+'\n'+revision.get('explanation','')+'\n'+state.get('diff','')
        text+='\n\n'+json.dumps(report.get('checks',[]),indent=2)
        if state.get('error'):text+='\n'+state['error']
        text+='\nBroader comparison: '+('Available' if state.get('comparison') else 'Not run')
        self.review.delete('1.0','end');self.review.insert('1.0',text)
        self.apply_button.configure(state='normal' if state['state']=='ready' and report.get('ready') else 'disabled')

    def message(self):
        if not self.session:
            self.status.set('Select a candle first');return
        text=self.chat.get('1.0','end').strip()
        if not text:return
        # Tk calls happen here; rasterization and service writes run off-thread.
        canvas=self.chart._canvas
        postscript=canvas.postscript(colormode='color',x=0,y=0,width=canvas.winfo_width(),height=canvas.winfo_height())
        selections=list(self.selections)
        session_id=self.session['session_id']
        def send():
            image=Image.open(io.BytesIO(postscript.encode()));image.load(scale=2)
            stream=io.BytesIO();image.save(stream,format='PNG')
            return get_pattern_edit_service().message(session_id,text,selections,base64.b64encode(stream.getvalue()).decode())
        self.work(send,self.received)
        self.selections=[];self.chat.delete('1.0','end')

    def preview(self):
        if self.session:
            self.work(lambda:get_pattern_edit_service().submit(self.session['session_id']),lambda _:self.status.set('Generating preview…'))

    def poll(self):
        if not self.alive:return
        try:
            callback,value,error=self.events.get_nowait()
            self.busy=False
            if error:self.status.set(error)
            elif callback:callback(value)
        except queue.Empty:pass
        if self.session and not self.busy and self.session.get('state') not in ('discarded','applied'):
            self.work(lambda:get_pattern_edit_service().read(self.session['session_id']),self.received)
        self.after(750,self.poll)

    def action(self,method,*args):
        if self.session:
            self.work(lambda:getattr(get_pattern_edit_service(),method)(self.session['session_id'],*args),self.received)

    def apply(self):
        if not self.session:return
        state=self.session;revision=state.get('revision') or {};report=state.get('report') or {}
        self.action('apply',revision.get('revision_id'),report.get('report_id'),revision.get('candidate_sha256'),revision.get('revision_id'))

    def history(self):
        pattern=self.original['edit_context']['pattern_id']
        self.work(lambda:get_pattern_edit_service().history(pattern),lambda rows:messagebox.showinfo('Version History','\n'.join(f"v{v['version_number']} {v['version_id']}\n{v['description']}" for v in rows) or 'No versions yet',parent=self))

    def rollback(self):
        # Rollback is a new reviewable draft (P8.5); it never activates directly.
        if not self.session:
            self.status.set('Open a session before rolling back');return
        def choose(rows):
            if not rows:
                self.status.set('No versions to restore');return
            listing='\n'.join(f"{v['version_number']}: {v['description']}" for v in rows)
            choice=simpledialog.askstring('Rollback','Version number to restore:\n'+listing,parent=self)
            if not choice:return
            target=next((v for v in rows if str(v['version_number'])==choice.strip()),None)
            if target is None:
                self.status.set('Unknown version number');return
            reason=simpledialog.askstring('Rollback','Reason for restoring v'+choice.strip()+':',parent=self)
            if not reason:return
            self.work(lambda:get_pattern_edit_service().rollback(self.session['session_id'],target['version_id'],reason),self.received)
        self.work(lambda:get_pattern_edit_service().history(self.original['edit_context']['pattern_id']),choose)

    def reopen(self):
        trade_id=self.original['edit_context']['trade_id']
        def pick(rows):
            if rows:self.work(lambda:get_pattern_edit_service().read(rows[-1]['session_id']),self.received)
            else:self.status.set('No saved drafts for this trade')
        self.work(lambda:get_pattern_edit_service().sessions(trade_id),pick)

    def show_preview(self,kind):
        if kind=='historical':self.chart.set_payload(self.original);return
        if self.session:
            self.work(lambda:get_pattern_edit_service().preview_chart(self.session['session_id'],kind),self.chart.set_payload)

    def compare(self):
        if not self.session:return
        symbols=simpledialog.askstring('Broader Comparison','Local symbols, separated by commas:',parent=self)
        if symbols:
            self.work(lambda:get_pattern_edit_service().compare_symbols(self.session['session_id'],symbols.split(',')),lambda _:None)
