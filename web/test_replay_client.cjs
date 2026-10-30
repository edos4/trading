// Exercise the actual Replay client and chart wrapper without browser dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.events = {}; this.children = []; this.style = {}; this.value = ''; this.hidden = true; this.dataset = {}; this.classList = {add(){}, toggle(){}, remove(){}}; this.ownerDocument = null; }
  addEventListener(name, fn) { this.events[name] = fn; }
  removeEventListener(name) { delete this.events[name]; }
  setAttribute(key, value) { this[key] = value; }
  remove() { this.removed = true; }
  focus() {}
  appendChild(child) { this.children.push(child); }
  set innerHTML(value) { this.children = []; this.html = value; }
  get innerHTML() { return this.html; }
}
const elements = new Map();
const get = key => { if (!elements.has(key)) elements.set(key, new Element()); const el = elements.get(key); el.ownerDocument = document; return el; };
const document = {body:new Element(), getElementById: get, querySelector: get, querySelectorAll: () => [], createElement() { const el = new Element(); el.ownerDocument = document; return el; }, addEventListener(){}, removeEventListener(){}};
document.body.ownerDocument = document;
const annotation = {type:'segment', start_date:'2024-01-02', end_date:'2024-01-12', start_price:10, end_price:12, color:'#ff9800'};
const row = {symbol:'TEST', action:'BUY', pattern:'test', entry:12, current:13, exit:14, sim_opened:'2024-01-12T00:00:00+00:00', sim_closed:'2024-01-19T00:00:00+00:00', chart_annotations:[annotation]};
const exported = {books:[{market:'ph', open_positions:[row], closed_trades:[row]}]};
let saved = null, replayGeneration = 0;
const corrections = [{id:'drawn',kind:'pattern',label:'correct trough',points:[
  {time:'2024-01-02',value:10,pane:'price'}, {time:'2024-01-08',value:9.5,pane:'price'}]}];
const drawMounts = [];
const requests = [], lines = [];
const series = () => ({attachPrimitive(){}, detachPrimitive(){}, setData(data){this.data=data;}, applyOptions(){}, priceScale(){return {applyOptions(){}};}, createPriceLine(){}, setMarkers(markers){this.markers=markers;}});
const chart = {addCandlestickSeries:series, addHistogramSeries:series, addLineSeries(options){const s=series();s.options=options;lines.push(s);return s;}, subscribeCrosshairMove(){}, unsubscribeCrosshairMove(){},
  priceScale(){return {width:()=>40,options:()=>({minimumWidth:40})};},
  paneSize(){return {width:800,height:460};},
  timeScale(){return {fitContent(){}, subscribeVisibleLogicalRangeChange(){}, unsubscribeVisibleLogicalRangeChange(){}, setVisibleLogicalRange(){}, getVisibleLogicalRange:()=>null, subscribeSizeChange(){}, unsubscribeSizeChange(){}};}, remove(){}};
const window = {TB_PAGE:'replay', location:{}, innerWidth:1200, innerHeight:800,
  ChartDraw:{attach(options){drawMounts.push(options);if(options.leading)options.host.appendChild(options.leading);return {snapshot:()=>corrections,pending:()=>false,destroy(){}}}}};
