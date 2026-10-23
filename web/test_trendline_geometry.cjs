// Run with node web/test_trendline_geometry.cjs. Use the bundled library's real
// data layer and autoscaler without bootstrapping a DOM or browser runtime.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const libraryWindow = {};
const vendor = fs.readFileSync('web/static/vendor/lightweight-charts.standalone.production.js', 'utf8');
// These private symbols belong to the checked-in v4.2.3 build. A vendor update
// must update this test adapter; production source is never rewritten.
assert.ok(vendor.includes('window.LightweightCharts=ze'));
vm.runInNewContext(vendor.replace('window.LightweightCharts=ze',
  'window.testInternals={DataLayer:he,TimeBehavior:Gn,lineDefaults:n,ChartModel:Vn,chartDefaults:Se,seriesDefaults:l,candleDefaults:t,histogramDefaults:r,merge:T};window.LightweightCharts=ze'),
{window: libraryWindow, document: {}, navigator: {language: 'en-US'}, performance});
const library = libraryWindow.LightweightCharts;
const {DataLayer, TimeBehavior, lineDefaults} = libraryWindow.testInternals;
assert.equal(library.version(), '4.2.3');
assert.equal(lineDefaults.lineType, library.LineType.Simple); // straight interpolation
const {ChartModel, chartDefaults, seriesDefaults, candleDefaults, histogramDefaults, merge} = libraryWindow.testInternals;
const nativeOptions = chartDefaults();
const nativeBehavior = new TimeBehavior();
nativeBehavior.setOptions(nativeOptions);
const nativeChart = new ChartModel(() => {}, nativeOptions, nativeBehavior);
const nativeCandles = nativeChart.dd('Candlestick', {...seriesDefaults, ...candleDefaults});
const nativeLine = nativeChart.dd('Line', {...seriesDefaults, ...lineDefaults});
assert.equal(nativeLine.Dt(), nativeCandles.Dt());
assert.equal(nativeLine.Dt().Pa(), 'right'); // both actually share the price scale
nativeChart.S();

class Element {
  constructor(document) { this.ownerDocument = document; this.style = {}; this.children = []; }
  appendChild(child) { this.children.push(child); }
  set innerHTML(value) { this.children = []; }
}
const document = {createElement() { return new Element(document); }};
const host = new Element(document);
const charts = [];

function createChart(el, options) {
  const behavior = new TimeBehavior();
  const layer = new DataLayer(behavior);
  const nativeOptions = merge(chartDefaults(), options);
  behavior.setOptions(nativeOptions);
  const native = new ChartModel(() => {}, nativeOptions, behavior);
  native.S_(803);
  native.qc()[0].Io(460);
  const timeline = native.St();
  const allSeries = [];
  const scale = {
    fitContent() { timeline.hc(); },
    getVisibleLogicalRange() { const r = timeline.Du(); return r && {from: r.Bs(), to: r.ui()}; },
    setVisibleLogicalRange(range) { timeline.lc(range); },
    subscribeVisibleLogicalRangeChange(fn) { timeline.sc().l(() => fn(scale.getVisibleLogicalRange())); },
    timeToCoordinate(time) {
      const index = timeline.Oa(behavior.convertHorzItemToInternal(time), false);
      return index == null ? null : timeline.zt(index);
    },
    coordinateToLogical: x => timeline.Nu(x),
    width: () => timeline.Hi(),
  };
  function addSeries(kind, options = {}) {
    const defaults = kind === 'Candlestick' ? candleDefaults : kind === 'Histogram' ? histogramDefaults : lineDefaults;
    const model = native.dd(kind, merge({...seriesDefaults, ...defaults}, options));
    const api = {
      kind, options, model,
      setData(data) {
        this.data = data;
        const update = layer.Kb(model, data);
        native._d(update.St.Lu, update.St.hw, update.St.lw);
        for (const [series, change] of update.ew) series.J(change.$e, change.rw);
        native.Wu();
      },
      applyOptions(next) { model.$h(next); },
      priceScale: () => ({applyOptions(next) { model.Dt().$h(next); }}),
      createPriceLine() {}, setMarkers() {},
    };
    allSeries.push(api);
    return api;
  }
  const chart = {
    options, allSeries, layer, scale,
    addCandlestickSeries: options => addSeries('Candlestick', options),
    addHistogramSeries: options => addSeries('Histogram', options),
    addLineSeries: options => addSeries('Line', options),
    timeScale: () => scale,
    subscribeCrosshairMove() {}, remove() { native.S(); },
    resize(width) { native.S_(width); },
    times: () => layer.Xb.map(point => point.originalTime),
  };
  charts.push(chart);
  return chart;
}

