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
  'window.testInternals={DataLayer:he,TimeBehavior:Gn,TimeScaleApi:Re,lineDefaults:n,ChartModel:Vn,chartDefaults:Se,seriesDefaults:l,candleDefaults:t,histogramDefaults:r,merge:T};window.LightweightCharts=ze'),
{window: libraryWindow, document: {}, navigator: {language: 'en-US'}, performance});
const library = libraryWindow.LightweightCharts;
const {DataLayer, TimeBehavior, TimeScaleApi, lineDefaults} = libraryWindow.testInternals;
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
  constructor(document) { this.ownerDocument = document; this.style = {}; this.children = []; this.clientHeight = 460; }
  appendChild(child) { this.children.push(child); child.parentElement = this; }
  get offsetTop() {
    return this.parentElement?.children.slice(0, this.parentElement.children.indexOf(this))
      .reduce((height, child) => height + child.clientHeight, 0) || 0;
  }
  setAttribute(key, value) { this[key] = value; }
  remove() {}
  set innerHTML(value) { this.children = []; }
}
const document = {createElement() { return new Element(document); }};
const host = new Element(document);
const charts = [];
const pendingLayouts = new Set();
function flushLayouts() {
  for (let pass = 0; pendingLayouts.size || charts.some(chart => chart.hasPendingDraw()); pass++) {
    assert.ok(pass < 20, 'deferred layouts and logical ranges must settle without a feedback loop');
    for (const chart of charts) {
      if (pendingLayouts.delete(chart)) chart.layout();
      chart.flushDraw();
    }
  }
}

function createChart(el, options) {
  const behavior = new TimeBehavior();
  const layer = new DataLayer(behavior);
  const nativeOptions = merge(chartDefaults(), options);
  behavior.setOptions(nativeOptions);
  let pendingDraw = null, removed = false;
  const native = new ChartModel(invalidation => {
    if (pendingDraw) pendingDraw.ts(invalidation);
    else pendingDraw = invalidation;
  }, nativeOptions, behavior);
  // The actual widgets size their axes independently from their labels. Price
  // labels such as 200.00 are wider than the RSI's 100.0; equal host widths do
  // not initially produce equal plot widths.
  let hostWidth = 880, labelWidth = charts.length % 2 === 0 ? 77 : 53;
  const axisWidth = () => Math.max(labelWidth, nativeOptions.rightPriceScale.minimumWidth);
  let allocatedAxisWidth = axisWidth();
  native.S_(hostWidth - allocatedAxisWidth);
  native.qc()[0].Io(460);
  const timeline = native.St();
  // Invoke the pinned library's actual public width API against the widget
  // geometry assigned by ChartWidget.Pb (0x0 while the date axis is hidden).
  const publicScale = new TimeScaleApi(native, {
    um: () => ({width: nativeOptions.timeScale.visible ? timeline.Hi() : 0, height: 0}),
    Nm: () => ({l() {}, p() {}}),
  }, behavior);
  const allSeries = [];
  const rangeListeners = new Map(), sizeListeners = new Set();
  const scale = {
    fitContent() { publicScale.fitContent(); },
    getVisibleLogicalRange() { const r = timeline.Du(); return r && {from: r.Bs(), to: r.ui()}; },
    setVisibleLogicalRange(range) { publicScale.setVisibleLogicalRange(range); },
    subscribeVisibleLogicalRangeChange(fn) {
      const listener = () => fn(scale.getVisibleLogicalRange());
      rangeListeners.set(fn, listener); timeline.sc().l(listener);
    },
    unsubscribeVisibleLogicalRangeChange(fn) { timeline.sc().v(rangeListeners.get(fn)); rangeListeners.delete(fn); },
    subscribeSizeChange(fn) { sizeListeners.add(fn); },
    unsubscribeSizeChange(fn) { sizeListeners.delete(fn); },
    timeToCoordinate(time) {
      const index = timeline.Oa(behavior.convertHorzItemToInternal(time), false);
      return index == null ? null : timeline.zt(index);
    },
    coordinateToLogical: x => timeline.Nu(x),
    // The public width measures the date-axis widget, which is absent on the
    // price chart when RSI owns the dates. The native plot still has width.
    width: () => publicScale.width(),
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
      primitives: [],
      attachPrimitive(primitive) { this.primitives.push(primitive); primitive.attached({requestUpdate() {}}); },
      detachPrimitive(primitive) { this.primitives = this.primitives.filter(item => item !== primitive); primitive.detached(); },
    };
    allSeries.push(api);
    return api;
  }
  const chart = {
    options: () => nativeOptions, allSeries, layer, scale,
    applyOptions(next) { merge(nativeOptions, next); },
    addCandlestickSeries: options => addSeries('Candlestick', options),
    addHistogramSeries: options => addSeries('Histogram', options),
    addLineSeries: options => addSeries('Line', options),
    timeScale: () => scale,
    paneSize: () => ({width: timeline.Hi(), height: 460}),
    priceScale: () => ({
      width: () => allocatedAxisWidth,
      options: () => nativeOptions.rightPriceScale,
      // The public API schedules widget layout for the next render; reported
      // widths stay at the allocated size until that render actually occurs.
      applyOptions(next) { Object.assign(nativeOptions.rightPriceScale, next); pendingLayouts.add(chart); },
    }),
    subscribeCrosshairMove() {},
    remove() { removed = true; pendingDraw = null; native.S(); pendingLayouts.delete(chart); },
    hasPendingDraw: () => !removed && pendingDraw != null,
    flushDraw() {
      if (removed || !pendingDraw) return;
      const invalidation = pendingDraw; pendingDraw = null;
      // ChartWidget.ns applies these coalesced native invalidation actions on
      // its render frame. Using actual API/merge code catches deferred echoes.
      for (const action of invalidation.Qn()) {
        if (action.qn === 0) timeline.hc();
        else if (action.qn === 1) timeline.lc(action.Ot);
      }
    },
    layout() {
      const oldWidth = timeline.Hi();
      allocatedAxisWidth = axisWidth();
      native.S_(hostWidth - allocatedAxisWidth);
      if (nativeOptions.timeScale.visible && oldWidth !== timeline.Hi()) {
        for (const listener of sizeListeners) listener(scale.width(), 460);
      }
    },
    resize(width) { hostWidth = width + allocatedAxisWidth; chart.layout(); },
    setLabelWidth(width) { labelWidth = width; chart.layout(); },
    panNative(range) { timeline.lc(range); }, // pointer pan updates the model before paint
    subscriptions: () => ({range: rangeListeners.size, size: sizeListeners.size}),
    times: () => layer.Xb.map(point => point.originalTime),
  };
  charts.push(chart);
  return chart;
}

