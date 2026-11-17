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
const sources = [source('406-ema',406),source('442-repaint',442,{availability:'blocked',blocker:'Repainting formula missing'}),
  source('385-candles',385,{kind:'event-study',title:'<img src=x onerror=alert(1)>'})];
const summary = {signal_count:2,trade_count:1,open_count:0,coverage:{evaluated_symbols:1},variants:{},equity:{}};
const batch = {id:'batch-one',state:'completed-with-blockers',request:{market:'us',start_date:'2020-01-01',end_date:'2021-01-01'},
  progress:{total_symbols:1,completed_symbols:1},counts:{completed:2,blocked:1},runs:sources.map(s=>({source:s.id,state:s.availability==='blocked'?'blocked':'completed',summary}))};
const calls = [];
let deferred, poll, failPost = false, keyCounter = 0;
async function api(url, options={}) {
  calls.push({url,options});
  if(url==='/api/experiments/catalog') return {sources};
  if(url==='/api/experiments/runs' && !options.method) return {batches:[]};
  if(url==='/api/experiments/runs' && options.method==='POST') {
    if(failPost) { failPost=false; throw new Error('simulated lost response'); }
    return batch;
  }
  if(url.startsWith('/api/experiments/sources/')) {
    const id=url.split('/').at(-1);
    if(deferred?.id===id) return deferred.promise;
    return sources.find(s=>s.id===id);
  }
  if(url.endsWith('/cancel')) return {...batch,state:'cancelled',cancel:1};
  if(url.includes('/records?')) return {rows:[{cursor:1,symbol:'SPY',date:'2020-06-01',variant:'ema-10-50',status:'observed',evidence:{}}],next:null,total:1};
  const id=url.split('/folders/')[1];
  if(id) return {source:sources.find(s=>s.id===id),run:batch.runs.find(s=>s.source===id)};
  return batch;
}
const context = {
  document:{getElementById:get,createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),querySelectorAll:()=>tabs},
  window:{location:{href:'http://localhost/experiments',search:''},history:{replaceState(){}},addEventListener(){},devicePixelRatio:1},
  localStorage:{getItem(){return null;},setItem(){}},
  URL,URLSearchParams,crypto:{randomUUID:()=> 'browser-request-key-'+(++keyCounter)},api,
  setInterval(fn){poll=fn; return 1;},clearInterval(){},console,
};
const flush = async()=>{for(let i=0;i<8;i++) await new Promise(resolve=>setImmediate(resolve));};
const descendants = el => [el,...el.children.flatMap(descendants)];
const folder = id => descendants(get('exp-folders')).find(el=>el.tag==='button' && descendants(el).some(c=>c.textContent===id));
vm.runInNewContext(fs.readFileSync(__dirname+'/static/experiments.js','utf8'),context);

(async()=>{
  await flush();
  assert.match(get('exp-catalog-count').textContent,/3 folders · 2 runnable · 1 blocked/);
  assert.equal(get('exp-title').textContent,'Source 406');
  await folder('442-repaint').events.click(); await flush();
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
  await tabs.find(t=>t.dataset.expTab==='signals').events.click(); await flush();
  assert.match(get('exp-table-note').textContent,/1 of 1 rows/);
  assert.ok(calls.some(c=>c.url.includes('/folders/406-ema/records?kind=signals')));
  await folder('385-candles').events.click(); await flush();
  assert.match(get('exp-table-note').textContent,/Event study/);
  assert.equal(get('exp-export').hidden,false);
  get('exp-history').value=''; await get('exp-history').events.change(); await flush();
  assert.equal(get('exp-progress').value,0);
  assert.equal(get('exp-run-id').textContent,'');
  assert.equal(get('exp-export').hidden,true);
  console.log('Experiments client: catalog, blockers, safe text, request, drilldown and stale-response checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
