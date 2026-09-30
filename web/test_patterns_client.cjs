// Version changes must never leave another candidate's charts on screen.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.children = []; this.dataset = {}; this.classList = {add(){}, remove(){}}; }
  replaceChildren() { this.children = []; }
  appendChild(child) { this.children.push(child); }
  addEventListener() {}
  querySelector() { return this; }
  set innerHTML(value) { this.children = []; }
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
let resolveOld;
const document = {getElementById:get, createElement:()=>new Element(), addEventListener(){}};
const window = {TVChart:{unmount(){}}};
const context = vm.createContext({document, window, fetch: async path => {
  if (path.endsWith('/old')) await new Promise(resolve => { resolveOld = resolve; });
  return {ok:true, json:async()=> path.endsWith('/versions') ? {versions:[{version_id:'base',validation:'passed'}]} : path.endsWith('/source') ? {files:{}} :
    path.endsWith('/diff') ? {files:{}} :
    {version_id:path.split('/').at(-1), lifecycle:{validation:'passed'}, backtests:[]}};
}});
let source = fs.readFileSync('web/static/patterns.js', 'utf8');
source = source.replace('document.addEventListener("DOMContentLoaded", init);',
  'window.test = {state, selectVersion, loadDetail, renderComparison, loadVersions, applyChartHandoff};');
vm.runInContext(source, context);
(async () => {
  const {state, selectVersion, loadDetail, renderComparison} = window.test;
  state.versions = [{version_id:'new', validation:'passed'}];
  renderComparison({run_id:'old-run', state:'completed', result:{metrics:{}, detections:[
    {symbol:'OLD', session:'2026-01-01', action:'BUY'}]}}, null);
  assert.equal(get('pat-detections').children.length, 1);
  state.versionId = 'old';
  const oldRequest = loadDetail('old');
  await selectVersion('new');
  assert.equal(get('pat-detections').children.length, 0);
  assert.equal(get('pat-chart').hidden, true);
  assert.match(get('pat-detection-status').textContent, /No completed backtest/);
  resolveOld();
  await oldRequest;
  assert.match(get('pat-detail').textContent, /new/);
  assert.doesNotMatch(get('pat-detail').textContent, /old/);
  get('pat-pattern').value = 'test-pattern';
  state.patterns = [{pattern_id:'test-pattern',default_version_id:'base',generation:1}];
  state.chartContext = {pattern:'test-pattern',manual_corrections:[{id:'drawing'}]};
  state.versionId = null;
  await window.test.loadVersions();
  assert.equal(state.versionId, null); // legacy/missing version must not silently select today's default
  await selectVersion('base');
  assert.equal(state.versionId, 'base');
  assert.equal(state.chartContext.manual_corrections[0].id, 'drawing');
  await window.test.applyChartHandoff({
    symbol:'AC', pattern:'test-pattern', market:'ph', pattern_version_id:'missing-version',
    candles:[{time:'2024-01-02'},{time:'2024-01-05'}],
    manual_corrections:[{id:'drawing', kind:'pattern', label:'neckline'}],
  });
  assert.equal(state.versionId, null);
  assert.equal(get('pat-market').value, 'ph');
  assert.equal(get('pat-start_date').value, '2024-01-02');
  assert.equal(get('pat-end_date').value, '2024-01-05');
  assert.match(get('pat-correction-summary').textContent, /neckline/);
  assert.match(get('pat-action-status').textContent, /select a base version explicitly/);
  assert.equal(state.chartContext.manual_corrections[0].label, 'neckline');
  console.log('Version switching clears stale detections and ignores outdated responses');
})().catch(error => { console.error(error); process.exitCode = 1; });
