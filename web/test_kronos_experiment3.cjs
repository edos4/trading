// Saved forecasts survive stock changes/refresh; actual lines extend only with observations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() { this.children = []; this.events = {}; this.style = {}; this.value = ''; }
  appendChild(child) {
    this.children.push(child);
    if (child.value && !this.value) this.value = child.value;
  }
  replaceChildren() { this.children = []; this.value = ''; }
  addEventListener(name, fn) { this.events[name] = fn; }
}
const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const drawn = [];
const requests = [];
let actuals = [];
let failure = false;
const forecast = (symbol, rank) => ({symbol, rank, days: 2,
  anchor: {time: '2026-11-10', value: 100}, history: [{time: '2026-11-10', value: 100}],
  base: [{time: '2026-11-11', value: 101}, {time: '2026-11-12', value: 102}],
  finetuned: [{time: '2026-11-11', value: 99}, {time: '2026-11-12', value: 98}], actuals,
  direction_results: [
    {day: 1, actual: actuals.length ? 'up' : null,
     base: {predicted: 'up', correct: actuals.length ? true : null},
     finetuned: {predicted: 'down', correct: actuals.length ? false : null}},
    {day: 2, actual: null, base: {predicted: 'up', correct: null}, finetuned: {predicted: 'down', correct: null}},
  ]});
const payload = () => ({status: 'collecting', metadata: {generated_at: '2026-11-10T19:04:09-05:00',
  target_dates: ['2026-11-11', '2026-11-12']}, actual_count: actuals.length * 2,
  scores: {base: [{day: 1, count: actuals.length * 2, direction_correct: 2, direction_accuracy_pct: 100, mape_pct: 1}],
           finetuned: [{day: 1, count: actuals.length * 2, direction_correct: 0, direction_accuracy_pct: 0, mape_pct: 4}]},
  direction_report: [
    {date: '2026-11-11', day: 1, expected: 2, evaluated: actuals.length * 2, pending: actuals.length ? 0 : 2,
     base: {correct: 2, incorrect: 0, accuracy_pct: 100}, finetuned: {correct: 0, incorrect: 2, accuracy_pct: 0}},
    {date: '2026-11-12', day: 2, expected: 2, evaluated: 0, pending: 2,
     base: {correct: 0, incorrect: 0, accuracy_pct: null}, finetuned: {correct: 0, incorrect: 0, accuracy_pct: null}},
  ],
  results: [forecast('AAPL', 1), forecast('NVDA', 2)]});
const document = {getElementById: get, querySelector: get, createElement: () => new Element()};
const window = {KronosCharts: {
  makePanel: () => ({section: new Element(), host: new Element(), save: new Element()}),
  drawChart: (host, data) => { drawn.push(data); return {remove() {}}; }, saveChart() {},
}};
const fetch = async (url, options) => {
  requests.push({url, options});
  return {ok: !failure, json: async () => failure ? {detail: 'Database unavailable'} : payload()};
};
vm.runInNewContext(fs.readFileSync('web/static/kronos_experiment3.js', 'utf8'), {window, document, fetch});

(async () => {
  await new Promise(setImmediate);
  assert.equal(drawn.length, 1);
  assert.equal(drawn[0].symbol, 'AAPL');
  assert.equal(drawn[0].history.length, 1, 'no actual values invented for forecast dates');
  assert.equal(get('#experiment3-table tbody').children.length, 2);
  assert.match(get('#experiment3-table tbody').children[0].children[3].textContent, /Up · Pending/);
  const reportCells = (i) => get('#experiment3-direction-report tbody').children[i].children.map((c) => c.textContent);
  assert.deepEqual(reportCells(0), ['2026-11-11', '+1D', '0/2', '2', 'Pending', 'Pending', 'Pending', 'Pending', 'Pending', 'Pending']);
  get('experiment3-symbol').value = 'NVDA';
  get('experiment3-symbol').events.change();
  assert.equal(drawn.at(-1).symbol, 'NVDA');
  const frozen = JSON.stringify(drawn.at(-1).base);
  actuals = [{time: '2026-11-11', value: 103}];
  await get('experiment3-refresh').events.click();
  assert.equal(drawn.at(-1).symbol, 'NVDA', 'refresh preserves selection');
  assert.equal(drawn.at(-1).history.length, 2);
  assert.equal(drawn.at(-1).history.at(-1).value, 103);
  assert.equal(JSON.stringify(drawn.at(-1).base), frozen, 'actuals do not rewrite predictions');
  const cells = get('#experiment3-table tbody').children[0].children;
  assert.match(cells[3].textContent, /Up · Correct/);
  assert.match(cells[4].textContent, /Down · Incorrect/);
  assert.match(cells[5].textContent, /103\.00 · ↑ Up/);
  assert.match(cells[6].textContent, /Up · Pending/, 'day 2 is not scored using day 1 actual');
  assert.match(get('experiment3-scores').children.at(-2).textContent, /100\.0% direction correct \(2\/2\)/);
  assert.match(get('experiment3-chart').children[0].children[0].textContent, /base ↑ Up \(Correct\)/);
  assert.deepEqual(reportCells(0), ['2026-11-11', '+1D', '2/2', '0', '2', '0', '100.0%', '0', '2', '0.0%']);
  assert.deepEqual(reportCells(1), ['2026-11-12', '+2D', '0/2', '2', 'Pending', 'Pending', 'Pending', 'Pending', 'Pending', 'Pending']);
  assert.ok(requests.every((r) => r.url === '/api/kronos/experiment3' && !r.options.method), 'read-only GETs only');
  failure = true;
  await get('experiment3-refresh').events.click();
  assert.match(get('experiment3-status').textContent, /Database unavailable/);
  assert.equal(get('experiment3-refresh').disabled, false);
  // Exercise the shared renderer too: the required three colors and anchor join.
  const series = [];
  const rendererWindow = {};
  vm.runInNewContext(fs.readFileSync('web/static/kronos.js', 'utf8'), {
    document, window: rendererWindow,
    LightweightCharts: {CrosshairMode: {Normal: 0}, LineStyle: {Dashed: 2, Solid: 0},
      createChart: () => ({
        addLineSeries(options) {
          const line = {options, setData(data) { this.data = data; }};
          series.push(line);
          return line;
        },
        timeScale: () => ({setVisibleLogicalRange() {}}),
      })},
  });
  rendererWindow.KronosCharts.drawChart(new Element(), drawn.at(-1));
  assert.deepEqual(series.map((s) => s.options.color), ['#ffffff', '#2962ff', '#ef5350']);
  assert.equal(series[0].data.length, 2, 'actual series has observed future close');
  assert.equal(series[1].data[0].time, '2026-11-10', 'forecast joins the frozen anchor');
  assert.equal(series[1].options.lineStyle, 2);
  assert.equal(series[2].options.lineStyle, 0);
  console.log('Experiment 3 client checks passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
