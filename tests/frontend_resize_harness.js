'use strict';

const assert = require('node:assert/strict');
const { appEnvironment, analyticsEnvironment, fireTimeout, settle } = require('./frontend_request_refresh_harness.js');

async function testAppResizeIsLocalAndDebounced() {
  const env = appEnvironment(), api = env.api;
  api.bindEvents();
  api.state.data = {
    summary: {}, workspaceRows: [], dailyActivity: [], hourlyActivity: [],
    requestDistribution: { bins: [], p50: 0, p90: 0, p99: 0 }, dailyModelUsage: []
  };
  env.node('#view-trends').hidden = true;
  env.window.dispatchEvent({ type: 'resize' });
  env.window.dispatchEvent({ type: 'resize' });

  const pending = env.timers.filter(timer => timer.ms === 130 && !timer.cleared);
  assert.equal(pending.length, 1, 'rapid resize events coalesce into one chart-layout update');
  fireTimeout(env, pending[0]);
  await settle();
  assert.equal(env.window.__renders.length, 0, 'resizing never calls full renderAll');
  assert.equal(env.requests.length, 0, 'resizing never refetches dashboard data');
  assert.equal(env.dispatchedEvents.filter(event => event.type === 'codex-dashboard:resize-charts').length, 1,
    'the app notifies chart consumers through the dedicated resize event');

  env.node('#view-trends').hidden = false;
  env.window.dispatchEvent({ type: 'resize' });
  const trendsResize = env.timers.filter(timer => timer.ms === 130 && !timer.cleared);
  assert.equal(trendsResize.length, 1);
  fireTimeout(env, trendsResize[0]);
  await settle();
  assert.equal(env.window.__renders.length, 0, 'the trends page redraws its chart nodes without full renderAll');
  assert.equal(env.requests.length, 0, 'trends-page resize still performs no data fetch');
  assert.equal(env.dispatchedEvents.filter(event => event.type === 'codex-dashboard:resize-charts').length, 2);
}

function testAnalyticsResizeEventOnlyRedrawsVisibleOverviewTrend() {
  const env = analyticsEnvironment(), api = env.api;
  api.state.dashboard = { dataVersion: 1, dailyModelUsage: [] };
  api.bindEvents();
  const trend = env.node('#analyticsTrendChart');
  const requests = env.node('#requestsTable tbody');
  const sessions = env.node('#sessionsTable tbody');
  trend.innerHTML = 'trend marker';
  requests.innerHTML = 'request marker';
  sessions.innerHTML = 'session marker';

  env.window.dispatchEvent({ type: 'resize' });
  assert.equal(trend.innerHTML, 'trend marker', 'analytics does not subscribe directly to window resize');

  env.node('#view-overview').hidden = true;
  env.window.dispatchEvent({ type: 'codex-dashboard:resize-charts' });
  assert.equal(trend.innerHTML, 'trend marker', 'a hidden overview trend is not redrawn');

  env.node('#view-overview').hidden = false;
  env.window.dispatchEvent({ type: 'codex-dashboard:resize-charts' });
  assert.match(trend.innerHTML, /暂无趋势数据/, 'the visible overview trend responds to the dedicated resize notification');
  assert.equal(requests.innerHTML, 'request marker', 'resize does not rerender request details');
  assert.equal(sessions.innerHTML, 'session marker', 'resize does not rerender session details');
  assert.equal(env.requests.length, 0, 'chart resize does not issue data queries');
}

async function run() {
  await testAppResizeIsLocalAndDebounced();
  testAnalyticsResizeEventOnlyRedrawsVisibleOverviewTrend();
  process.stdout.write('Frontend resize regressions passed.\n');
}

run().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
