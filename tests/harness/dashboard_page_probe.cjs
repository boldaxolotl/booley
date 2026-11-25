'use strict';
// node dashboard_page_probe.cjs <page.js> <scenario.json>
// Runs the page script (or the page's scripts joined) in a vm with stand-ins for the browser and prints what they did.
// `scenario.steps` then drives the page: {click: id}, {change|input: id, target: {...}},
// {key}, {poll: true} and {release: path} (answer a route answer marked `held`); `views[0]` is
// the page after loading and `views[i + 1]` after step `i`.
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

const elements = {};
const focused = [];
const clipboard = [];
const hiddenIds = new Set(scenario.hidden || []);
const documentListeners = [];
function byId(id) {
  if (!elements[id]) {
    const el = element();
    el.hidden = hiddenIds.has(id);
    el.attributes = {};
    el.setAttribute = (name, value) => { el.attributes[name] = value; };
    el.focus = () => focused.push(id);
    elements[id] = el;
  }
  return elements[id];
}
byId('live');
byId('results');
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
const held = {};
const delays = [];
const timers = [];
let next = 0;
function replyWith(response) {
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

const routes = scenario.routes || {};
function fetchStandIn(url, options) {
  const { cache, method, body } = options || {};
  fetches.push({ url, cache, method, body: body && JSON.parse(body) });
  const path = url.split('?')[0];
  if (path === '/api/snapshot') return replyWith(scenario.responses[next++]);
  const response = (routes[path] || []).shift() || { status: 404, text: 'Not Found' };
  if (!response.held) return replyWith(response);
  return new Promise((resolve) => {
    (held[path] = held[path] || []).push(() => resolve(replyWith(response)));
  });
}

const context = {
  document: {
    getElementById: byId,
    addEventListener: (type, fn) => documentListeners.push({ type, fn }),
  },
  navigator: { clipboard: { writeText: (text) => { clipboard.push(text); return Promise.resolve(); } } },
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
  for (let i = 0; i < 50; i += 1) await new Promise((r) => setImmediate(r));
}

function fire(listeners, type, event) {
  listeners.filter((l) => l.type === type).forEach((l) => l.fn(event));
}

function view() {
  const seen = {};
  for (const [id, el] of Object.entries(elements)) {
    seen[id] = { html: el.html, text: el.textContent, hidden: el.hidden, attributes: { ...el.attributes } };
  }
  return seen;
}

async function step(one) {
  const [kind] = Object.keys(one);
  if (kind === 'poll') timers.shift()();
  else if (kind === 'release') (held[one.release] || []).forEach((release) => release());
  else if (kind === 'key') fire(documentListeners, 'keydown', { key: one.key });
  else fire(byId(one[kind]).listeners, kind, { type: kind, target: one.target });
  await settle();
}

(async () => {
  vm.runInNewContext(fs.readFileSync(jsPath, 'utf8'), context);
  const views = [];
  const renders = [];
  await settle();
  if (scenario.steps) views.push(view());
  for (const one of scenario.steps || []) {
    await step(one);
    views.push(view());
  }
  for (let i = 0; !scenario.steps && i < scenario.responses.length; i += 1) {
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
    views,
    focused,
    clipboard,
    writes: { live: elements.live.writes, results: elements.results.writes },
    storage: store,
  }));
})();
