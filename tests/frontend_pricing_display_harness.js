'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { environment, settle } = require('./frontend_request_refresh_harness.js');
const root = path.resolve(__dirname, '..');

const empty = { total_tokens: 0, request_count: 0, priced_tokens: 0, unpriced_tokens: 0,
  priced_request_count: 0, unpriced_request_count: 0, cost_before_discount_usd: 0, cost_after_discount_usd: 0 };
const unpriced = { ...empty, total_tokens: 420, request_count: 1, unpriced_tokens: 420, unpriced_request_count: 1 };
const priced = { ...empty, total_tokens: 420, request_count: 1, priced_tokens: 420, priced_request_count: 1,
  cost_before_discount_usd: 4, cost_after_discount_usd: 2 };
const partial = { ...priced, total_tokens: 600, request_count: 2, unpriced_tokens: 180, unpriced_request_count: 1 };
const current = { local_date: '2026-11-25', summary: priced, daily_model_usage: [
  { ...priced, date: '2026-11-25', model: 'gpt-5', pricing_status: 'priced' }
] };

function build(bridge, storage = new Map()) {
  const env = environment();
  env.context.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  if (bridge) env.window.pywebview = { api: bridge };
  const app = fs.readFileSync(path.join(root, 'web', 'app.js'), 'utf8');
  vm.runInContext(app, env.context);
  const analytics = fs.readFileSync(path.join(root, 'web', 'analytics.js'), 'utf8')
    .replace("document.addEventListener('DOMContentLoaded', initialize, { once: true });",
      'window.__pricing = { renderComparison, renderTrend, renderSessions, exportRequestPage, state };');
  vm.runInContext(analytics, env.context);
  return env;
}

async function loadData(env, payload) {
  const req = env.requests.find(r => r.url.startsWith('/api/dashboard'));
  assert.ok(req);
  req.resolve(payload);
  await settle();
}

function feeTile(env, summary, baseline) {
  env.window.__pricing.renderComparison({ summary, comparison: { summary: baseline } });
  return env.node('#comparisonMetrics').innerHTML.match(/<article[^>]*><span>费用[^]*?<\/article>/)[0];
}

async function pricingCoverage() {
  const env = build();
  let tile = feeTile(env, unpriced, empty);
  assert.match(tile, /<strong>未计价<\/strong>/);
  assert.doesNotMatch(tile, /0\.0%/);
  tile = feeTile(env, priced, unpriced);
  assert.match(tile, /上期 未计价/);
  assert.doesNotMatch(tile, /0\.0%|基期为 0/);
  tile = feeTile(env, partial, priced);
  assert.match(tile, /部分计价/);
  assert.doesNotMatch(tile, /0\.0%/);
  tile = feeTile(env, priced, { ...priced, cost_after_discount_usd: 1 });
  assert.match(tile, /\+100\.0%/);
  tile = feeTile(env, { ...priced, cost_after_discount_usd: 0 }, empty);
  assert.match(tile, /<strong>\$0<\/strong>/);
  assert.match(tile, /0\.0%/);
  const noTokenRequest = { ...priced, unpriced_request_count: 1, request_count: 2 };
  assert.match(feeTile(env, noTokenRequest, priced), /部分计价/);
  env.window.__pricing.state.sessions = [{ thread_key: 'unpriced-test', total_tokens: 420,
    request_count: 2, unpriced_request_count: 2, cost_after_discount_usd: 0 }];
  env.window.__pricing.renderSessions();
  assert.match(env.node('#sessionsTable tbody').innerHTML, /未计价/);
  assert.doesNotMatch(env.node('#sessionsTable tbody').innerHTML, /部分计价|\$0/);
  await loadData(env, { ...current, summary: partial, daily_model_usage: [
    ...current.daily_model_usage, { ...unpriced, date: '2026-11-25', model: 'unknown', pricing_status: 'unpriced' }
  ] });
  assert.match(env.node('#kpiGrid').innerHTML, /部分计价/);
  env.window.__pricing.state.dashboard = env.window.CodexDashboard.getData();
  env.node('#trendMetric').value = 'cost';
  env.window.__pricing.renderTrend();
  assert.match(env.node('#trendSummary').innerHTML, /部分计价/);
  env.window.__pricing.state.requests = { data: [{ ...unpriced, pricing_status: 'unpriced' }, { ...priced, pricing_status: 'priced' }] };
  env.window.__pricing.exportRequestPage();
  const csv = env.blobs.at(-1).parts[0].replace(/^\ufeff/, '').split('\r\n').map(line => line.split(','));
  const [header, unknown, known] = csv;
  assert.equal(unknown[header.indexOf('displayed_cost_usd')], '');
  assert.equal(known[header.indexOf('displayed_cost_usd')], '2');
  assert.equal(known[header.indexOf('cost_display_mode')], 'after');
}

async function nativePreference() {
  let prefs = { theme: 'light' }, exported = [];
  const bridge = {
    load_preferences: async () => ({ ...prefs }),
    save_preferences: async values => { prefs = { ...prefs, ...values }; return { ok: true }; },
    save_csv: async (_filename, csv) => { exported.push(csv); return { ok: true }; }
  };
  const first = build(bridge);
  await settle();
  first.window.CodexDashboard.setCostMode('before');
  await settle();
  assert.equal(prefs.cost_mode, 'before');
  const second = build(bridge); // Fresh localStorage, as on a different loopback port.
  await settle();
  assert.equal(second.window.CodexDashboard.getCostMode(), 'before');
  await loadData(second, current);
  assert.match(second.node('#kpiGrid').innerHTML, /折扣前/);
  await second.window.CodexDashboard.exportCsv();
  const [header, data] = exported[0].replace(/^\ufeff/, '').split('\r\n').map(line => line.split(','));
  assert.equal(data[header.indexOf('cost_display_mode')], 'before');
  assert.equal(data[header.indexOf('displayed_cost_usd')], '4.00000000');
  const browserStorage = new Map();
  build(null, browserStorage).window.CodexDashboard.setCostMode('before');
  assert.equal(build(null, browserStorage).window.CodexDashboard.getCostMode(), 'before');
}

const test = process.argv[2] === 'native' ? nativePreference : pricingCoverage;
test().then(() => console.log('pricing scenario verified')).catch(error => { console.error(error); process.exitCode = 1; });
