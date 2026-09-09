// Exercise the actual Replay client and chart wrapper without browser dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.events = {}; this.children = []; this.value = ''; this.hidden = true; this.dataset = {}; this.classList = {add(){}, toggle(){}, remove(){}}; }
  addEventListener(name, fn) { this.events[name] = fn; }
  appendChild(child) { this.children.push(child); }
  set innerHTML(value) { this.children = []; this.html = value; }
  get innerHTML() { return this.html; }
}
const elements = new Map();
const get = key => { if (!elements.has(key)) elements.set(key, new Element()); return elements.get(key); };
const document = {getElementById: get, querySelector: get, querySelectorAll: () => [], createElement: () => new Element(), addEventListener(){}};
const annotation = {type:'segment', start_date:'2024-01-02', end_date:'2024-01-12', start_price:10, end_price:12, color:'#ff9800'};
const row = {symbol:'TEST', action:'BUY', pattern:'test', entry:12, current:13, exit:14, sim_opened:'2024-01-12T00:00:00+00:00', sim_closed:'2024-01-19T00:00:00+00:00', chart_annotations:[annotation]};
const exported = {books:[{market:'ph', open_positions:[row], closed_trades:[row]}]};
let saved = null;
const requests = [], lines = [];
const series = () => ({setData(data){this.data=data;}, priceScale(){return {applyOptions(){}};}, createPriceLine(){}, setMarkers(){}});
const chart = {addCandlestickSeries:series, addHistogramSeries:series, addLineSeries(){const s=series();lines.push(s);return s;}, subscribeCrosshairMove(){}, timeScale(){return {fitContent(){}};}, remove(){}};
const window = {TB_PAGE:'replay'};
const context = vm.createContext({window, document, console, FormData, LightweightCharts:{createChart:()=>chart,CrosshairMode:{Normal:0},LineStyle:{Dashed:2,Solid:0}}, fetch: async (url, opts) => {
  let data = {};
  if (url.endsWith('/load')) data = {replay:saved};
  if (url.endsWith('/upload')) saved = JSON.parse(opts.body);
  if (url.endsWith('/chart')) {
    const body=JSON.parse(opts.body); requests.push(body);
    assert.deepEqual(body.chart_annotations,[annotation]);
    assert.equal(body.market,'ph');
    assert.equal(body.entry_time,row.sim_opened);
    data={candles:[{time:'2024-01-02',open:10,high:11,low:9,close:10},{time:'2024-01-12',open:11,high:13,low:10,close:12}],segments:[{data:[{time:'2024-01-02',value:10},{time:'2024-01-12',value:12}],color:'#ff9800'}]};
  }
  return {status:200,ok:true,headers:{get:()=> 'application/json'},json:async()=>data};
}});
vm.runInContext(fs.readFileSync('web/static/tv_chart.js','utf8'),context);
vm.runInContext(fs.readFileSync('web/static/app.js','utf8'),context);
(async()=>{
  vm.runInContext('initReplay()',context);
  await new Promise(setImmediate);
  get('replay-file').files=[{text:async()=>JSON.stringify(exported)}];
  await get('replay-load').events.click();
  for (const [selector,side] of [['#replay-pos tbody','open'],['#replay-closed tbody','closed']]) {
    const tradeRow=get(selector).children[0];
    await tradeRow.events.dblclick();
    assert.equal(get('replay-chart-modal').hidden,false);
    assert.equal(get('replay-chart-status').hidden,true);
    assert.equal(requests.at(-1).side,side);
    assert.equal(lines.at(-1).data.length,2);
    get('replay-chart-close').events.click();
    assert.equal(get('replay-chart-modal').hidden,true);
  }
  // Reload the persisted export and exercise the row handler again.
  vm.runInContext('initReplay()',context);
  await new Promise(setImmediate);
  await get('#replay-closed tbody').children[0].events.dblclick();
  assert.equal(requests.length,3);
  console.log('Replay upload, reload, open/closed double-click, and pattern line rendering passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