const window = {};
vm.runInNewContext(fs.readFileSync('web/static/tv_chart.js', 'utf8'), {
  window, document,
  LightweightCharts: {...library, createChart},
});
const candles = Array.from({length: 100}, (_, index) => ({
  time: new Date(Date.UTC(2024, 0, index + 1)).toISOString().slice(0, 10),
  open: 30, high: 31, low: 29, close: 30,
}));
const rail = {data: [{time: candles[10].time, value: 10}, {time: candles[90].time, value: 20}],
  color: '#42a5f5', style: '-', width: 2};
const resistance = {...rail, style: ':', data: [
  {time: candles[0].time, value: 50}, {time: candles[99].time, value: 40},
]};
const payload = {
  candles, rsi14: [{time: candles[99].time, value: 50}],
  segments: [{...rail, color: '#ffeb3b'}], trendlines: [rail, resistance],
  // A line-only forecast also needs whitespace padding in the RSI pane.
  forecast: [{time: '2024-04-10', value: 32}],
};
const originalPayload = JSON.stringify(payload);
window.TVChart.mount(host, payload, {readOnly: true});
const [price, rsi] = charts;
assert.deepEqual(price.times(), rsi.times());
assert.deepEqual(price.scale.getVisibleLogicalRange(), rsi.scale.getVisibleLogicalRange());
const rendered = price.allSeries.find(series => series.options.color === '#42a5f5');
const descending = price.allSeries.find(series => series.options.color === '#42a5f5' && series.options.lineStyle === library.LineStyle.Dotted);
const pattern = price.allSeries.find(series => series.options.color === '#ffeb3b');
assert.equal(pattern.options.autoscaleInfoProvider, undefined);
assert.equal(rendered.data.length, 2); // no extension or intermediate point mutation
assert.equal(JSON.stringify(payload), originalPayload);
assert.equal(rendered.model.Dt(), price.allSeries[0].model.Dt());

const nativeRange = (series, from, to, custom = true) => {
  price.scale.setVisibleLogicalRange({from, to});
  const info = custom ? series.model.zl(Math.floor(from), Math.ceil(to))
    : series.model.pa(Math.floor(from), Math.ceil(to));
  return info && JSON.parse(JSON.stringify(info.Vh()));
};
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-10, `${actual} != ${expected}`);
const physicalRange = series => {
  const [a, b] = [series.data[0], series.data.at(-1)];
  const x0 = price.scale.timeToCoordinate(a.time), x1 = price.scale.timeToCoordinate(b.time);
  const valueAt = x => a.value + (b.value - a.value) * (x - x0) / (x1 - x0);
  const values = [valueAt(Math.max(0, x0)), valueAt(Math.min(price.scale.width(), x1))];
  return {minValue: Math.min(...values), maxValue: Math.max(...values)};
};
// Reproduce the real library's sparse-data failure, then exercise the viewer's
// provider through that same library's custom-provider invocation path.
assert.equal(nativeRange(rendered, 30, 50, false), null);
let range = nativeRange(rendered, 30, 50).priceRange;
let expected = physicalRange(rendered);
close(range.minValue, expected.minValue);
close(range.maxValue, expected.maxValue);
range = nativeRange(descending, 30, 50).priceRange;
expected = physicalRange(descending);
close(range.minValue, expected.minValue);
close(range.maxValue, expected.maxValue);
price.resize(271);
range = nativeRange(rendered, 30.12345, 50.98765).priceRange;
expected = physicalRange(rendered);
close(range.minValue, expected.minValue);
close(range.maxValue, expected.maxValue);
range = nativeRange(rendered, -10, 40).priceRange;
expected = physicalRange(rendered);
close(range.minValue, 10);
close(range.maxValue, expected.maxValue);
range = nativeRange(rendered, 60, 110).priceRange;
expected = physicalRange(rendered);
close(range.minValue, expected.minValue);
close(range.maxValue, 20);
assert.equal(nativeRange(rendered, 100, 110), null); // a rail stops at its endpoint
assert.equal(nativeRange(rendered, -20, -1), null);
assert.deepEqual(rsi.scale.getVisibleLogicalRange(), price.scale.getVisibleLogicalRange());
window.TVChart.unmount();

