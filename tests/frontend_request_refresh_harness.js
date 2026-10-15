'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const cacheMetrics = require(path.join(root, 'web', 'cache-metrics.js'));

function environment() {
  const nodes = new Map(), listeners = new Map(), requests = [], timers = [], blobs = [];
  const node = selector => {
    if (nodes.has(selector)) return nodes.get(selector);
    const handlers = new Map();
    const value = {
      innerHTML: '', textContent: '', hidden: false, disabled: false, open: false,
      value: selector === '#requestSort' ? 'time_desc' : '', dataset: {}, attributes: {},
      classList: { toggle() {}, add() {}, remove() {} },
      setAttribute(name, data) { this.attributes[name] = String(data); },
      removeAttribute(name) { delete this.attributes[name]; },
      addEventListener(name, callback) { handlers.set(name, callback); },
      querySelector(part) { return node(`${selector} ${part}`); }, querySelectorAll() { return []; },
      showModal() { this.open = true; },
      close() { this.open = false; if (handlers.has('close')) handlers.get('close')(); },
      appendChild() {}, remove() {}, click() {}, getBoundingClientRect() { return {}; }
    };
    nodes.set(selector, value);
    return value;
  };
  const window = {
    CodexCacheMetrics: cacheMetrics, location: { search: '' },
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(callback);
    },
    dispatchEvent(event) { (listeners.get(event.type) || []).forEach(callback => callback(event)); },
    matchMedia() { return { matches: false }; },
    setTimeout: (callback, ms) => { const timer = { callback, ms, cleared: false }; timers.push(timer); return timer; },
    clearTimeout(timer) { if (timer) timer.cleared = true; },
    setInterval() { return 1; }, clearInterval() {}, requestAnimationFrame(callback) { callback(); },
    EventSource: { OPEN: 1 }
  };
  const document = {
    body: node('body'), activeElement: null,
    querySelector: node, querySelectorAll() { return []; },
    getElementById: id => node(`#${id}`), createElement: tag => node(`created:${tag}`),
    addEventListener() {}
  };
  const context = vm.createContext({
    window, document, Intl, URLSearchParams, AbortController, EventSource: window.EventSource,
    localStorage: { getItem() { return null; }, setItem() {} },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options && options.detail; } },
    Blob: class { constructor(parts) { this.parts = parts; blobs.push(this); } },
    URL: { createObjectURL() { return 'blob:test'; }, revokeObjectURL() {} },
    setTimeout: window.setTimeout, clearTimeout: window.clearTimeout,
    fetch(url, options = {}) {
      return new Promise((resolve, reject) => {
        requests.push({ url: String(url), options, reject,
          resolve(payload) { resolve({ ok: true, json: async () => payload }); },
          fail(status) { resolve({ ok: false, status }); }
        });
      });
    }
  });
  return { window, document, context, node, requests, timers, blobs };
}

async function settle() { for (let i = 0; i < 15; i += 1) await Promise.resolve(); }
const row = (id, extra = {}) => ({ request_id: id, output_tokens: 400, input_tokens: 1000, ...extra });
const page = (id, current = 1, extra = {}) => ({ data: [row(id, extra)], total: 120, page: current, page_size: 30 });

function analyticsEnvironment() {
  const env = environment();
  env.snapshot = { range: 'all', provider: 'all', workspace: 'all', model: 'all' };
  env.window.CodexDashboard = { getState: () => env.snapshot, getCostMode: () => 'after' };
  let source = fs.readFileSync(path.join(root, 'web', 'analytics.js'), 'utf8');
  source = source.replace("document.addEventListener('DOMContentLoaded', initialize, { once: true });", `
    window.__test = { state, loadQueryData, loadRequestPage, loadTasks, requestSpeedInfo,
      detailRows, exportRequestPage, openRequestDetail, bindEvents };
  `);
  vm.runInContext(source, env.context, { filename: 'analytics.js' });
  env.api = env.window.__test;
  return env;
}