const window = {};
vm.runInNewContext(fs.readFileSync('web/static/chart_draw.js', 'utf8'), {window, document});
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
  {time: candles[5].time, value: 50}, {time: candles[95].time, value: 40},
]};
const payload = {
  candles, rsi14: [{time: candles[99].time, value: 50}],
  segments: [{...rail, color: '#ffeb3b'}], trendlines: [rail, resistance],
  manual_corrections: [{id: 'vertical', kind: 'vert', time: candles[50].time}],
  // A line-only forecast also needs whitespace padding in the RSI pane.
  forecast: [{time: '2024-04-10', value: 32}],
};
const originalPayload = JSON.stringify(payload);
window.TVChart.mount(host, payload, {readOnly: true});
flushLayouts();
const [price, rsi] = charts;
assert.deepEqual(price.times(), rsi.times());
assert.deepEqual(price.scale.getVisibleLogicalRange(), rsi.scale.getVisibleLogicalRange());
assert.equal(price.scale.width(), 0);
assert.ok(price.paneSize().width > 0);
const sameCandlePixels = (a, b, rows) => {
  assert.equal(a.paneSize().width, b.paneSize().width);
  assert.deepEqual(a.scale.getVisibleLogicalRange(), b.scale.getVisibleLogicalRange());
  for (const row of rows) close(a.scale.timeToCoordinate(row.time), b.scale.timeToCoordinate(row.time));
};
const rendered = price.allSeries.find(series => series.options.color === '#42a5f5');
const descending = price.allSeries.find(series => series.options.color === '#42a5f5' && series.options.lineStyle === library.LineStyle.Dotted);
const pattern = price.allSeries.find(series => series.options.color === '#ffeb3b');
assert.equal(pattern.options.autoscaleInfoProvider, undefined);
assert.equal(rendered.data.length, 2); // no extension or intermediate point mutation
assert.equal(JSON.stringify(payload), originalPayload);
assert.equal(rendered.model.Dt(), price.allSeries[0].model.Dt());

