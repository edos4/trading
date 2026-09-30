// Run with node web/test_chart_draw.cjs; no browser or dependencies required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(top=0) { this.events={}; this.children=[]; this.style={}; this.offsetTop=top; this.clientHeight=100; this.tagName='DIV'; }
  addEventListener(name, fn) { this.events[name]=fn; }
  removeEventListener(name) { delete this.events[name]; }
  appendChild(el) { this.children.push(el); el.parent=this; }
  contains(el) { return el === this || this.children.includes(el); }
  setAttribute(key, value) { this[key]=value; }
  getBoundingClientRect() { return {left:0, top:this.offsetTop, bottom:this.offsetTop+100}; }
  focus() {}
  remove() { if(this.parent) this.parent.children=this.parent.children.filter(c=>c!==this); }
}
const document = new Element(); document.createElement=()=>new Element();
const window = {};
vm.runInNewContext(fs.readFileSync('web/static/chart_draw.js','utf8'), {window, document});
const host = new Element();
const times=['2026-01-01','2026-01-02','2026-01-03','2026-01-04','2026-01-05'];
let attached=0, detached=0, changes=[];
const applied=[];
const panes=['price','rsi'].map((pane,index)=>{
  const live={mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true};
  return {pane,el:new Element(index*100),
  chart:{options:()=>({handleScroll:live}),
    applyOptions(options){Object.assign(live, options.handleScroll); applied.push(JSON.parse(JSON.stringify(live)));},timeScale:()=>({
    timeToCoordinate:t=>times.indexOf(t)*10,coordinateToTime:x=>x===80?'2099-01-01':times[Math.round(x/10)]})},
  series:{coordinateToPrice:y=>100-y,priceToCoordinate:v=>100-v,
    attachPrimitive(p){ attached++; p.attached({requestUpdate(){}}); },detachPrimitive(p){detached++;p.detached();}},
  };
});
let tools=window.ChartDraw.attach({host,panes,times,onChange:s=>changes.push(s)});
const button = text => host.children[0].children.find(e=>e.textContent===text);
const click = text => button(text).events.click();
function event(x,y) {return {clientX:x,clientY:y,button:0,target:host,preventDefault(){},stopPropagation(){},stopImmediatePropagation(){this.stopped=true;}};}
function down(x,y,pane=0) {panes[pane].el.events.pointerdown(event(x,y));}
function up(x,y) {document.events.pointerup(event(x,y));}
function key(name, other={}) {const ev={...event(0,0),key:name,...other}; document.events.keydown(ev); return ev;}
const snapshot=()=>JSON.parse(JSON.stringify(tools.snapshot()));
click('Draw pattern');
assert.equal(applied.at(-1).mouseWheel, true);
assert.equal(applied.at(-1).pressedMouseMove, false);
down(0,40); up(0,40); down(20,20); up(20,20); down(40,40); up(40,40);
assert.equal(tools.pending(),true); key('Enter');
let original=snapshot(); assert.equal(original[0].points.length,3);
assert.equal(original[0].points[0].time,times[0]);
click('Select'); down(20,20); document.events.pointermove(event(30,30)); up(30,30);
assert.equal(snapshot()[0].points[1].time,times[3]);
assert.equal(original[0].points[1].time,times[2]);
click('Undo'); assert.deepEqual(snapshot(),original);
const label=host.children[0].children.find(e=>e.type==='text'); label.value='correct shoulder';label.events.change();
assert.equal(snapshot()[0].label,'correct shoulder');
click('Trend');down(0,60);document.events.pointermove(event(40,140));up(40,140);
assert.equal(snapshot()[1].points[1].pane,'rsi');
click('VLine');down(10,40);up(10,40);assert.equal(snapshot()[2].kind,'vert');
const escaped=key('Escape'); assert.equal(escaped.stopped, true);
assert.equal(applied.at(-1).pressedMouseMove, true);
assert.equal(applied.at(-1).horzTouchDrag, true);
const prior=snapshot();
click('Select');down(40,40);document.events.pointermove(event(30,50));document.events.pointercancel();
assert.deepEqual(snapshot(),prior); // cancelling a drag restores its immutable starting state
click('Draw pattern');down(0,40);
const field=new Element(); field.tagName='INPUT';
key('Escape',{target:field}); assert.equal(tools.pending(),true);
key('Escape',{target:document}); assert.equal(tools.pending(),false);
assert.deepEqual(snapshot(),prior);
click('Clear');assert.equal(snapshot().length,0);click('Undo');assert.deepEqual(snapshot(),prior);
tools.load(original); original[0].points[0].value=999;assert.notEqual(snapshot()[0].points[0].value,999);
const saved=snapshot();click('Draw pattern'); down(80,40);
assert.equal(tools.pending(), false);
assert.match(host.children[0].children.find(e=>e.role==='status').textContent, /actual candle/);
tools.destroy();assert.equal(attached,detached);
assert.equal(document.events.pointerup,undefined);assert.equal(document.events.keydown,undefined);
tools=window.ChartDraw.attach({host,panes,times,shapes:saved,readOnly:true});
assert.deepEqual(snapshot(),saved);assert.equal(host.children.length,0);
tools.destroy();assert.equal(attached,detached);assert.ok(changes.length>5);
console.log('Drawing geometry, immutable snapshots, editing, labels, undo, cancellation, preview, and cleanup passed');