async function testAnalyticsRefresh() {
  const env = analyticsEnvironment(), api = env.api;
  api.state.dashboard = { dataVersion: 1, summary: {} };
  let complete = false;
  const initial = api.loadQueryData().then(() => { complete = true; });
  assert.equal(env.requests.length, 2);
  env.requests[0].resolve(page('fresh'));
  await settle();
  assert.equal(api.state.requests.data[0].request_id, 'fresh', 'request rows render before task aggregation finishes');
  assert.equal(env.node('#requestsPanel').attributes['aria-busy'], 'false');
  assert.equal(api.state.tasksLoading, true);
  assert.equal(complete, false);
  await api.loadQueryData();
  assert.equal(env.requests.length, 2, 'same data version does not issue duplicate queries');
  env.requests[1].resolve({ data: [] });
  await initial;

  api.state.requestPage = 2;
  api.state.dashboard = { dataVersion: 2, summary: {} };
  const background = api.loadQueryData();
  assert.match(env.requests[2].url, /page=2/);
  assert.equal(api.state.requestPage, 2, 'new data retains the selected page');
  env.snapshot = { ...env.snapshot, provider: 'provider-b', workspace: 'workspace-b' };
  const filtered = api.loadQueryData();
  assert.equal(api.state.requestPage, 1, 'changing filters starts on the first page');
  assert.equal(env.requests[2].options.signal.aborted, true);
  env.requests[4].resolve(page('provider-b'));
  env.requests[5].resolve({ data: [{ task_id: 'task-b', summary: {} }] });
  await filtered;
  env.requests[2].resolve(page('stale-page', 2));
  env.requests[3].resolve({ data: [{ task_id: 'stale-task', summary: {} }] });
  await background;
  assert.equal(api.state.requests.data[0].request_id, 'provider-b');
  assert.equal(api.state.tasks[0].task_id, 'task-b', 'old filter responses cannot overwrite the current filter');

  env.node('#requestSort').value = 'token_desc';
  const tokenSort = api.loadRequestPage();
  env.node('#requestSort').value = 'time_desc';
  const recentSort = api.loadRequestPage();
  assert.equal(env.requests[6].options.signal.aborted, true);
  env.requests[7].resolve(page('recent-sort'));
  await recentSort;
  env.requests[6].resolve(page('stale-sort'));
  await tokenSort;
  assert.equal(api.state.requests.data[0].request_id, 'recent-sort');

  api.state.requestPage = 3;
  const third = api.loadRequestPage();
  api.state.requestPage = 2;
  const second = api.loadRequestPage();
  env.requests[9].resolve(page('second-page', 2));
  await second;
  env.requests[8].resolve(page('stale-third', 3));
  await third;
  assert.equal(api.state.requests.page, 2, 'late pagination responses are rejected');

  const retry = api.loadQueryData(true);
  assert.equal(env.requests.length, 12, 'explicit refresh reloads the same filter and data version');
  env.requests[10].reject(new Error('network unavailable'));
  env.requests[11].resolve({ data: [] });
  await retry;
  assert.equal(api.state.requests.data[0].request_id, 'second-page', 'background errors retain the last useful rows');
  assert.match(env.node('#requestsContext').textContent, /更新失败/);
  assert.equal(api.state.requestsLoading, false);

  api.state.requestPage = 5;
  const shrink = api.loadRequestPage({ force: true });
  env.requests[12].resolve({ data: [], total: 31, page: 5, page_size: 30 });
  await settle();
  assert.match(env.requests[13].url, /page=2/);
  env.requests[13].resolve({ data: [row('clamped')], total: 31, page: 2, page_size: 30 });
  await shrink;
  assert.equal(api.state.requestPage, 2, 'a shrinking dataset clamps an invalid page');
}

