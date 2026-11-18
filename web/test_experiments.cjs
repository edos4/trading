// Exercise the actual client against a small fake DOM/API, including stale reads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.events = {}; this.dataset = {};
    this.attributes = {}; this.value = ''; this.hidden = false; this.disabled = false;
    this.textContent = ''; this.style = {}; this.classList = {toggle() {}};
  }
  addEventListener(name, callback) { this.events[name] = callback; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  get firstChild() { return this.children[0]; }
  setAttribute(key, value) { this.attributes[key] = value; }
  querySelector(selector) { return this.parts?.[selector]; }
  getContext() { return {scale(){},clearRect(){},fillText(){},beginPath(){},lineTo(){},moveTo(){},stroke(){}}; }
  set innerHTML(_) { throw new Error('Client must not render source text with innerHTML'); }
}
const elements = new Map();
const get = id => { if(!elements.has(id)) elements.set(id,new Element()); return elements.get(id); };
get('exp-table').parts = {thead:new Element('thead'), tbody:new Element('tbody')};
get('exp-data-errors').parts = {pre:new Element('pre')};
const tabs = ['positions','trades','signals','performance','outcomes','logs','source'].map(name => {
  const el = new Element('button'); el.dataset.expTab = name; return el;
});
Object.entries({'exp-market':'us','exp-start':'2020-01-01','exp-end':'2021-01-01','exp-capital':'100000',
  'exp-notional':'10000','exp-slippage':'0.05','exp-cost':'','exp-symbols':'','exp-end-policy':'keep-open','exp-filter':'all'}).forEach(([k,v])=>get(k).value=v);
const source = (id,number,extra={})=>({id,number,title:`Source ${number}`,availability:'ready',markets:['us','ph'],
  parameters:{},assumptions:[],limitations:[],kind:'trades',writeup:'source text',plan:'plan text',...extra});
const sources = [source('406-ema',406,{video_url:'/api/experiments/sources/406-ema/video'}),source('442-repaint',442,{availability:'blocked',blocker:'Repainting formula missing'}),
  source('385-candles',385,{kind:'event-study',title:'<img src=x onerror=alert(1)>'})];
const summary = pnl => ({signal_count:2,trade_count:1,open_count:0,coverage:{evaluated_symbols:1},
  variants: pnl == null ? {} : {book:{kind:'trades',net_pnl:pnl},other:{kind:'trades',net_pnl:pnl}},equity:{}});
const batch = {id:'batch-one',state:'completed-with-blockers',request:{market:'us',start_date:'2020-01-01',end_date:'2021-01-01'},
  progress:{total_symbols:1,completed_symbols:1},counts:{completed:2,blocked:1},
  runs:[{source:'406-ema',state:'completed',summary:summary(10)},{source:'442-repaint',state:'blocked',summary:null},
    {source:'385-candles',state:'completed',summary:summary(40)}]};