const nativeRange = (series, from, to, custom = true, chart = price) => {
  chart.scale.setVisibleLogicalRange({from, to});
  flushLayouts();
  const info = custom ? series.model.zl(Math.floor(from), Math.ceil(to))
    : series.model.pa(Math.floor(from), Math.ceil(to));
  return info && JSON.parse(JSON.stringify(info.Vh()));
};
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-10, `${actual} != ${expected}`);
sameCandlePixels(price, rsi, candles);
// The real correction painter uses the price pane's x for vertical lines in
// both panes. It must also coincide with the RSI candle's own x coordinate.
function verticalPixels(series) {
  const paths = [];
  const context = {save() {}, restore() {}, setLineDash() {}, beginPath() {}, stroke() {},
    moveTo(x, y) { paths.push({x, y}); }, lineTo(x, y) { paths.push({x, y}); }};
  for (const primitive of series.primitives) primitive.paneViews()[0].renderer().draw({
    useMediaCoordinateSpace(draw) { draw({context}); },
  });
  return paths;
}
for (const pane of [price, rsi]) {
  const correction = pane.allSeries.find(series => series.primitives.length);
  close(verticalPixels(correction)[0].x, pane.scale.timeToCoordinate(candles[50].time));
}
const physicalRange = (series, chart = price) => {
  const [a, b] = [series.data[0], series.data.at(-1)];
  const x0 = chart.scale.timeToCoordinate(a.time), x1 = chart.scale.timeToCoordinate(b.time);
  const valueAt = x => a.value + (b.value - a.value) * (x - x0) / (x1 - x0);
  const values = [valueAt(Math.max(0, x0)), valueAt(Math.min(chart.paneSize().width, x1))];
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
rsi.resize(271);
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
// Rounded logical slots may still contain an endpoint after its pixel leaves
// the plot. Neither ascending nor descending rails may retain those bounds.
for (const [series, from, to] of [
  [rendered, 10.9, 50], [rendered, 50, 89.1],
  [descending, 5.9, 50], [descending, 50, 94.1],
]) {
  assert.ok(nativeRange(series, from, to, false));
  const clipped = nativeRange(series, from, to).priceRange;
  const expected = physicalRange(series);
  close(clipped.minValue, expected.minValue);
  close(clipped.maxValue, expected.maxValue);
}
for (const [series, from, to, endpoint, side] of [
  [rendered, 90.9, 99, rendered.data.at(-1), 'left'],
  [rendered, 0, 9.1, rendered.data[0], 'right'],
  [descending, 95.9, 100, descending.data.at(-1), 'left'],
  [descending, 0, 4.1, descending.data[0], 'right'],
]) {
  assert.ok(nativeRange(series, from, to, false));
  assert.equal(nativeRange(series, from, to), null);
  const x = price.scale.timeToCoordinate(endpoint.time);
  assert.ok(side === 'left' ? x < 0 : x > price.paneSize().width);
}
price.scale.setVisibleLogicalRange({from: 30, to: 50});
flushLayouts();
const margins = {above: 7, below: 11};
const info = rendered.options.autoscaleInfoProvider(() => ({
  priceRange: {minValue: -1000, maxValue: 1000}, margins,
}));
assert.equal(info.margins, margins);
close(info.priceRange.minValue, physicalRange(rendered).minValue);
close(info.priceRange.maxValue, physicalRange(rendered).maxValue);
assert.deepEqual(rsi.scale.getVisibleLogicalRange(), price.scale.getVisibleLogicalRange());
sameCandlePixels(price, rsi, candles);
rsi.scale.setVisibleLogicalRange({from: 12.25, to: 45.75});
flushLayouts();
sameCandlePixels(price, rsi, candles);
assert.deepEqual(price.scale.getVisibleLogicalRange(), {from: 12.25, to: 45.75});
// A zoom/pan may expose a more expensive price or a longer axis label. The
// resulting layout notification must widen both axes and preserve alignment.
price.setLabelWidth(105);
flushLayouts();
sameCandlePixels(price, rsi, candles);
rsi.setLabelWidth(119);
flushLayouts();
sameCandlePixels(price, rsi, candles);
assert.deepEqual(price.scale.getVisibleLogicalRange(), {from: 12.25, to: 45.75});
for (const pane of [price, rsi]) {
  const correction = pane.allSeries.find(series => series.primitives.length);
  close(verticalPixels(correction)[0].x, pane.scale.timeToCoordinate(candles[50].time));
}
// Rapid interactions can arrive before the reflected public API changes have
// painted. The latest user pan must win over either pane's older queued echo.
price.panNative({from: 10, to: 20});
rsi.panNative({from: 30, to: 40});
price.panNative({from: 50, to: 60});
flushLayouts();
sameCandlePixels(price, rsi, candles);
assert.deepEqual(price.scale.getVisibleLogicalRange(), {from: 50, to: 60});
rsi.scale.setVisibleLogicalRange({from: 43.12345, to: 61.98765});
rsi.scale.setVisibleLogicalRange({from: 30.12345, to: 50.98765});
flushLayouts();
sameCandlePixels(price, rsi, candles);
close(price.scale.getVisibleLogicalRange().from, 30.12345);
close(price.scale.getVisibleLogicalRange().to, 50.98765);
price.setLabelWidth(143);
flushLayouts();
sameCandlePixels(price, rsi, candles);
close(price.scale.getVisibleLogicalRange().from, 30.12345);
close(price.scale.getVisibleLogicalRange().to, 50.98765);
assert.deepEqual(price.subscriptions(), {range: 1, size: 0});
assert.deepEqual(rsi.subscriptions(), {range: 1, size: 0});
window.TVChart.unmount();
assert.deepEqual(price.subscriptions(), {range: 0, size: 0});
assert.deepEqual(rsi.subscriptions(), {range: 0, size: 0});

// A recorded NVDA support rail from the real cached OHLC tape. Weekend dates
// added by forecasts/annotations must not shift its fitted values on candles.
const nvda = JSON.parse(fs.readFileSync('tests/fixtures/barcache/us/NVDA.json', 'utf8'));
const nvdaCandles = nvda.bars.map(row => ({
  time: new Date(row.t * 1000).toISOString().slice(0, 10),
  open: row.o, high: row.h, low: row.l, close: row.c,
})).filter(row => row.time >= '2026-04-23' && row.time <= '2026-06-29');
const nvdaRail = {data: [{time: '2026-04-23', value: 197.2206}, {time: '2026-06-29', value: 189.8}],
  color: '#42a5f5', style: '-', width: 2};
const nvdaValueAt = index => 197.2206 + (189.8 - 197.2206) * index / (nvdaCandles.length - 1);
const innerNvdaRail = {...nvdaRail, color: '#4fc3f7', data: [
  {time: nvdaCandles[5].time, value: nvdaValueAt(5)},
  {time: nvdaCandles.at(-6).time, value: nvdaValueAt(nvdaCandles.length - 6)},
]};
const nvdaPayload = {
  candles: nvdaCandles, rsi14: [{time: nvdaCandles.at(-1).time, value: 50}],
  trendlines: [nvdaRail, innerNvdaRail],
  forecast: [{time: '2026-04-25', value: 200}, {time: '2026-06-30', value: 200}],
  pred_candles: [{time: '2026-05-16', open: 200, high: 201, low: 199, close: 200}],
  segments: [{data: [{time: '2026-06-06', value: 205}, {time: '2026-06-29', value: 205}], color: '#ffeb3b'}],
};
const originalNvda = JSON.stringify(nvdaPayload);
const beforeNvda = charts.length;
window.TVChart.mount(host, nvdaPayload, {readOnly: true});
flushLayouts();
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
const innerLine = nvdaPrice.allSeries.find(series => series.options.color === '#4fc3f7');
assert.ok(innerLine.data.length > 2);
const startIndex = nvdaPrice.scale.coordinateToLogical(nvdaPrice.scale.timeToCoordinate(innerLine.data[0].time));
const endIndex = nvdaPrice.scale.coordinateToLogical(nvdaPrice.scale.timeToCoordinate(innerLine.data.at(-1).time));
for (const [from, to] of [[0, startIndex - 0.9], [endIndex + 0.9, endIndex + 5]]) {
  assert.ok(nativeRange(innerLine, from, to, false, nvdaPrice));
  assert.equal(nativeRange(innerLine, from, to, true, nvdaPrice), null);
}
sameCandlePixels(nvdaPrice, nvdaRsi, nvdaCandles);
window.TVChart.unmount();

// Without RSI, the price chart owns its visible date axis. Its autoscale must
// use the same physical geometry and exclude rounded offscreen endpoints.
const beforeNoRsi = charts.length;
window.TVChart.mount(host, {candles, trendlines: [rail]}, {readOnly: true});
flushLayouts();
assert.equal(charts.length, beforeNoRsi + 1);
const noRsi = charts[beforeNoRsi];
assert.ok(noRsi.scale.width() > 0);
assert.equal(noRsi.scale.width(), noRsi.paneSize().width);
const noRsiRail = noRsi.allSeries.find(series => series.options.color === '#42a5f5');
const noRsiBounds = nativeRange(noRsiRail, 30.12345, 50.98765, true, noRsi).priceRange;
const noRsiExpected = physicalRange(noRsiRail, noRsi);
close(noRsiBounds.minValue, noRsiExpected.minValue);
close(noRsiBounds.maxValue, noRsiExpected.maxValue);
for (const [from, to] of [[0, 9.1], [90.9, 99]]) {
  assert.ok(nativeRange(noRsiRail, from, to, false, noRsi));
  assert.equal(nativeRange(noRsiRail, from, to, true, noRsi), null);
}
window.TVChart.unmount();
console.log('Bundled-library rail geometry, shared pane pixels, correction lines, resize/axis layout, and subscription cleanup passed');