async function testRequestSpeedAndDetails() {
  const env = analyticsEnvironment(), api = env.api;
  const valid = row('estimate', { request_duration_estimate_ms: 2000,
    request_output_tokens_per_second_estimate: 200, request_timing_source: 'token_usage_record' });
  assert.equal(api.requestSpeedInfo(valid).text, '≈ 200.0 tok/s');
  assert.match(api.requestSpeedInfo(valid).title, /包含首字等待/);
  assert.equal(api.requestSpeedInfo({ ...valid, output_tokens: 199 }).text, '—');
  assert.equal(api.requestSpeedInfo({ ...valid, request_duration_estimate_ms: 999 }).text, '—');
  assert.equal(api.requestSpeedInfo({ ...valid, request_speed_exclusion_reason: 'ambiguous_usage_match' }).text, '—');
  assert.match(api.requestSpeedInfo({ ...valid, request_speed_exclusion_reason: 'ambiguous_usage_match' }).reason, /匹配/);
  assert.equal(api.requestSpeedInfo(row('turn-only', { speed_duration_ms: 1000, output_tokens_per_second: 400, ttft_ms: 100 })).text, '—', 'turn throughput and TTFT never fill the request estimate');
  const detail = api.detailRows({ ...valid, speed_duration_ms: 60000, ttft_ms: 500 });
  assert.equal(detail.find(([label]) => label === '请求估算耗时（含首字等待）')[1], '≈ 2.000 秒');
  assert.equal(detail.find(([label]) => label === '轮次耗时')[1], '60,000 ms');
  api.state.requests.data = [valid];
  api.exportRequestPage();
  const csv = env.blobs[0].parts[0];
  const headers = csv.split('\r\n')[0].replace(/^\ufeff/, '').split(',');
  for (const field of ['request_duration_estimate_ms', 'request_output_tokens_per_second_estimate', 'request_timing_source', 'request_speed_exclusion_reason']) assert.ok(headers.includes(field));
  assert.ok(!headers.includes('speed_duration_ms'));
  assert.match(csv, /2000,200,token_usage_record/);

  api.bindEvents();
  const first = api.openRequestDetail('first');
  const latest = api.openRequestDetail('latest');
  assert.equal(env.requests[0].options.signal.aborted, true);
  env.requests[1].resolve({ request: { ...valid, model: 'latest-model' } });
  await latest;
  env.requests[0].resolve({ request: { ...valid, model: 'stale-model' } });
  await first;
  assert.match(env.node('#requestDetailFacts').innerHTML, /latest-model/);
  assert.doesNotMatch(env.node('#requestDetailFacts').innerHTML, /stale-model/);
  const closing = api.openRequestDetail('closing');
  env.node('#requestDetailDialog').close();
  assert.equal(env.requests[2].options.signal.aborted, true);
  env.requests[2].resolve({ request: { ...valid, model: 'closed-model' } });
  await closing;
  assert.equal(env.node('#requestDetailDialog').open, false, 'a late response must not reopen a closed dialog');
}

async function testSameVersionRecoveryAndEarlyFilters() {
  const env = analyticsEnvironment(), api = env.api;
  api.state.dashboard = { dataVersion: 10, summary: {} };
  api.bindEvents();
  const initial = api.loadQueryData();
  env.requests[0].reject(new Error('temporary failure'));
  env.requests[1].resolve({ data: [] });
  await initial;
  const recovered = api.loadQueryData();
  assert.equal(env.requests.length, 3, 'a failed query retries on the next dashboard render even if its data version is unchanged');
  env.requests[2].resolve(page('recovered'));
  await recovered;
  assert.equal(api.state.requestsError, '');
  env.snapshot = { ...env.snapshot, model: 'early-filter' };
  env.window.dispatchEvent({ type: 'codex-dashboard:loading', detail: { state: env.snapshot } });
  assert.equal(env.requests.length, 5, 'new filter queries start without waiting for dashboard aggregation');
  assert.match(env.requests[3].url, /model=early-filter/);
  assert.equal(api.state.requests.data.length, 0, 'old-filter rows are cleared while the new filter loads');
  env.requests[3].resolve(page('filtered'));
  env.requests[4].resolve({ data: [] });
  await settle();
  assert.equal(api.state.requests.data[0].request_id, 'filtered');
}

function appEnvironment() {
  const env = environment();
  let source = fs.readFileSync(path.join(root, 'web', 'app.js'), 'utf8');
  const startup = source.indexOf("  window.addEventListener('pywebviewready', loadDesktopPreferences);");
  assert.ok(startup > 0);
  source = source.slice(0, startup) + `
    window.__renders = [];
    renderAll = (data, options = {}) => window.__renders.push({ data, options });
    window.__test = { state, fetchDashboard, refreshNow, scheduleRefresh };
  })();`;
  vm.runInContext(source, env.context, { filename: 'app.js' });
  env.api = env.window.__test;
  return env;
}