// A recorded NVDA support rail from the real cached OHLC tape. Weekend dates
// added by forecasts/annotations must not shift its fitted values on candles.
const nvda = JSON.parse(fs.readFileSync('tests/fixtures/barcache/us/NVDA.json', 'utf8'));
const nvdaCandles = nvda.bars.map(row => ({
  time: new Date(row.t * 1000).toISOString().slice(0, 10),
  open: row.o, high: row.h, low: row.l, close: row.c,
})).filter(row => row.time >= '2026-04-23' && row.time <= '2026-06-29');
const nvdaRail = {data: [{time: '2026-04-23', value: 197.2206}, {time: '2026-06-29', value: 189.8}],
  color: '#42a5f5', style: '-', width: 2};
const nvdaPayload = {
  candles: nvdaCandles, rsi14: [{time: nvdaCandles.at(-1).time, value: 50}],
  trendlines: [nvdaRail],
  forecast: [{time: '2026-04-25', value: 200}, {time: '2026-06-30', value: 200}],
  pred_candles: [{time: '2026-05-16', open: 200, high: 201, low: 199, close: 200}],
  segments: [{data: [{time: '2026-06-06', value: 205}, {time: '2026-06-29', value: 205}], color: '#ffeb3b'}],
};
const originalNvda = JSON.stringify(nvdaPayload);
const beforeNvda = charts.length;
window.TVChart.mount(host, nvdaPayload, {readOnly: true});
const [nvdaPrice, nvdaRsi] = charts.slice(beforeNvda);
assert.deepEqual(nvdaPrice.times(), nvdaRsi.times());
const nvdaLine = nvdaPrice.allSeries.find(series => series.options.color === '#42a5f5');
assert.equal(nvdaLine.data.length, nvdaCandles.length);
assert.equal(nvdaLine.data[0], nvdaRail.data[0]);
assert.equal(nvdaLine.data.at(-1), nvdaRail.data.at(-1));
assert.ok(nvdaLine.model.wn.gt()); // run the real pane-view coordinate transform
for (const [index, row] of nvdaCandles.entries()) {
  const point = nvdaLine.data.find(point => point.time === row.time);
  const expected = 197.2206 + (189.8 - 197.2206) * index / (nvdaCandles.length - 1);
  close(point.value, expected);
  const nativePoint = nvdaLine.model.wn.Es[index];
  close(nativePoint._t, expected);
  close(nativePoint.nt, nvdaPrice.scale.timeToCoordinate(row.time));
  assert.ok(Number.isFinite(nativePoint.st));
}
assert.equal(nvdaPrice.allSeries.find(series => series.options.title === 'Forecast close').data.length, 2);
assert.equal(nvdaPrice.allSeries.find(series => series.options.title === 'Forecast').data.length, 1);
assert.equal(JSON.stringify(nvdaPayload), originalNvda);
assert.equal(nvdaLine.data.some(point => point.time === '2026-06-30'), false); // no rail extension
window.TVChart.unmount();
console.log('Bundled-library pixel autoscale, clipped endpoints, original NVDA candle geometry, and shared overlay/RSI times passed');