const stored = new Map();
const sessionStorage = {getItem:key=>stored.get(key),setItem:(key, value)=>stored.set(key,value)};
const context = vm.createContext({window, document, sessionStorage, console, FormData, LightweightCharts:{createChart:()=>chart,CrosshairMode:{Normal:0},LineStyle:{Dashed:2,Dotted:1,Solid:0}}, fetch: async (url, opts) => {
  let data = {};
  if (url.endsWith('/load')) data = {replay:saved};
  if (url.endsWith('/upload')) { saved = JSON.parse(opts.body); saved.correction_replay_id = 'replay-' + (++replayGeneration); data = {replay_id:saved.correction_replay_id}; }
  if (url.endsWith('/chart')) {
    const body=JSON.parse(opts.body); requests.push(body);
    assert.deepEqual(body.chart_annotations,[annotation]);
    assert.equal(body.market,'ph');
    assert.equal(body.entry_time,row.sim_opened);
    data={candles:[{time:'2024-01-02',open:10,high:11,low:9,close:10},{time:'2024-01-08',open:10,high:11,low:9,close:10},{time:'2024-01-12',open:11,high:13,low:10,close:12}],markers:[{time:'2024-01-08',price:9.5,position:'belowBar',shape:'circle',color:'#ffeb3b',text:'Bottom'}],segments:[{data:[{time:'2024-01-02',value:10},{time:'2024-01-08',value:9.5},{time:'2024-01-12',value:12}],color:'#ffeb3b',label:'Rounding bottom fit'}],trendlines:[{data:[{time:'2024-01-02',value:9},{time:'2024-01-12',value:13}],color:'#42a5f5',style:'-',width:2,label:''}]};
  }
  if (url.endsWith('/chart')) Object.assign(data, {
    symbol:'TEST', pattern:'test', market:'ph', pattern_version_id:'version-1',
    pattern_note:requests.length > 1 ? 'Yellow: detected pattern' : '',
    trendline_note:requests.length > 1 ? 'Only the support rail is present — unconfirmed (2 touches); dotted.' : '',
  });
  return {status:200,ok:true,headers:{get:()=> 'application/json'},json:async()=>data};
}});
vm.runInContext(fs.readFileSync('web/static/tv_chart.js','utf8'),context);
vm.runInContext(fs.readFileSync('web/static/app.js','utf8'),context);
(async()=>{
  assert.equal(window.TVChart.canClose(), true);
  vm.runInContext('initReplay()',context);
  await new Promise(setImmediate);
  get('replay-file').files=[{text:async()=>JSON.stringify(exported)}];
  await get('replay-load').events.click();
  const identities = [];
  for (const [selector,side] of [['#replay-pos tbody','open'],['#replay-closed tbody','closed']]) {
    const tradeRow=get(selector).children[0];
    await tradeRow.events.dblclick();
    assert.equal(get('replay-chart-modal').hidden,false);
    assert.equal(get('replay-chart-status').hidden,side==='open');
    if (side==='closed') {
      assert.match(get('replay-chart-status').textContent,/Yellow: detected pattern · Only the support rail/);
      assert.match(get('replay-chart-status').textContent,/unconfirmed \(2 touches\); dotted/);
    }
    assert.equal(requests.at(-1).side,side);
    assert.equal(lines.at(-2).data.length,3);
    assert.equal(lines.at(-2).options.color,'#ffeb3b');
    assert.equal(lines.at(-2).markers[0].text,'Rounding bottom fit');
    assert.equal(lines.at(-2).markers[0].time,'2024-01-08');
    assert.equal(lines.at(-3).data[0].value,9.5);
    assert.equal(lines.at(-3).markers[0].text,'Bottom');
    assert.equal(lines.at(-1).data.length,2);
    assert.equal(lines.at(-1).options.color,'#42a5f5');
    assert.equal(lines.at(-1).options.priceLineVisible,false);
    assert.match(drawMounts.at(-1).guidance, /Blue rails: solid confirmed/);
    assert.match(drawMounts.at(-1).guidance, /Corrections have point handles/);
    get('replay-chart-host').events.contextmenu?.({preventDefault(){},clientX:50,clientY:50});
    const menu = document.body.children.at(-1);
    menu.onclick();
    assert.match(window.location.href, /^\/patterns\?chart=/);
    const attached = JSON.parse(stored.get(decodeURIComponent(window.location.href.split('=')[1])));
    assert.equal(attached.pattern_version_id, 'version-1');
    assert.equal(attached.symbol, 'TEST');
    assert.equal(attached.candles.length, 3);
    assert.deepEqual(attached.manual_corrections, corrections);
    assert.equal(attached.replay_id, 'replay-1');
    assert.equal(attached.entry_time, row.sim_opened);
    assert.equal(attached.replay_cutoff, row.sim_opened);
    identities.push(attached.trade_id);
    assert.deepEqual(Array.from(drawMounts.at(-1).times), ['2024-01-02','2024-01-08','2024-01-12']);
    get('replay-chart-close').events.click();
    assert.equal(get('replay-chart-modal').hidden,true);
  }
  assert.equal(identities.length, 2);
  assert.notEqual(identities[0], identities[1]);
  // Reload the persisted export and exercise the row handler again.
  vm.runInContext('initReplay()',context);
  await new Promise(setImmediate);
  await get('#replay-closed tbody').children[0].events.dblclick();
  assert.equal(requests.length,3);
  await get('replay-load').events.click();
  await get('#replay-pos tbody').children[0].events.dblclick();
  drawMounts.at(-1).onCorrect();
  const second = JSON.parse(stored.get(decodeURIComponent(window.location.href.split('=')[1])));
  assert.equal(second.replay_id, 'replay-2');
  assert.equal(second.replay_cutoff, row.sim_opened);
  const later = {...row, sim_opened:'2024-03-03T00:00:00+00:00', entry:22};
  get('replay-file').files=[{text:async()=>JSON.stringify({books:[{market:'ph', open_positions:[], closed_trades:[row, later]}]}) }];
  await get('replay-load').events.click();
  const closed = get('#replay-closed tbody').children;
  assert.equal(closed.length, 2);
  await closed[0].events.dblclick();
  const firstKey = requests.at(-1).entry_time;
  drawMounts.at(-1).onChange([{id:'only-this-row'}]);
  get('replay-chart-close').events.click();
  await closed[1].events.dblclick();
  assert.notEqual(requests.at(-1).entry_time, firstKey);
  assert.deepEqual(JSON.parse(JSON.stringify(drawMounts.at(-1).shapes)), []);
  get('replay-chart-close').events.click();
  await closed[0].events.dblclick();
  assert.equal(requests.at(-1).entry_time, firstKey);
  assert.deepEqual(JSON.parse(JSON.stringify(drawMounts.at(-1).shapes)), [{id:'only-this-row'}]);
  window.TVChart.mount(get('replay-chart-host'), {
    candles:['2024-01-02','2024-01-03','2024-01-04'].map(time=>({time,open:1,high:1,low:1,close:1})),
    rsi14:[{time:'2024-01-04', value:40}],
    symbol:'TEST', pattern:'test', market:'ph', trade_id:'early-bars', entry_time:'2024-01-04',
  });
  assert.deepEqual(Array.from(drawMounts.at(-1).times), ['2024-01-02','2024-01-03','2024-01-04']);
  assert.equal(drawMounts.at(-1).guidance, undefined); // no rail confirmation claim on a correction-only chart
  const data = {
    candles:['2024-01-02','2024-01-03','2024-01-04'].map(time=>({time,open:10,high:12,low:8,close:10})),
    rsi14:[{time:'2024-01-04', value:40}],
    segments:[{data:[{time:'2024-01-02',value:11},{time:'2024-01-04',value:11}],style:'--',color:'#ffeb3b',label:'Pattern'}],
    trendlines:['-','--',':'].map((style, index)=>({
      data:[{time:'2024-01-02',value:8+index},{time:'2024-01-03',value:9+index}],
      color:'#42a5f5',style,width:2,label:'',kind:'support',pivots:index+2,
      confirmed:index===0,broken:index===1,
    })),
  };
  const original = JSON.stringify(data);
  const before = lines.length;
  window.TVChart.mount(get('replay-chart-host'),data,{readOnly:true});
  // The RSI, pattern and automatic rails stay on separate series. Sparse rail
  // endpoints are preserved, so a clipped rail is not extrapolated to today.
  const overlays = lines.slice(before, before+4);
  assert.equal(overlays[0].options.color,'#ffeb3b');
  assert.equal(overlays[0].options.lineStyle,2);
  for (const [index,style] of [0,2,1].entries()) {
    const rendered = overlays[index+1];
    assert.deepEqual(rendered.data,data.trendlines[index].data);
    assert.equal(rendered.data.at(-1).time,'2024-01-03');
    assert.equal(rendered.options.lineStyle,style);
    assert.equal(rendered.options.lastValueVisible,false);
    assert.equal(rendered.options.crosshairMarkerVisible,false);
    assert.equal(rendered.markers,undefined);
  }
  assert.equal(JSON.stringify(data),original);
  assert.equal(drawMounts.at(-1).readOnly,true);
  assert.deepEqual(Array.from(drawMounts.at(-1).times),data.candles.map(row=>row.time));
  assert.deepEqual(JSON.parse(JSON.stringify(drawMounts.at(-1).shapes)),[]);
  window.TVChart.unmount();
  const dii = {length:60, period:40};
  for (const field of ['up','down','bulls','bears','upper','lower']) {
    dii[field] = data.candles.map((bar, index) => index === 0 ? {time:bar.time} :
      {time:bar.time, value:{up:21,down:14,bulls:60,bears:40,upper:12,lower:8}[field]});
  }
  window.TVChart.mount(get('replay-chart-host'), {...data, dii}, {readOnly:true});
  assert.equal(get('replay-chart-host').children.length, 4); // price, RSI, DII, view toggles
  assert.match(get('replay-chart-host').children[2].children[0].textContent, /DII \(60, 40\).*last displayed bar.*Bulls 60.0%.*Bears 40.0%/);
  context.diiReadout = {dii};
  assert.match(vm.runInContext('fmtRsi(diiReadout, "2024-01-02")', context), /Up — Down — Bulls — Bears —/);
  assert.match(vm.runInContext('fmtRsi(diiReadout, "2024-01-04")', context), /Up 21 Down 14 Bulls 60.0% Bears 40.0%/);
  // View toggles: each click re-mounts the same payload without that overlay.
  const toggles = get('replay-chart-host').children.at(-1);
  assert.deepEqual(toggles.children.map(button => button.textContent), ['Patterns', 'RSI', 'DII']);
  assert.deepEqual(toggles.children.map(button => button['aria-pressed']), ['true', 'true', 'true']);
  let added = lines.length;
  toggles.children[2].events.click();  // DII off
  assert.equal(get('replay-chart-host').children.length, 3); // price, RSI, toggles
  assert.equal(lines.length - added, 5); // pattern segment, three rails, RSI
  added = lines.length;
  get('replay-chart-host').children.at(-1).children[1].events.click();  // RSI off
  assert.equal(get('replay-chart-host').children.length, 1); // price only, plus toggles
  assert.equal(lines.length - added, 4); // pattern segment and three rails
  added = lines.length;
  const draws = drawMounts.length;
  get('replay-chart-host').children.at(-1).children[0].events.click();  // patterns off
  assert.equal(lines.length, added); // no overlay series, no correction layer
  assert.equal(drawMounts.length, draws);
  window.TVChart.unmount();
  console.log('Replay upload, reload, open/closed double-click, and pattern line rendering passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