const calls = [];
const importJobs = [];
let deferred, poll, failPost = false, keyCounter = 0;
const positionRows = [];
async function api(url, options={}) {
  calls.push({url,options});
  if(url==='/api/experiments/imports' && !options.method) return {imports:importJobs.map(j=>({...j})),capabilities:{ready:true,message:'Ready'}};
  if(url==='/api/experiments/imports' && options.method==='POST') {
    if(!importJobs.length) importJobs.push({id:'url-new',url:JSON.parse(options.body).url,state:'queued',stage:'queued',message:'Waiting',files:[]});
    return {...importJobs[0]};
  }
  if(url.startsWith('/api/experiments/imports/')) {
    const job=importJobs.find(j=>j.id===url.split('/')[4]);
    if(url.endsWith('/retry')) Object.assign(job,{state:'queued',stage:'queued',error:null});
    if(url.endsWith('/cancel')) Object.assign(job,{state:'cancelled',stage:'cancelled'});
    return {...job};
  }
  if(url==='/api/experiments/catalog') return {sources};
  if(url==='/api/experiments/runs' && !options.method) return {batches:[]};
  if(url==='/api/experiments/runs' && options.method==='POST') {
    if(failPost) { failPost=false; throw new Error('simulated lost response'); }
    if(JSON.parse(options.body).source_ids) {
      const selected=sources.find(s=>s.id===JSON.parse(options.body).source_ids[0]);
      return {...batch,id:'selected-run',sources:[selected],runs:[{source:selected.id,state:'completed',summary:summary(5)}]};
    }
    return batch;
  }
  if(url.startsWith('/api/experiments/sources/')) {
    const id=url.split('/').at(-1);
    if(deferred?.id===id) return deferred.promise;
    return sources.find(s=>s.id===id);
  }
  if(url.endsWith('/cancel')) return {...batch,state:'cancelled',cancel:1};
  if(url.includes('/records?')) {
    if(url.includes('kind=positions')) return {rows: positionRows, next:null, total:positionRows.length};
    return {rows:[{cursor:1,symbol:'SPY',date:'2020-06-01',variant:'ema-10-50',status:'observed',evidence:{}}],next:null,total:1};
  }
  const id=url.split('/folders/')[1];
  if(id) return {source:sources.find(s=>s.id===id),run:batch.runs.find(s=>s.source===id)};
  return batch;
}
const context = {
  Date, Intl,
  document:{getElementById:get,createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),querySelectorAll:()=>tabs},
  window:{location:{href:'http://localhost/experiments',search:''},history:{replaceState(){}},addEventListener(){},devicePixelRatio:1},
  localStorage:{getItem(){return null;},setItem(){}},
  URL,URLSearchParams,crypto:{randomUUID:()=> 'browser-request-key-'+(++keyCounter)},api,
  setInterval(fn){poll=fn; return 1;},clearInterval(){},console,
};
const flush = async()=>{for(let i=0;i<8;i++) await new Promise(resolve=>setImmediate(resolve));};
const descendants = el => [el,...el.children.flatMap(descendants)];
const folder = id => descendants(get('exp-folders')).find(el=>el.tag==='button' && descendants(el).some(c=>c.textContent===id));
const folderIds = () => descendants(get('exp-folders')).filter(el=>el.tag==='button').map(el=>descendants(el).find(c=>c.tag==='small').textContent);
vm.runInNewContext(fs.readFileSync(__dirname+'/static/experiments.js','utf8'),context);

