// Run with node web/test_chart_export.cjs. Validate the real client and ZIP
// bytes without a browser, database, or access to a running paper account.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {crc32} = require('node:zlib');

class Element {
  constructor() {
    this.events = {}; this.children = []; this.style = {}; this.value = '';
    this.hidden = true; this.dataset = {};
    this.classList = {add() {}, toggle() {}, remove() {}};
  }
  addEventListener(name, callback) { this.events[name] = callback; }
  setAttribute(key, value) { this[key] = value; }
  appendChild(child) { this.children.push(child); }
  querySelectorAll() { return []; }
  set innerHTML(value) { this.children = []; }
  remove() { this.removed = true; }
  click() { this.events.click?.(); }
}

const trade = (symbol, trade_id, pattern = 'triangle', extra = {}) => ({
  symbol, trade_id, pattern, action: 'BUY', entry: 10, current: 11, exit: 12,
  qty: 1, pnl: 2, closed: '2026-10-10T00:00:00Z', ...extra,
});
const log = (symbol, ts, status) => ({symbol, ts, status, pattern: 'triangle', timeframe: '1d'});
const envelope = {books: {
  us: {market: 'us', positions: [trade('AAPL', 'open-1'), trade('AAPL', 'open-2')],
    closed: [trade('OLD', 'old', 'other'), trade('AAPL', 'closed-aapl', 'triangle', {closed: '2026-11-01'})],
    signal_logs: [log('AAPL', '2026-11-01T01:00:00Z', 'accepted'), log('AAPL', '2026-11-01T02:00:00Z', 'rejected')]},
  ph: {market: 'ph', positions: [trade('IOT', 'open-iot')], closed: [trade('IOT', 'closed-iot')],
    signal_logs: [log('IOT', '2026-11-02T01:00:00Z', 'accepted')]},
}};
for (const book of Object.values(envelope.books)) {
  book.closed = book.closed.map(row => ({...row, market: book.market}));
}
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64');

async function setup({fail, cancelAt, missingSymbol = false} = {}) {
  const elements = new Map();
  const get = name => {
    if (!elements.has(name)) elements.set(name, new Element());
    return elements.get(name);
  };
  const markets = ['all', 'us', 'ph'].map(filter => Object.assign(new Element(), {dataset: {filter}}));
  const requests = [], mounts = [], headers = [], links = [], blobs = new Map();
  const body = new Element();
  const document = {body, getElementById: get, querySelector: get, addEventListener() {},
    querySelectorAll: selector => selector === '.filter-pills .pill-btn' ? markets : [],
    createElement(tag) {
      const el = new Element();
      if (tag === 'a') { links.push(el); el.click = () => { el.clicked = true; }; }
      return el;
    },
  };
  const window = {location: {}, TVChart: {
    create: () => ({
      mount(host, data, hooks) { mounts.push({host, data, hooks}); },
      unmount() {},
      captureBlob(header) { headers.push(header); return new Blob([png], {type: 'image/png'}); },
    }),
  }};
  const source = structuredClone(envelope);
  if (missingSymbol) source.books.us.signal_logs.push(log('', '2026-11-03', 'accepted'));
  const context = vm.createContext({window, document, console, FormData, Blob, TextEncoder,
    URLSearchParams, AbortController, setInterval() {}, setTimeout() {}, clearTimeout() {},
    requestAnimationFrame: callback => setImmediate(callback),
    URL: {createObjectURL(blob) { const url = `blob:${blobs.size}`; blobs.set(url, blob); return url; }, revokeObjectURL() {}},
    fetch: async (url, opts = {}) => {
      let data = {};
      if (url === '/api/paper/status') data = source;
      if (url.startsWith('/api/paper/chart?')) {
        const args = Object.fromEntries(new URL(url, 'http://localhost').searchParams);
        requests.push(args);
        if (requests.length === cancelAt) get('paper-cancel-charts').events.click();
        if (fail?.(args)) throw new Error('History unavailable');
        data = {title: `${args.symbol} 1D · triangle · BUY`, timeframe: '1d',
          pattern_note: 'Yellow: detected pattern', trendline_note: 'Rail state: support broken.',
          ohlc: {open: 10, high: 12, low: 9, close: 11, change: 1, change_pct: 10}};
      }
      return {status: 200, ok: true, headers: {get: () => 'application/json'}, json: async () => data};
    },
  });
  for (const file of ['chart_export.js', 'app.js']) vm.runInContext(fs.readFileSync(`web/static/${file}`, 'utf8'), context);
  vm.runInContext('initPaper()', context);
  await new Promise(setImmediate);
  return {get, markets, requests, mounts, headers, links, blobs, body};
}

