// Exercise the real Paper client: strategy choice and single/both start requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() {
    this.events = {}; this.children = []; this.style = {}; this.value = '';
    this.checked = false; this.disabled = false; this.dataset = {};
    this.classList = {add() {}, remove() {}, toggle() {}};
  }
  addEventListener(name, callback) { this.events[name] = callback; }
  dispatchEvent(event) { return this.events[event.type]?.(event); }
  appendChild(child) { this.children.push(child); }
  querySelectorAll() { return []; }
  setAttribute(key, value) { this[key] = value; }
  set innerHTML(value) { this.children = []; }
}
const elements = new Map();
const get = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const starts = ['us', 'ph'].map(book => Object.assign(new Element(), {dataset: {book}}));
const requests = [];
const errors = [];
const document = {
  getElementById: get, querySelector: get,
  querySelectorAll: selector => selector === '.book-start' ? starts : [],
  createElement: () => new Element(), addEventListener() {},
};
const context = vm.createContext({
  document, window: {location: {}}, console: {...console, error: (...args) => errors.push(args)}, FormData, Event,
  setInterval() {}, setTimeout() {}, clearTimeout() {},
  fetch: async (url, opts = {}) => {
    if (opts.method === 'POST') requests.push({url, body: JSON.parse(opts.body)});
    const data = url === '/api/rules' ? {rules: []} : {books: {}, clocks: {}};
    return {status: 200, ok: true, headers: {get: () => 'application/json'}, json: async () => data};
  },
});
vm.runInContext(fs.readFileSync('web/static/app.js', 'utf8'), context);
vm.runInContext('initPaper()', context);

(async () => {
  await new Promise(setImmediate);
  for (const [i, market] of ['us', 'ph'].entries()) {
    get(`${market}-n`).value = '10';
    get(`${market}-patterns-v2`).checked = true;
    get(`${market}-indicators-only`).checked = true;
    get(`${market}-indicators-only`).dispatchEvent(new Event('change'));
    get(`${market}-patterns-v2`).checked = true;
    get(`${market}-rule`).value = 'old-rule';
    get(`${market}-rsi-divergence`).checked = true;
    get(`${market}-rsi-divergence`).dispatchEvent(new Event('change'));
    for (const key of ['indicators-only', 'rule']) {
      assert.equal(get(`${market}-${key}`).disabled, true);
    }
    for (const key of ['patterns-v2', 'pattern-only']) {
      assert.equal(get(`${market}-${key}`).disabled, false);
    }
    assert.equal(get(`${market}-indicators-only`).checked, false);
    assert.equal(get(`${market}-patterns-v2`).checked, true);
    assert.equal(get(`${market}-rule`).value, '');
    await starts[i].events.click();
    assert.equal(requests.at(-1).url, '/api/paper/start');
    assert.equal(requests.at(-1).body.market, market);
    assert.equal(requests.at(-1).body.rsi_divergence, true);
    assert.equal(requests.at(-1).body.patterns_v2, true);
  }
  // RSI-only remains available by turning off Patternsv2.
  get('ph-patterns-v2').checked = false;
  await starts[1].events.click();
  assert.equal(requests.at(-1).body.rsi_divergence, true);
  assert.equal(requests.at(-1).body.patterns_v2, false);
  get('us-rsi-divergence').checked = false;
  get('us-rsi-divergence').dispatchEvent(new Event('change'));
  assert.equal(get('us-rule').disabled, false);
  assert.equal(get('us-patterns-v2').disabled, false);
  get('us-patterns-v2').checked = true;
  get('ph-patterns-v2').checked = true;
  await get('paper-start-both').events.click();
  assert.equal(requests.at(-1).url, '/api/paper/start-both');
  assert.equal(requests.at(-1).body.us.rsi_divergence, false);
  assert.equal(requests.at(-1).body.us.patterns_v2, true);
  assert.equal(requests.at(-1).body.ph.rsi_divergence, true);
  assert.equal(requests.at(-1).body.ph.patterns_v2, true);
  assert.deepEqual(errors, []);
  console.log('Paper RSI Divergence controls and start requests pass');
})().catch(error => {console.error(error); process.exitCode = 1;});