(async()=>{
  await flush();
  assert.match(get('exp-catalog-count').textContent,/3 folders · 2 runnable · 1 blocked/);
  assert.deepEqual(folderIds(),['406-ema','442-repaint','385-candles']);
  assert.equal(get('exp-title').textContent,'Source 406');
  assert.equal(get('exp-source-video').hidden,false);
  assert.equal(get('exp-source-video').src,'/api/experiments/sources/406-ema/video');
  assert.equal(get('exp-watch').hidden,false);
  assert.equal(get('exp-video-empty').hidden,true);
  assert.ok(descendants(get('exp-writeup')).some(el => el.textContent === 'source text'));
  await folder('442-repaint').events.click(); await flush();
  assert.equal(get('exp-source-video').hidden,true);
  assert.equal(get('exp-source-video').dataset.source,'');
  assert.equal(get('exp-watch').hidden,true);
  assert.equal(get('exp-video-empty').hidden,false);
  get('exp-doc-find').value = 'source';
  await get('exp-doc-find').events.input();
  assert.ok(descendants(get('exp-writeup')).some(el => el.tag === 'mark' && el.textContent === 'source'));
  assert.match(get('exp-doc-count').textContent,/1 match/);
  await get('exp-doc-plan').events.click();
  assert.equal(get('exp-writeup').hidden,true);
  assert.equal(get('exp-plan').hidden,false);
  assert.match(get('exp-doc-count').textContent,/No matches/);
  get('exp-doc-find').value = '';
  await get('exp-doc-find').events.input();
  assert.equal(get('exp-doc-count').textContent,'');
  await get('exp-doc-writeup').events.click();
  assert.equal(get('exp-writeup').hidden,false);
  assert.equal(get('exp-blocker').textContent,'Repainting formula missing');
  assert.equal(get('exp-folder-state').textContent,'blocked');
  await folder('385-candles').events.click(); await flush();
  assert.equal(get('exp-title').textContent,'<img src=x onerror=alert(1)>');
  assert.ok(!descendants(get('exp-selected')).some(el=>el.tag==='img'));

  // A late source response must not replace a newer selection.
  let resolve;
  deferred={id:'442-repaint',promise:new Promise(r=>resolve=r)};
  const late=folder('442-repaint').events.click();
  await folder('406-ema').events.click(); await flush();
  resolve(sources[1]); await late; await flush(); deferred=null;
  assert.equal(get('exp-title').textContent,'Source 406');

  get('exp-start').value='2022-01-01';
  const before=calls.length;
  await get('exp-form').events.submit({preventDefault(){}});
  assert.equal(calls.length,before); assert.match(get('exp-error').textContent,/Start date/);
  get('exp-start').value='2020-01-01'; get('exp-symbols').value='SPY, AAPL';
  failPost=true;
  await get('exp-form').events.submit({preventDefault(){}}); await flush();
  await get('exp-form').events.submit({preventDefault(){}}); await flush();
  const posts=calls.filter(call=>call.options.method==='POST');
  assert.equal(posts.length,2);
  assert.equal(JSON.parse(posts[0].options.body).idempotency_key,JSON.parse(posts[1].options.body).idempotency_key);
  assert.equal(keyCounter,1);
  const posted=JSON.parse(calls.find(call=>call.options.method==='POST').options.body);
  assert.deepEqual(posted.symbols,['SPY','AAPL']); assert.equal(posted.market,'us');
  assert.equal(posted.start_date,'2020-01-01'); assert.equal(posted.end_date,'2021-01-01');
  assert.equal(posted.slippage_pct,.0005); assert.equal(posted.txn_cost_pct,null);
  assert.ok(!('experiment_id' in posted)); // Start schedules the entire catalog.
  assert.deepEqual(folderIds(),['406-ema','442-repaint','385-candles']);
  assert.ok(descendants(folder('406-ema')).some(el=>el.textContent==='2 variants'));
  batch.state='processing';
  batch.runs[0].summary.variants.duplicate = {...batch.runs[0].summary.variants.book};
  await poll(); await flush();
  assert.deepEqual(folderIds(),['406-ema','442-repaint','385-candles']);
  assert.ok(descendants(folder('406-ema')).some(el=>el.textContent==='3 variants'));
  batch.state='completed-with-blockers';
  await tabs.find(t=>t.dataset.expTab==='signals').events.click(); await flush();
  assert.match(get('exp-table-note').textContent,/1 of 1 rows/);
  assert.ok(calls.some(c=>c.url.includes('/folders/406-ema/records?kind=signals')));
  await folder('385-candles').events.click(); await flush();
  assert.match(get('exp-table-note').textContent,/Event study/);
  assert.equal(get('exp-export').hidden,false);
  // 2026-11-18 02:30 UTC is 21:30 in New York on 2026-11-17, after the cash close.
  context.window.__experimentClock = () => new Date('2026-11-18T02:30:00Z');
  Object.assign(batch.request, {market:'us', end_date:'2026-11-18', slippage_pct:0.0005, txn_cost_pct:null});
  positionRows.splice(0, positionRows.length,
    {cursor:1, symbol:'AA', action:'BUY', qty:10, current:100, unrealized_pnl:5,
     valuation:'stale; security status unresolved', mark_date:'2026-11-17', liquidation_pnl_estimate:null},
    {cursor:2, symbol:'OLD', action:'BUY', qty:1, current:10, unrealized_pnl:0,
     valuation:'stale; security status unresolved', mark_date:'2026-11-16', liquidation_pnl_estimate:null});
  await tabs.find(t=>t.dataset.expTab==='positions').events.click(); await flush();
  const cells = descendants(get('exp-table').parts.tbody).filter(el => el.tag==='td').map(el => el.textContent);
  assert.ok(cells.includes('last traded close'));
  assert.ok(cells.includes('stale; security status unresolved'));
  assert.ok(cells.some(text => text.startsWith('3.5')));
  get('exp-history').value=''; await get('exp-history').events.change(); await flush();
  assert.deepEqual(folderIds(),['406-ema','442-repaint','385-candles']);
  assert.equal(get('exp-progress').value,0);
  assert.equal(get('exp-frozen').hidden,true);
  assert.equal(get('exp-run-id').textContent,'');
  assert.equal(get('exp-export').hidden,true);
  // One URL schedules generation, retains errors/retry, then exposes a selected-source run.
  const runsBefore=calls.filter(c=>c.url==='/api/experiments/runs' && c.options.method==='POST').length;
  get('exp-import-url').value='https://youtube.com/watch?v=example';
  await get('exp-import-form').events.submit({preventDefault(){}}); await flush();
  assert.equal(get('exp-import-submit').disabled,false);
  assert.match(get('exp-import-status').textContent,/queued/);
  assert.deepEqual(JSON.parse(calls.find(c=>c.url==='/api/experiments/imports' && c.options.method==='POST').options.body),{url:'https://youtube.com/watch?v=example'});
  assert.equal(calls.filter(c=>c.url==='/api/experiments/runs' && c.options.method==='POST').length,runsBefore);
  Object.assign(importJobs[0],{state:'blocked',stage:'failed',error:'Source not accessible'});
  await poll(); await flush();
  assert.equal(get('exp-import-message').textContent,'Source not accessible');
  const retry=descendants(get('exp-import-list')).find(el=>el.tag==='button' && el.textContent==='Retry');
  assert.ok(retry); await retry.events.click(); await flush();
  assert.ok(calls.some(c=>c.url.endsWith('/imports/url-new/retry')));
  sources.push(source('url-new',0,{title:'<svg onload=alert(1)>',rule_spec:{version:1},
    video_url:'/api/experiments/sources/url-new/video'}));
  Object.assign(importJobs[0],{state:'completed',stage:'completed',error:null,source_id:'url-new',
    files:['writeup.md','33ai-integration.md','video.mp4'],message:'Generated'});
  await poll(); await flush();
  assert.equal(get('exp-import-open').hidden,false);
  assert.equal(get('exp-import-video').hidden,false);
  assert.equal(get('exp-import-files').children[0].href,'/api/experiments/imports/url-new/files/writeup.md');
  await get('exp-import-open').events.click(); await flush();
  assert.equal(get('exp-title').textContent,'<svg onload=alert(1)>');
  assert.equal(get('exp-source-video').hidden,false);
  assert.equal(get('exp-source-video').src,'/api/experiments/sources/url-new/video');
  assert.equal(get('exp-run-scope').value,'selected');
  assert.equal(get('exp-run').textContent,'Start selected experiment');
  assert.ok(!descendants(get('exp-selected')).some(el=>el.tag==='svg'));
  await get('exp-form').events.submit({preventDefault(){}}); await flush();
  const selectedPost=calls.filter(c=>c.url==='/api/experiments/runs' && c.options.method==='POST').at(-1);
  assert.deepEqual(JSON.parse(selectedPost.options.body).source_ids,['url-new']);
  assert.deepEqual(folderIds(),['url-new']);
  console.log('Experiments client: archive, URL generation/retry, selected replay, safe text and stale-response checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
