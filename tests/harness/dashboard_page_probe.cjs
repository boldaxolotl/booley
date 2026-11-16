'use strict';
// node dashboard_page_probe.cjs <dashboard.js> <scenario.json>
// Runs the page script in a vm with stand-ins for the browser and prints what it did.
const fs = require('fs');
const vm = require('vm');

const [jsPath, scenarioPath] = process.argv.slice(2);
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));

function element() {
  const el = { html: '', writes: 0, listeners: [] };
  Object.defineProperty(el, 'innerHTML', {
    get() { return el.html; },
    set(value) { el.html = value; el.writes += 1; },
  });
  el.addEventListener = (type, fn, capture) => el.listeners.push({ type, fn, capture });
  return el;
}

const elements = { live: element(), results: element() };
const store = {};
const blocked = scenario.storage === 'blocked';
const localStorage = {
  getItem(key) {
    if (blocked) throw new Error('storage blocked');
    return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
  },
  setItem(key, value) {
    if (blocked) throw new Error('storage blocked');
    store[key] = String(value);
  },
};

const fetches = [];
const delays = [];
const timers = [];
let next = 0;
function fetchStandIn(url, options) {
  fetches.push({ url, cache: options && options.cache });
  const response = scenario.responses[next++];
  if (response.reject !== undefined) {
    return Promise.reject(new Error(response.reject));
  }
  return Promise.resolve({
    ok: response.status < 400,
    status: response.status,
    json: () => (response.text !== undefined
      ? Promise.reject(new SyntaxError('not JSON'))
      : Promise.resolve(response.body)),
  });
}

const context = {
  document: { getElementById: (id) => elements[id] },
  location: { search: scenario.search },
  localStorage,
  URLSearchParams,
  fetch: fetchStandIn,
  setTimeout: (fn, ms) => { delays.push(ms); timers.push(fn); },
  Date,
  String,
  Math,
  Boolean,
  isNaN,
};

async function settle() {
  for (let i = 0; i < 10; i += 1) await new Promise((r) => setImmediate(r));
}

(async () => {
  vm.runInNewContext(fs.readFileSync(jsPath, 'utf8'), context);
  const renders = [];
  for (let i = 0; i < scenario.responses.length; i += 1) {
    await settle();
    renders.push(elements.live.html + elements.results.html);
    if (scenario.toggle_after === i) {
      const details = { id: 'results-section', open: true };
      for (const l of elements.results.listeners) {
        if (l.type === 'toggle') l.fn({ target: details });
      }
    }
    if (i + 1 < scenario.responses.length) timers.shift()();
  }
  process.stdout.write(JSON.stringify({
    fetches,
    delays,
    renders,
    writes: { live: elements.live.writes, results: elements.results.writes },
    storage: store,
  }));
})();
