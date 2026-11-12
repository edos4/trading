// Saved Arrow directions are separate from price observations and missing actuals.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.children=[];this.events={};this.style={};this.value=''; }
  appendChild(child) {this.children.push(child);if(child.value && !this.value) this.value=child.value;}
  replaceChildren() {this.children=[];this.value='';}
  addEventListener(name,fn) {this.events[name]=fn;}
}
const elements=new Map();
const get=id => {if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const drawn=[],requests=[];
let actuals=[],failure=false,abstained=false;
const forecast=(symbol,rank) => ({symbol,rank,anchor:{time:'2026-11-11',value:100},
  history:[{time:'2026-11-11',value:100}],actuals,
  forecasts:[1,3].map(h => ({horizon:h,date:h===1?'2026-11-12':'2026-11-16',
    direction:abstained?null:'up',abstained,reasons:abstained?['short history']:[],probabilities:{up:.6,down:.2,flat:.2}})),
  direction_results:[1,3].map(h => ({horizon:h,actual:h===1&&actuals.length?'up':null,
    correct:h===1&&actuals.length&&!abstained?true:null,abstained}))});
const report=h => ({horizon:h,date:h===1?'2026-11-12':'2026-11-16',expected:2,
  observed:h===1?actuals.length*2:0,count:h===1&&!abstained?actuals.length*2:0,
  correct:h===1&&!abstained?actuals.length*2:0,incorrect:0,abstained:abstained?2:0,
  pending:h===1&&actuals.length?0:2,accuracy_pct:h===1&&actuals.length&&!abstained?100:null});
const payload=() => ({status:'collecting',metadata:{as_of:'2026-11-11',generated_at:'2026-11-12T07:00:00-05:00',
    target_dates:{1:'2026-11-12',3:'2026-11-16'},model:{version:'test'}},
  actual_count:actuals.length*2,expected_actual_count:4,
  scores:{arrow:[report(1),report(3)]},direction_report:[report(1),report(3)],
  results:[forecast('NVDA',1),forecast('SPCX',2)]});
const document={getElementById:get,querySelector:get,createElement:()=>new Element()};
const window={KronosCharts:{drawChart:(host,data)=>{drawn.push(data);return{remove(){}};}}};
const fetch=async(url,options)=>{requests.push({url,options});return{ok:!failure,
  json:async()=>failure?{detail:'Database unavailable'}:payload()};};
vm.runInNewContext(fs.readFileSync('web/static/arrow_experiment4.js','utf8'),{window,document,fetch});
(async()=>{
  await new Promise(setImmediate);
  assert.equal(drawn.length,1);
  assert.equal(drawn[0].history.length,1,'never invent future closes');
  assert.equal(drawn[0].base.length+drawn[0].finetuned.length,0,'Arrow directions are not price paths');
  assert.match(get('experiment4-title').textContent,/Arrow.*2026-11-11/);
  const cells=i=>get('#experiment4-direction-report tbody').children[i].children.map(c=>c.textContent);
  assert.deepEqual(cells(0),['2026-11-12','+1D','0/2','0','0','0','0','2','Pending']);
  get('experiment4-symbol').value='SPCX';get('experiment4-symbol').events.change();
  actuals=[{time:'2026-11-12',value:101}];
  await get('experiment4-refresh').events.click();
  assert.equal(drawn.at(-1).symbol,'SPCX','refresh preserves stock selection');
  assert.equal(drawn.at(-1).history.at(-1).value,101);
  assert.deepEqual(cells(0),['2026-11-12','+1D','2/2','2','2','0','0','0','100.0%']);
  assert.deepEqual(cells(1),['2026-11-16','+3D','0/2','0','0','0','0','2','Pending']);
  let row=get('#experiment4-table tbody').children[0].children;
  assert.match(row[3].textContent,/Up · Correct.*up 60.0%/);
  assert.match(row[4].textContent,/101.00 · ↑ Up/);
  assert.match(row[5].textContent,/Up · Pending/,'three-day target is independently pending');
  assert.equal(row[6].textContent,'Pending');
  abstained=true;
  await get('experiment4-refresh').events.click();
  row=get('#experiment4-table tbody').children[0].children;
  assert.match(row[3].textContent,/Abstained: short history/);
  assert.deepEqual(cells(0),['2026-11-12','+1D','2/2','0','0','0','2','0','Pending']);
  assert.ok(requests.every(r=>r.url==='/api/kronos/experiment4'&&!r.options.method),'read-only GETs');
  failure=true;await get('experiment4-refresh').events.click();
  assert.match(get('experiment4-status').textContent,/Database unavailable/);
  assert.equal(get('experiment4-refresh').disabled,false);
  const series=[],renderer={};
  vm.runInNewContext(fs.readFileSync('web/static/kronos.js','utf8'),{document,window:renderer,
    LightweightCharts:{CrosshairMode:{Normal:0},LineStyle:{Dashed:2,Solid:0},createChart:()=>({
      addLineSeries(options){const line={options,setData(data){this.data=data;}};series.push(line);return line;},
      timeScale:()=>({setVisibleLogicalRange(){}}),
    })}});
  renderer.KronosCharts.drawChart(new Element(),drawn.at(-1));
  assert.equal(series.length,1,'only observed prices are drawn');
  assert.equal(series[0].options.color,'#ffffff');assert.equal(series[0].data.length,2);
  console.log('Arrow Experiment 4 client checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