async function testManualAndSseRefresh() {
  const env = appEnvironment(), api = env.api;
  Object.assign(api.state, { range: 'custom', customStartDate: '2026-10-01', customEndDate: '2026-10-15', provider: 'provider-b', workspace: 'workspace-b', model: 'model-b' });
  const manual = api.refreshNow();
  assert.equal(api.refreshNow(), manual, 'double clicks share the same scan');
  assert.equal(env.requests.length, 1);
  assert.equal(env.requests[0].url, '/api/scan/now');
  assert.equal(env.requests[0].options.method, 'POST');
  assert.equal(env.requests[0].options.headers['X-Dashboard-Request'], '1');
  assert.equal(env.node('#refreshButton').disabled, true);
  api.scheduleRefresh({ data: JSON.stringify({ version: 2 }) });
  assert.equal(env.timers.length, 0, 'the scan notification does not cause an extra dashboard fetch');
  env.requests[0].resolve({ health: { version: 2 } });
  await settle();
  assert.equal(env.requests.length, 2, 'dashboard is fetched only after the scan completes');
  const params = new URLSearchParams(env.requests[1].url.split('?')[1]);
  assert.equal(params.get('provider'), 'provider-b');
  assert.equal(params.get('workspace'), 'workspace-b');
  assert.equal(params.get('model'), 'model-b');
  assert.equal(params.get('start_date'), '2026-10-01');
  assert.equal(params.get('end_date'), '2026-10-15');
  env.requests[1].resolve({ data_version: 2, summary: {}, generated_at: '2026-10-15T00:00:00Z' });
  await manual;
  assert.equal(api.state.data.dataVersion, 2);
  assert.equal(env.window.__renders[0].options.refreshQueries, true);
  assert.equal(env.node('#refreshButton').disabled, false);

  api.scheduleRefresh({ data: JSON.stringify({ version: 3 }) });
  api.scheduleRefresh({ data: JSON.stringify({ version: 3 }) });
  assert.equal(env.timers.length, 1, 'repeated SSE versions are coalesced');
  env.timers[0].callback();
  const poll = api.fetchDashboard({ silent: true, eventVersion: '3' });
  assert.equal(env.requests.length, 3, 'same-version concurrent dashboard reads are shared');
  assert.match(env.requests[2].url, /^\/api\/dashboard/);
  assert.notEqual(env.requests[2].options.method, 'POST', 'automatic updates never start a scan');
  env.requests[2].resolve({ data_version: 3, summary: {} });
  await poll;

  const old = api.fetchDashboard();
  api.state.provider = 'new-provider';
  const recent = api.fetchDashboard();
  assert.equal(env.requests[3].options.signal.aborted, true);
  env.requests[4].resolve({ data_version: 5, summary: { total_tokens: 5 } });
  await recent;
  env.requests[3].resolve({ data_version: 4, summary: { total_tokens: 4 } });
  await old;
  assert.equal(api.state.data.dataVersion, 5, 'an aborted dashboard response cannot overwrite newer filters');
}

async function testNewerSseDuringManualDashboardRead() {
  const env = appEnvironment(), api = env.api;
  const manual = api.refreshNow();
  api.scheduleRefresh({ data: JSON.stringify({ version: 2 }) });
  env.requests[0].resolve({ health: { version: 2 } });
  await settle();
  assert.equal(api.state.dashboardEventVersion, '2');
  api.scheduleRefresh({ data: JSON.stringify({ version: 3 }) });
  api.scheduleRefresh({ data: JSON.stringify({ version: 3 }) });
  assert.equal(env.timers.length, 0, 'SSE signals wait for the active manual refresh');
  env.requests[1].resolve({ data_version: 2, summary: {} });
  await manual;
  assert.equal(api.state.data.dataVersion, 2);
  const queued = env.timers.filter(timer => !timer.cleared);
  assert.equal(queued.length, 1, 'a version newer than the manual dashboard GET is replayed once');
  queued[0].callback();
  assert.equal(env.requests.length, 3);
  assert.match(env.requests[2].url, /^\/api\/dashboard/);
  assert.notEqual(env.requests[2].options.method, 'POST', 'catching up the newer event does not repeat the scan');
  assert.equal(api.state.dashboardEventVersion, '3');
  env.requests[2].resolve({ data_version: 3, summary: {} });
  await api.state.dashboardPromise;
  assert.equal(api.state.data.dataVersion, 3);
  assert.equal(env.timers.length, 1, 'the follow-up read does not schedule a refresh loop');
  assert.equal(env.requests.filter(request => request.options.method === 'POST').length, 1);
}

(async () => {
  await testAnalyticsRefresh();
  await testRequestSpeedAndDetails();
  await testSameVersionRecoveryAndEarlyFilters();
  await testManualAndSseRefresh();
  await testNewerSseDuringManualDashboardRead();
  process.stdout.write('Request speed and refresh regressions passed.\n');
})().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
