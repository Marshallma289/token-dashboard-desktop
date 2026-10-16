'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const cacheMetrics = require(path.join(root, 'web', 'cache-metrics.js'));

function environment() {
  const nodes = new Map(), listeners = new Map(), documentListeners = new Map();
  const requests = [], timers = [], intervals = [], blobs = [], eventSources = [], dispatchedEvents = [];
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
      dispatch(name, event = {}) { if (handlers.has(name)) return handlers.get(name)(event); },
      querySelector(part) { return node(`${selector} ${part}`); }, querySelectorAll() { return []; },
      showModal() { this.open = true; },
      close() { this.open = false; if (handlers.has('close')) handlers.get('close')(); },
      appendChild() {}, remove() {}, click() {}, getBoundingClientRect() { return {}; }
    };
    nodes.set(selector, value);
    return value;
  };
  class MockEventSource {
    static OPEN = 1;
    static CONNECTING = 0;
    static CLOSED = 2;
    constructor(url) {
      this.url = url;
      this.readyState = MockEventSource.CONNECTING;
      this.eventListeners = new Map();
      eventSources.push(this);
    }
    addEventListener(name, callback) {
      if (!this.eventListeners.has(name)) this.eventListeners.set(name, []);
      this.eventListeners.get(name).push(callback);
    }
    open() {
      this.readyState = MockEventSource.OPEN;
      if (this.onopen) this.onopen({ type: 'open' });
    }
    fail() {
      this.readyState = MockEventSource.CONNECTING;
      if (this.onerror) this.onerror({ type: 'error' });
    }
    emit(name, event = {}) {
      if (name === 'open') return this.open();
      if (name === 'error') return this.fail();
      if (name === 'message' && this.onmessage) this.onmessage(event);
      (this.eventListeners.get(name) || []).forEach(callback => callback(event));
    }
  }
  const window = {
    CodexCacheMetrics: cacheMetrics, location: { search: '' },
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(callback);
    },
    dispatchEvent(event) {
      dispatchedEvents.push(event);
      (listeners.get(event.type) || []).forEach(callback => callback(event));
    },
    matchMedia() { return { matches: false }; },
    setTimeout: (callback, ms) => { const timer = { callback, ms, cleared: false }; timers.push(timer); return timer; },
    clearTimeout(timer) { if (timer) timer.cleared = true; },
    setInterval(callback, ms) {
      const interval = { callback, ms, cleared: false, fire() { if (!this.cleared) return this.callback(); } };
      intervals.push(interval);
      return interval;
    },
    clearInterval(interval) { if (interval) interval.cleared = true; },
    requestAnimationFrame(callback) { callback(); },
    EventSource: MockEventSource
  };
  const document = {
    body: node('body'), activeElement: null, visibilityState: 'visible',
    querySelector: node, querySelectorAll() { return []; },
    getElementById: id => node(`#${id}`), createElement: tag => node(`created:${tag}`),
    addEventListener(name, callback) {
      if (!documentListeners.has(name)) documentListeners.set(name, []);
      documentListeners.get(name).push(callback);
    },
    dispatch(name, event = {}) {
      (documentListeners.get(name) || []).forEach(callback => callback({ type: name, target: document, ...event }));
    }
  };
  const context = vm.createContext({
    window, document, Intl, URLSearchParams, AbortController, EventSource: MockEventSource,
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
  return { window, document, context, node, requests, timers, intervals, blobs, eventSources, dispatchedEvents };
}

async function settle() { for (let i = 0; i < 15; i += 1) await Promise.resolve(); }
function pendingTimeouts(env, ms) { return env.timers.filter(timer => !timer.cleared && (ms === undefined || timer.ms === ms)); }
function fireTimeout(env, timer) {
  assert.ok(timer, 'expected a pending timeout');
  timer.cleared = true;
  return timer.callback();
}
const row = (id, extra = {}) => ({ request_id: id, output_tokens: 400, input_tokens: 1000, ...extra });
const page = (id, current = 1, extra = {}) => ({ data: [row(id, extra)], total: 120, page: current, page_size: 30 });

function analyticsEnvironment() {
  const env = environment();
  env.snapshot = { range: 'all', provider: 'all', workspace: 'all', model: 'all' };
  env.window.CodexDashboard = { getState: () => env.snapshot, getCostMode: () => 'after' };
  let source = fs.readFileSync(path.join(root, 'web', 'analytics.js'), 'utf8');
  source = source.replace("document.addEventListener('DOMContentLoaded', initialize, { once: true });", `
    window.__test = { state, loadQueryData, loadRequestPage, loadTasks, loadSessions, renderSessions, renderContextWindow, requestSpeedInfo,
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
  assert.equal(env.requests.length, 3, 'request, task, and session queries start independently');
  assert.match(env.requests[0].url, /^\/api\/requests\?/);
  assert.match(env.requests[1].url, /^\/api\/tasks\?/);
  assert.match(env.requests[2].url, /^\/api\/sessions\?/);
  env.requests[0].resolve(page('fresh'));
  await settle();
  assert.equal(api.state.requests.data[0].request_id, 'fresh', 'request rows render before task aggregation finishes');
  assert.equal(env.node('#requestsPanel').attributes['aria-busy'], 'false');
  assert.equal(api.state.tasksLoading, true);
  assert.equal(api.state.sessionsLoading, true, 'a slow session query does not delay the request page');
  assert.equal(complete, false);
  await api.loadQueryData();
  assert.equal(env.requests.length, 3, 'same data version does not issue duplicate queries');
  env.requests[1].resolve({ data: [{ task_id: 'initial-task', summary: {} }] });
  await settle();
  assert.equal(api.state.tasks[0].task_id, 'initial-task');
  assert.equal(api.state.sessionsLoading, true, 'task completion does not block on sessions');
  assert.equal(complete, false);
  env.requests[2].resolve({ data: [{ thread_key: 'initial-session' }], omitted_request_count: 0 });
  await initial;
  assert.equal(api.state.sessions[0].thread_key, 'initial-session');

  api.state.requestPage = 2;
  api.state.dashboard = { dataVersion: 2, summary: {} };
  const background = api.loadQueryData();
  assert.match(env.requests[3].url, /page=2/);
  assert.equal(api.state.requestPage, 2, 'new data retains the selected page');
  env.snapshot = { ...env.snapshot, provider: 'provider-b', workspace: 'workspace-b' };
  const filtered = api.loadQueryData();
  assert.equal(api.state.requestPage, 1, 'changing filters starts on the first page');
  assert.equal(env.requests[3].options.signal.aborted, true);
  assert.equal(env.requests[4].options.signal.aborted, true);
  assert.equal(env.requests[5].options.signal.aborted, true);
  assert.match(env.requests[6].url, /provider=provider-b/);
  assert.match(env.requests[8].url, /^\/api\/sessions\?/);
  assert.match(env.requests[8].url, /provider=provider-b/);
  env.requests[6].resolve(page('provider-b'));
  env.requests[7].resolve({ data: [{ task_id: 'task-b', summary: {} }] });
  env.requests[8].resolve({ data: [{ thread_key: 'session-b' }], omitted_request_count: 1 });
  await filtered;
  env.requests[3].resolve(page('stale-page', 2));
  env.requests[4].resolve({ data: [{ task_id: 'stale-task', summary: {} }] });
  env.requests[5].resolve({ data: [{ thread_key: 'stale-session' }], omitted_request_count: 0 });
  await background;
  assert.equal(api.state.requests.data[0].request_id, 'provider-b');
  assert.equal(api.state.tasks[0].task_id, 'task-b', 'old filter responses cannot overwrite the current filter');
  assert.equal(api.state.sessions[0].thread_key, 'session-b', 'late sessions from the old filter are discarded');
  assert.equal(api.state.sessionOmitted, 1, 'omitted session counts come from the current filter response');

  env.node('#requestSort').value = 'token_desc';
  const tokenSort = api.loadRequestPage();
  env.node('#requestSort').value = 'time_desc';
  const recentSort = api.loadRequestPage();
  assert.equal(env.requests[9].options.signal.aborted, true);
  env.requests[10].resolve(page('recent-sort'));
  await recentSort;
  env.requests[9].resolve(page('stale-sort'));
  await tokenSort;
  assert.equal(api.state.requests.data[0].request_id, 'recent-sort');

  api.state.requestPage = 3;
  const third = api.loadRequestPage();
  api.state.requestPage = 2;
  const second = api.loadRequestPage();
  env.requests[12].resolve(page('second-page', 2));
  await second;
  env.requests[11].resolve(page('stale-third', 3));
  await third;
  assert.equal(api.state.requests.page, 2, 'late pagination responses are rejected');

  const retry = api.loadQueryData(true);
  assert.equal(env.requests.length, 16, 'explicit refresh reloads requests, tasks, and sessions for the same filter and version');
  env.requests[13].reject(new Error('network unavailable'));
  env.requests[14].resolve({ data: [] });
  env.requests[15].resolve({ data: [{ thread_key: 'session-b' }], omitted_request_count: 0 });
  await retry;
  assert.equal(api.state.requests.data[0].request_id, 'second-page', 'background errors retain the last useful rows');
  assert.match(env.node('#requestsContext').textContent, /更新失败/);
  assert.equal(api.state.requestsLoading, false);

  api.state.requestPage = 5;
  const shrink = api.loadRequestPage({ force: true });
  env.requests[16].resolve({ data: [], total: 31, page: 5, page_size: 30 });
  await settle();
  assert.match(env.requests[17].url, /page=2/);
  env.requests[17].resolve({ data: [row('clamped')], total: 31, page: 2, page_size: 30 });
  await shrink;
  assert.equal(api.state.requestPage, 2, 'a shrinking dataset clamps an invalid page');
}

function sessionRow(threadKey, extra = {}) {
  return {
    thread_key: threadKey, workspace: '/workspace/session', models: ['model-a', 'model-b'],
    request_count: 2, total_tokens: 4096, cost_after_discount_usd: 0.0123,
    unpriced_request_count: 0, last_seen: '2026-10-15T10:30:00Z', ...extra
  };
}

async function testSessionFilteringDrilldownAndClear() {
  const env = analyticsEnvironment(), api = env.api;
  const staleKey = 'a'.repeat(64), currentKey = 'b'.repeat(64);
  api.state.snapshot = env.snapshot;
  const staleLoad = api.loadSessions();
  assert.equal(env.requests.length, 1);
  assert.match(env.requests[0].url, /^\/api\/sessions\?days=0$/);

  env.snapshot = { ...env.snapshot, provider: 'provider-b', workspace: 'workspace-b', model: 'model-b' };
  api.state.snapshot = env.snapshot;
  const currentLoad = api.loadSessions();
  assert.equal(env.requests[0].options.signal.aborted, true, 'changing filters aborts the old session query');
  assert.match(env.requests[1].url, /provider=provider-b/);
  assert.match(env.requests[1].url, /workspace=workspace-b/);
  assert.match(env.requests[1].url, /model=model-b/);
  env.requests[1].resolve({ data: [sessionRow(currentKey)], omitted_request_count: 3 });
  await currentLoad;
  assert.equal(api.state.sessions[0].thread_key, currentKey);
  assert.match(env.node('#sessionsTable tbody').innerHTML, new RegExp(`data-session-thread="${currentKey}"`));
  assert.match(env.node('#sessionsStatus').textContent, /3 条请求缺少会话标识/);
  assert.match(env.node('#sessionsPageSummary').textContent, /第 1 \/ 1 页/);

  env.requests[0].resolve({ data: [sessionRow(staleKey)], omitted_request_count: 0 });
  await staleLoad;
  assert.equal(api.state.sessions[0].thread_key, currentKey, 'a late response cannot replace sessions from the newer filter');

  api.state.queryVersion = 'session-test-version';
  api.bindEvents();
  env.node('#sessionsTable').dispatch('click', {
    target: { closest: selector => selector === '[data-session-thread]' ? { dataset: { sessionThread: currentKey } } : null }
  });
  assert.equal(api.state.sessionThread, currentKey);
  assert.equal(api.state.requestPage, 1);
  assert.match(env.requests[2].url, /^\/api\/requests\?/);
  assert.equal(new URLSearchParams(env.requests[2].url.split('?')[1]).get('thread_key'), currentKey,
    'session drilldown sends the de-identified thread_key to the request endpoint');
  assert.match(api.state.requestsContext, /当前定位会话/);
  const drilled = api.state.requestPromise;
  env.requests[2].resolve(page('session-request'));
  await drilled;
  assert.equal(api.state.requests.data[0].request_id, 'session-request');
  assert.equal(env.node('#clearSessionFilter').hidden, false);

  env.node('#clearSessionFilter').dispatch('click');
  assert.equal(api.state.sessionThread, '');
  assert.equal(api.state.requestsContext, '');
  assert.equal(api.state.requestPage, 1);
  assert.equal(new URLSearchParams(env.requests[3].url.split('?')[1]).has('thread_key'), false,
    'clearing the session location removes thread_key from the request query');
  const cleared = api.state.requestPromise;
  env.requests[3].resolve(page('all-filtered-requests'));
  await cleared;
  assert.equal(api.state.requests.data[0].request_id, 'all-filtered-requests');
  assert.equal(env.node('#clearSessionFilter').hidden, true);
}

function testContextWindowBoundaries() {
  const env = analyticsEnvironment(), api = env.api;
  const panel = env.node('#requestContextWindow');

  api.renderContextWindow({});
  assert.match(panel.innerHTML, /占用比例未知/);
  assert.match(panel.innerHTML, /窗口上限未提供/);
  assert.match(panel.innerHTML, /aria-hidden="true"/);
  assert.doesNotMatch(panel.innerHTML, /role="meter"/);

  api.renderContextWindow({ context_window_tokens: 128000, context_input_tokens: 0 });
  assert.match(panel.innerHTML, /<strong>0<\/strong>/, 'zero input is known data, not a missing value');
  assert.match(panel.innerHTML, /0\.0%/);
  assert.match(panel.innerHTML, /role="meter"/);
  assert.match(panel.innerHTML, /aria-valuenow="0\.0"/);
  assert.doesNotMatch(panel.innerHTML, /context-track is-near-limit/);

  api.renderContextWindow({ context_window_tokens: 128000, context_input_tokens: 32000 });
  assert.match(panel.innerHTML, /25\.0%/);
  assert.match(panel.innerHTML, /aria-valuenow="25\.0"/);
  assert.match(panel.innerHTML, /32,000 \/ 128,000 Token/);

  api.renderContextWindow({ context_window_tokens: 128000, context_input_tokens: 150000 });
  assert.match(panel.innerHTML, /117\.2%/, 'the text reports actual over-capacity usage');
  assert.match(panel.innerHTML, /context-track is-near-limit/);
  assert.match(panel.innerHTML, /aria-valuenow="100\.0"/, 'the accessible meter clamps at its maximum');
  assert.match(panel.innerHTML, /width:100%/);
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
  env.requests[2].resolve({ data: [], omitted_request_count: 0 });
  await initial;
  const recovered = api.loadQueryData();
  assert.equal(env.requests.length, 5, 'a failed request query and the independent session query retry on the next dashboard render');
  env.requests[3].resolve(page('recovered'));
  env.requests[4].resolve({ data: [], omitted_request_count: 0 });
  await recovered;
  assert.equal(api.state.requestsError, '');
  env.snapshot = { ...env.snapshot, model: 'early-filter' };
  env.window.dispatchEvent({ type: 'codex-dashboard:loading', detail: { state: env.snapshot } });
  assert.equal(env.requests.length, 8, 'new filter queries start without waiting for dashboard aggregation');
  assert.match(env.requests[5].url, /model=early-filter/);
  assert.match(env.requests[7].url, /model=early-filter/);
  assert.equal(api.state.requests.data.length, 0, 'old-filter rows are cleared while the new filter loads');
  env.requests[5].resolve(page('filtered'));
  env.requests[6].resolve({ data: [] });
  env.requests[7].resolve({ data: [{ thread_key: 'filtered-session' }], omitted_request_count: 0 });
  await settle();
  assert.equal(api.state.requests.data[0].request_id, 'filtered');
  assert.equal(api.state.sessions[0].thread_key, 'filtered-session');
}

function appEnvironment(sourcePath = path.join(root, 'web', 'app.js')) {
  const env = environment();
  let source = fs.readFileSync(sourcePath, 'utf8');
  const startup = source.indexOf("  window.addEventListener('pywebviewready', loadDesktopPreferences);");
  assert.ok(startup > 0);
  source = source.slice(0, startup) + `
    window.__renders = [];
    renderAll = (data, options = {}) => {
      window.__renders.push({ data, options });
      window.dispatchEvent(new CustomEvent('codex-dashboard:render', {
        detail: { data, state: dashboardSnapshot(), refreshQueries: Boolean(options.refreshQueries) }
      }));
    };
    window.__test = {
      state, fetchDashboard, refreshNow, scheduleRefresh, connectEvents,
      pollDashboard: typeof pollDashboard === 'function' ? pollDashboard : undefined,
      bindEvents
    };
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
  assert.equal(pendingTimeouts(env, 150).length, 0, 'the scan notification does not cause an extra dashboard fetch');
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
  assert.equal(pendingTimeouts(env, 150).length, 1, 'repeated SSE versions are coalesced');
  fireTimeout(env, pendingTimeouts(env, 150)[0]);
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
  assert.equal(pendingTimeouts(env, 150).length, 0, 'SSE signals wait for the active manual refresh');
  env.requests[1].resolve({ data_version: 2, summary: {} });
  await manual;
  assert.equal(api.state.data.dataVersion, 2);
  const queued = pendingTimeouts(env, 150);
  assert.equal(queued.length, 1, 'a version newer than the manual dashboard GET is replayed once');
  fireTimeout(env, queued[0]);
  assert.equal(env.requests.length, 3);
  assert.match(env.requests[2].url, /^\/api\/dashboard/);
  assert.notEqual(env.requests[2].options.method, 'POST', 'catching up the newer event does not repeat the scan');
  assert.equal(api.state.dashboardEventVersion, '3');
  env.requests[2].resolve({ data_version: 3, summary: {} });
  await api.state.dashboardPromise;
  assert.equal(api.state.data.dataVersion, 3);
  assert.equal(pendingTimeouts(env, 150).length, 0, 'the follow-up read does not schedule a refresh loop');
  assert.equal(env.requests.filter(request => request.options.method === 'POST').length, 1);
}

async function runAll() {
  await testAnalyticsRefresh();
  await testSessionFilteringDrilldownAndClear();
  testContextWindowBoundaries();
  await testRequestSpeedAndDetails();
  await testSameVersionRecoveryAndEarlyFilters();
  await testManualAndSseRefresh();
  await testNewerSseDuringManualDashboardRead();
  process.stdout.write('Request speed and refresh regressions passed.\n');
}

if (require.main === module) {
  runAll().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
}

module.exports = { environment, analyticsEnvironment, appEnvironment, settle, pendingTimeouts, fireTimeout, row, page };
