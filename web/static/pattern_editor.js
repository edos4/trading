/* Shared-service client. No credentials, source writes or eligibility decisions. */
window.PatternEditor = (() => {
  let data, session, selections=[], timer=null, epoch=0, busy=false;
  const el=()=>document.getElementById('pattern-editor');
  async function request(url,body,method='POST') {
    const response=await fetch(url,{method,headers:{'Content-Type':'application/json'},...(method==='GET'?{}:{body:JSON.stringify(body||{})})});
    const result=await response.json();if(!response.ok)throw Error(result.detail||'Request failed');return result;
  }
  const status=text=>{const node=document.getElementById('edit-status');if(node)node.textContent=text;};
  async function action(fn){if(busy)return;busy=true;try{await fn();}catch(e){status(e.message);}finally{busy=false;}}
  function close(){epoch++;clearInterval(timer);timer=null;session=null;}
  function open(payload){
    close();data=payload;selections=[];const host=el();if(!host)return;
    host.hidden=!data.edit_context;if(host.hidden)return;
    host.innerHTML=`<h3>Edit Pattern</h3><details><summary>How to edit</summary><p>Right-click a candle or drag a labeled point. Describe a general detection or trade-rule change. Generate Preview, review the executed chart, explanation, diff and checks, then Apply. New versions govern future signals; existing positions keep their rules.</p></details>
      <p id="edit-status">Select a candle to begin</p><label>Role <input id="edit-role" value="RS" size="9"></label>
      <label>Snap <select id="edit-snap"><option>close</option><option>high</option><option>low</option><option>open</option></select></label>
      <pre id="edit-selection" style="white-space:pre-wrap"></pre><textarea id="edit-chat" rows="4" style="width:100%" placeholder="Prefer this later peak as the right shoulder…"></textarea>
      <button id="edit-send">Send instruction</button> <button id="edit-preview">Generate Preview</button>
      <div><button id="edit-original">Original</button> <button id="edit-baseline">Baseline</button> <button id="edit-candidate">Candidate</button></div>
      <pre id="edit-review" style="white-space:pre-wrap;max-height:280px;overflow:auto"></pre>
      <button id="edit-apply" disabled>Apply</button> <button id="edit-cancel">Cancel</button> <button id="edit-discard">Discard</button>
      <button id="edit-history">Version History</button> <button id="edit-rollback">Rollback</button> <button id="edit-reopen">Reopen saved draft</button>
      <details><summary>Broader Comparison — optional</summary><input id="edit-symbols" placeholder="Symbols separated by commas"><input id="edit-start" type="date"><input id="edit-end" type="date"><button id="edit-compare">Compare</button><p id="edit-comparison">Not run</p></details>`;
    const bind=(id,fn)=>document.getElementById(id).onclick=()=>action(fn);
    bind('edit-send',async()=>{await ensure();const text=document.getElementById('edit-chat').value;const image=window.TVChart.capture();
      show(await request(`/api/pattern-edits/${session.session_id}/messages`,{text,selections,image}));selections=[];document.getElementById('edit-chat').value='';});
    bind('edit-preview',async()=>{await ensure();await request(`/api/pattern-edits/${session.session_id}/preview`,{idempotency_key:crypto.randomUUID()});status('Generating…');});
    bind('edit-cancel',async()=>show(await request(`/api/pattern-edits/${session.session_id}/cancel`)));
    bind('edit-discard',async()=>show(await request(`/api/pattern-edits/${session.session_id}`,{},'DELETE')));
    bind('edit-apply',async()=>show(await request(`/api/pattern-edits/${session.session_id}/apply`,{
      revision_id:session.current_revision_id,report_id:session.report_id,candidate_sha256:session.revision.candidate_sha256,idempotency_key:session.current_revision_id})));
    bind('edit-history',async()=>{const rows=await request(`/api/patterns/${data.edit_context.pattern_id}/versions`,null,'GET');document.getElementById('edit-review').textContent=rows.map(v=>`v${v.version_number} ${v.description}\n${v.version_id}`).join('\n\n');});
    bind('edit-rollback',async()=>{await ensure();const rows=await request(`/api/patterns/${data.edit_context.pattern_id}/versions`,null,'GET');
      if(!rows.length){status('No versions to restore');return;}
      const choice=prompt('Version number to restore:\n'+rows.map(v=>`${v.version_number}: ${v.description}`).join('\n'));
      if(!choice)return;const target=rows.find(v=>String(v.version_number)===choice.trim());
      if(!target){status('Unknown version number');return;}
      const reason=prompt('Reason for restoring v'+choice.trim()+':');if(!reason)return;
      show(await request(`/api/patterns/${data.edit_context.pattern_id}/rollback`,{session_id:session.session_id,version_id:target.version_id,reason}));});
    bind('edit-reopen',async()=>{const rows=await request(`/api/pattern-edits?trade_id=${encodeURIComponent(data.edit_context.trade_id)}`,null,'GET');if(rows.length){session=rows[rows.length-1];await refresh();}else status('No saved drafts');});
    bind('edit-compare',async()=>{await request(`/api/pattern-edits/${session.session_id}/compare`,{symbols:document.getElementById('edit-symbols').value.split(',').map(s=>s.trim()),start:document.getElementById('edit-start').value,end:document.getElementById('edit-end').value});});
    for(const kind of ['original','baseline','candidate'])bind('edit-'+kind,async()=>{const payload=kind==='original'?data:await request(`/api/pattern-edits/${session.session_id}/chart?kind=${kind}`,null,'GET');window.TVChart.mount(document.getElementById('paper-chart-host'),payload,{onEdit:kind==='original'?select:null});});
    timer=setInterval(()=>{if(session&&!busy)action(refresh);},1000);
  }
  async function ensure(){if(!session)session=await request('/api/pattern-edits',{trade_id:data.edit_context.trade_id,market:data.edit_context.market});}
  function select(bar,role){
    if(!data?.edit_context)return;
    if(role)document.getElementById('edit-role').value=({'Right shoulder':'RS','Left shoulder':'LS','Head':'HEAD','Entry':'entry'})[role]||role;
    const index=data.edit_context.dataset.candles.findIndex(c=>c.time===bar.time);if(index<0)return;
    const selected={dataset_index:index,role:document.getElementById('edit-role').value,snap:document.getElementById('edit-snap').value};
    selections.push(selected);document.getElementById('edit-selection').textContent=`${selected.role} → ${bar.time} ${selected.snap}=${bar[selected.snap]}\nO ${bar.open} H ${bar.high} L ${bar.low} C ${bar.close}`;
    action(async()=>{await ensure();await refresh();});
  }
  function show(value){session=value;status(`${session.pattern_id} · ${session.state}\nHistorical: ${session.trade.pattern_version_id||'legacy/unknown'} · Base: ${session.base_version_id}`);
    document.getElementById('edit-review').textContent=[...session.messages.map(m=>m.text),session.revision?.description,session.revision?.explanation,session.diff,JSON.stringify(session.report?.checks||[],null,2),session.error].filter(Boolean).join('\n\n');
    document.getElementById('edit-apply').disabled=!(session.state==='ready'&&session.report?.ready);
    document.getElementById('edit-comparison').textContent=session.comparison?'Comparison available':'Not run';
  }
  async function refresh(){const start=epoch;const value=await request(`/api/pattern-edits/${session.session_id}`,null,'GET');if(start===epoch)show(value);}
  return {open,close,select};
})();