async function unzip(harness) {
  const link = harness.links.at(-1);
  assert.equal(link.clicked, true);
  assert.equal(link.removed, true);
  assert.match(link.download, /^paper_charts_(all|us|ph)_.*\.zip$/);
  const blob = harness.blobs.get(link.href);
  assert.equal(blob.type, 'application/zip');
  const bytes = Buffer.from(await blob.arrayBuffer());
  const end = bytes.length - 22;
  assert.equal(bytes.readUInt32LE(end), 0x06054b50);
  const count = bytes.readUInt16LE(end + 10), start = bytes.readUInt32LE(end + 16);
  let cursor = start;
  const entries = {};
  for (let i = 0; i < count; i++) {
    assert.equal(bytes.readUInt32LE(cursor), 0x02014b50);
    assert.equal(bytes.readUInt16LE(cursor + 10), 0); // stored PNG bytes
    const size = bytes.readUInt32LE(cursor + 24), nameLength = bytes.readUInt16LE(cursor + 28);
    const name = bytes.subarray(cursor + 46, cursor + 46 + nameLength).toString('utf8');
    const local = bytes.readUInt32LE(cursor + 42);
    assert.equal(bytes.readUInt32LE(local), 0x04034b50);
    const dataStart = local + 30 + bytes.readUInt16LE(local + 26) + bytes.readUInt16LE(local + 28);
    const data = bytes.subarray(dataStart, dataStart + size);
    assert.equal(crc32(data), bytes.readUInt32LE(cursor + 16));
    assert.equal(crc32(data), bytes.readUInt32LE(local + 14));
    assert.equal(entries[name], undefined, 'unique archive paths');
    entries[name] = data;
    cursor += 46 + nameLength + bytes.readUInt16LE(cursor + 30) + bytes.readUInt16LE(cursor + 32);
  }
  assert.equal(cursor, end);
  assert.equal(cursor - start, bytes.readUInt32LE(end + 12));
  const archive = {report: JSON.parse(entries['report.json']), images: {}};
  for (const [name, data] of Object.entries(entries)) {
    if (name.endsWith('.png')) archive.images[name] = data.toString('base64');
  }
  for (const value of Object.values(archive.images)) assert.equal(value, png.toString('base64'));
  return archive;
}

(async () => {
  const all = await setup();
  await all.get('paper-download-charts').events.click();
  const archive = await unzip(all);
  assert.equal(archive.report.exported, 9);
  assert.equal(archive.report.failed, 0);
  assert.equal(new Set(Object.keys(archive.images)).size, 9); // repeated symbols retain one PNG per row
  assert.deepEqual(all.requests.filter(row => row.side === 'open').map(row => row.trade_id), ['open-1', 'open-2', 'open-iot']);
  assert.deepEqual(all.requests.filter(row => row.side === 'closed').map(row => [row.trade_id, row.index]),
    [['closed-aapl', '1'], ['old', '0'], ['closed-iot', '0']]); // sorted display, source indices
  assert.deepEqual(all.requests.filter(row => row.side === 'log').map(row => row.log_time),
    ['2026-11-01T01:00:00Z', '2026-11-01T02:00:00Z', '2026-11-02T01:00:00Z']);
  assert.ok(all.mounts.every(({hooks}) => hooks.readOnly && hooks.includeDrafts && hooks.priceHeight === 600));
  assert.match(all.headers[0][0].text, /AAPL/);
  assert.match(all.headers[0][1].text, /O 10.00.*C 11.00/);
  assert.match(all.headers[0].at(-1).text, /Yellow: detected pattern.*support broken/);
  assert.match(all.get('paper-charts-status').textContent, /9 of 9 charts downloaded in a ZIP/);
  assert.equal(all.get('paper-download-charts').disabled, false);
  assert.equal(all.get('paper-cancel-charts').hidden, true);
  assert.ok(all.mounts[0].host.removed);
  assert.ok(all.get('paper-chart-modal').hidden); // bulk export does not open the modal

  const filtered = await setup();
  filtered.markets[1].events.click();
  filtered.get('paper-closed-pattern').value = 'triangle';
  filtered.get('paper-closed-pattern').events.input();
  filtered.get('paper-log-status').value = 'rejected';
  filtered.get('paper-log-status').events.change();
  await filtered.get('paper-download-charts').events.click();
  assert.equal(filtered.requests.length, 4);
  assert.ok(filtered.requests.every(row => row.market === 'us'));
  assert.deepEqual(filtered.requests.filter(row => row.side === 'closed').map(row => row.trade_id), ['closed-aapl']);
  assert.deepEqual(filtered.requests.filter(row => row.side === 'log').map(row => row.log_time), ['2026-11-01T02:00:00Z']);

  const failed = await setup({fail: row => row.trade_id === 'open-1', missingSymbol: true});
  await failed.get('paper-download-charts').events.click();
  const partial = await unzip(failed);
  assert.equal(partial.report.requested, 10);
  assert.equal(partial.report.exported, 8);
  assert.equal(partial.report.failed, 2);
  assert.deepEqual(partial.report.rows.filter(row => row.outcome === 'failed').map(row => row.error),
    ['History unavailable', 'This row has no symbol to chart']);
  assert.match(failed.get('paper-charts-status').textContent, /2 failed; see report.json/);

  const cancelled = await setup({cancelAt: 2});
  await cancelled.get('paper-download-charts').events.click();
  const stopped = await unzip(cancelled);
  assert.equal(stopped.report.cancelled, true);
  assert.equal(stopped.report.exported, 1);
  assert.equal(stopped.report.rows.filter(row => row.outcome === 'not_exported').length, 8);
  assert.equal(cancelled.requests.length, 2);
  assert.match(cancelled.get('paper-charts-status').textContent, /Cancelled. 1 of 9/);

  const immediate = await setup({cancelAt: 1});
  await immediate.get('paper-download-charts').events.click();
  assert.equal(immediate.links.length, 0);
  assert.match(immediate.get('paper-charts-status').textContent, /Cancelled. 0 of 9/);

  const failedThenCancelled = await setup({fail: () => true, cancelAt: 2});
  await failedThenCancelled.get('paper-download-charts').events.click();
  const failedReport = await unzip(failedThenCancelled);
  assert.equal(failedReport.report.exported, 0);
  assert.equal(failedReport.report.failed, 1);
  assert.equal(failedReport.report.cancelled, true);
  assert.match(failedThenCancelled.get('paper-charts-status').textContent, /1 failed; see report.json/);
  console.log('Paper chart selection, identity, ZIP CRCs, headers, filtering, failures, and cancellation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
