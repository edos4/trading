// The explorer draws with the interactive viewer, not the server-rendered PNG.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() {
    this.events = {}; this.children = []; this.style = {}; this.value = '';
    this.hidden = true; this.dataset = {}; this.classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => this.classes.add(name)),
      remove: (...names) => names.forEach((name) => this.classes.delete(name)),
      toggle: (name, on) => { if (on === undefined ? !this.classes.has(name) : on) this.classes.add(name); else this.classes.delete(name); },
    };
  }
  addEventListener(name, fn) { this.events[name] = fn; }
  removeEventListener(name) { delete this.events[name]; }
  setAttribute(key, value) { this[key] = value; }
  remove() { this.removed = true; }
  focus() {}
  appendChild(child) { this.children.push(child); return child; }
  set innerHTML(value) { this.children = []; this.html = value; }
  get innerHTML() { return this.html; }
}

const elements = new Map();
const get = (key) => { if (!elements.has(key)) elements.set(key, new Element()); return elements.get(key); };
const document = {
  body: new Element(), getElementById: get, querySelector: get, querySelectorAll: () => [],
  createElement: () => new Element(), addEventListener() {}, removeEventListener() {},
};

const viewer = {
  title: 'AAPL 1D', symbol: 'AAPL', timeframe: '1d',
  ohlc: {open: 10, high: 12, low: 9, close: 11, change: 1, change_pct: 10},
  candles: [{time: '2024-01-02', open: 10, high: 11, low: 9, close: 10},
            {time: '2024-01-03', open: 10, high: 12, low: 10, close: 11}],
  volume: [{time: '2024-01-02', value: 1}, {time: '2024-01-03', value: 2}],
  vwap: [{time: '2024-01-02', value: 9.8}, {time: '2024-01-03', value: 10.5}],
  rsi14: [{time: '2024-01-03', value: 61.2}],
  levels: [{price: 5, title: 'stop', color: '#ef5350'}],
  markers: [], segments: [], trendlines: [],
  pattern_note: 'Yellow: detected pattern', trendline_note: '',
};

const post = {body: null};
let viewerAvailable = true;
let mountThrows = false;
const fetchStub = async (url, opts) => {
  const body = url.includes('/api/symbols')
    ? {symbols: [{symbol: 'AAPL', exchange: 'NASDAQ'}]}
    : {symbol: 'AAPL', exchange: 'NASDAQ', timeframe: '1d', bars: 2, ohlc: viewer.ohlc,
       signals: [], scan: 'none', scan_error: null, chart_png_b64: 'PNG', csv: 'Date,Close\n',
       viewer: viewerAvailable ? viewer : null};
  if (opts && opts.body) post.body = JSON.parse(opts.body);
  return {status: 200, ok: true, headers: {get: () => 'application/json'}, json: async () => body};
};

const TVChart = {
  mounts: [], unmounts: 0, attempts: 0,
  mount(el, data, hooks) {
    this.attempts += 1;
    if (mountThrows) throw new Error('TradingView chart library is not loaded');
    this.mounts.push({el, data, hooks});
  },
  unmount() { this.unmounts += 1; },
};
const window = {TB_PAGE: 'explorer', TVChart, location: {}, innerWidth: 1200, innerHeight: 800,
                TB_MARKETS: [{id: 'us', default_n_symbols: 50}]};
const context = vm.createContext({window, document, console, fetch: fetchStub,
  FormData, Blob: class {}, URL: {createObjectURL: () => 'blob:', revokeObjectURL() {}}, atob: () => ''});
vm.runInContext(fs.readFileSync('web/static/app.js', 'utf8'), context);

const openFirstSymbol = async () => {
  vm.runInContext('initExplorer()', context);
  await new Promise(setImmediate);
  const row = get('symbol-list').children[0];
  assert.ok(row, 'no symbol rows rendered');
  await row.onclick();
};

(async () => {
  await openFirstSymbol();
  assert.equal(post.body.include_viewer, true, 'the explorer must ask for the viewer payload');
  assert.equal(TVChart.mounts.length, 1, 'the viewer must be mounted');
  const [{el, data, hooks}] = TVChart.mounts;
  assert.equal(el, get('chart-host'), 'the viewer mounts into the chart host');
  assert.equal(data, viewer);
  assert.equal(hooks.toolbarHost, get('chart-tools'));
  // The explorer loads no chart_draw.js: no trade identity means corrections
  // could not be kept, so the toolbar carries only the overlay toggles.
  assert.equal(hooks.readOnly, undefined);
  assert.equal(typeof hooks.onCandle, 'function');
  // The server PNG is not shown, and the hover line carries the window summary.
  assert.equal(get('chart').hidden, true);
  assert.equal(get('chart-host').hidden, false);
  assert.equal(get('chart-tools').hidden, false);
  assert.equal(get('chart-ohlc').hidden, false);
  assert.match(get('chart-ohlc').textContent, /O 10\.00  H 12\.00  L 9\.00  C 11\.00/);
  assert.match(get('chart-ohlc').textContent, /VWAP 10\.50/);
  assert.equal(get('chart-wrap').classes.has('viewer'), true);
  // A crosshair move rewrites the line for that bar.
  hooks.onCandle(viewer.candles[1]);
  assert.match(get('chart-ohlc').textContent, /2024-01-03  O 10\.00/);
  assert.match(get('chart-ohlc').textContent, /\+1\.00 \(\+10\.00%\)/);
  hooks.onCandle(null);
  assert.match(get('chart-ohlc').textContent, /Last displayed bar/);

  // An older/MCP-shaped payload has no viewer: fall back to the rendered PNG.
  viewerAvailable = false;
  TVChart.mounts.length = 0;
  await get('symbol-list').children[0].onclick();
  assert.equal(TVChart.attempts, 1);
  assert.equal(TVChart.mounts.length, 0);
  assert.equal(get('chart').hidden, false);
  assert.equal(get('chart').src, 'data:image/png;base64,PNG');
  assert.equal(get('chart-host').hidden, true);
  assert.equal(get('chart-ohlc').hidden, true);
  assert.equal(get('chart-wrap').classes.has('viewer'), false);
  assert.equal(get('status').textContent.includes('chart viewer unavailable'), false);

  // A viewer that cannot mount must not leave the panel empty either, and the
  // reason has to survive: the caller writes the status line after this.
  viewerAvailable = true;
  mountThrows = true;
  await get('symbol-list').children[0].onclick();
  assert.equal(TVChart.attempts, 2);
  assert.equal(TVChart.mounts.length, 0);
  assert.equal(get('chart').hidden, false);
  assert.equal(get('chart-host').hidden, true);
  assert.match(get('status').textContent, /chart viewer unavailable.*showing the rendered chart/);
  console.log('explorer client OK');
})().catch((err) => { console.error(err); process.exit(1); });
