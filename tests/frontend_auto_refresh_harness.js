'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const { appEnvironment, settle, pendingTimeouts, fireTimeout } = require('./frontend_request_refresh_harness.js');

const sourcePath = process.argv[2] ? path.resolve(process.argv[2]) : undefined;
const makeEnvironment = () => appEnvironment(sourcePath);
const dashboard = (version, extra = {}) => ({
  data_version: version, local_date: '2026-10-16', summary: {}, generated_at: '2026-10-16T10:00:00Z', ...extra
});

function activePollInterval(env) {
  return env.intervals.find(interval => interval.ms === 30000 && !interval.cleared);
}

async function finishDashboard(env, requestIndex, payload) {
  const pending = env.api.state.dashboardPromise;
  assert.ok(pending, 'a dashboard GET should be in flight');
  env.requests[requestIndex].resolve(payload);
  await pending;
  await settle();
}

async function testSameSseVersionRetriesAfterFailedGet() {
  const env = makeEnvironment(), api = env.api;
  api.connectEvents();
  const source = env.eventSources[0];
  assert.ok(source, 'connectEvents creates the SSE connection');
  const notification = { data: JSON.stringify({ version: 42 }) };
  source.emit('dashboard', notification);
  const firstTimer = env.timers.filter(timer => timer.ms === 150 && !timer.cleared).at(-1);
  fireTimeout(env, firstTimer);
  assert.equal(env.requests.length, 1);
  assert.equal(env.requests[0].url.split('?')[0], '/api/dashboard');
  assert.notEqual(env.requests[0].options.method, 'POST');
  const failedGet = api.state.dashboardPromise;
  env.requests[0].reject(new Error('temporary dashboard failure'));
  await failedGet;
  await settle();
  assert.equal(api.state.dashboardFailed, true);

  source.emit('dashboard', notification);
  const retryTimer = env.timers.filter(timer => timer.ms === 150 && !timer.cleared).at(-1);
  assert.ok(retryTimer && retryTimer !== firstTimer,
    'a duplicate version is retried when its previous dashboard GET failed');
  fireTimeout(env, retryTimer);
  assert.equal(env.requests.length, 2, 'the same SSE version starts another automatic GET');
  assert.notEqual(env.requests[1].options.method, 'POST');
  await finishDashboard(env, 1, dashboard(42));
  assert.equal(api.state.appliedEventVersion, '42');
  assert.equal(api.state.dashboardFailed, false);
}

async function testInitialGetWaitsForSseAndQueuesOneTrailingRead() {
  const env = makeEnvironment(), api = env.api;
  api.connectEvents();
  const source = env.eventSources[0];
  const initial = api.fetchDashboard();
  assert.equal(env.requests.length, 1);
  const initialSignal = env.requests[0].options.signal;
  source.emit('dashboard', { data: JSON.stringify({ version: 51 }) });
  assert.equal(env.requests.length, 1, 'an SSE notice does not start a concurrent GET');
  assert.equal(initialSignal.aborted, false, 'the in-flight dashboard GET is not preempted');
  assert.equal(pendingTimeouts(env, 150).length, 0, 'the newer event is recorded for a trailing read');

  env.requests[0].resolve(dashboard(50));
  await initial;
  await settle();
  const trailingTimers = pendingTimeouts(env, 150);
  assert.equal(trailingTimers.length, 1, 'the completed GET schedules one trailing read for the newer event');
  fireTimeout(env, trailingTimers[0]);
  assert.equal(env.requests.length, 2);
  assert.equal(env.requests[0].options.signal.aborted, false);
  assert.notEqual(env.requests[1].options.method, 'POST');
  await finishDashboard(env, 1, dashboard(51));
  assert.equal(api.state.appliedEventVersion, '51');
  assert.equal(pendingTimeouts(env, 150).length, 0, 'the trailing read does not create a refresh loop');
}

async function testOpenSseStillHasThirtySecondGetWatchdog() {
  const env = makeEnvironment(), api = env.api;
  api.connectEvents();
  api.connectEvents();
  const activeIntervals = env.intervals.filter(interval => interval.ms === 30000 && !interval.cleared);
  assert.equal(activeIntervals.length, 1, 'connectEvents maintains one dashboard poll interval');
  const source = env.eventSources[0];
  source.open();
  assert.equal(source.readyState, env.window.EventSource.OPEN);
  assert.equal(env.requests.length, 1, 'SSE open triggers an immediate catch-up GET');
  await finishDashboard(env, 0, dashboard(10));
  const renders = env.window.__renders.length;

  const polling = activeIntervals[0].fire();
  assert.equal(env.requests.length, 2, 'the watchdog polls even while EventSource remains OPEN');
  assert.notEqual(env.requests[1].options.method, 'POST');
  assert.equal(env.requests[1].url, env.requests[0].url);
  await finishDashboard(env, 1, dashboard(10));
  await polling;
  assert.equal(env.window.__renders.length, renders, 'unchanged polling does not redraw the dashboard');
}

async function testReconnectFocusAndVisibleResumeCatchUp() {
  const env = makeEnvironment(), api = env.api;
  api.connectEvents();
  const source = env.eventSources[0];
  source.fail();
  source.open();
  assert.equal(env.requests.length, 1, 'reopening the event stream catches up with a GET');
  await finishDashboard(env, 0, dashboard(1));

  env.window.dispatchEvent({ type: 'focus' });
  assert.equal(env.requests.length, 2, 'window focus reads missed updates');
  await finishDashboard(env, 1, dashboard(1));

  env.document.visibilityState = 'hidden';
  env.document.dispatch('visibilitychange');
  assert.equal(env.requests.length, 2, 'hidden tabs do not start a catch-up request');
  env.document.visibilityState = 'visible';
  env.document.dispatch('visibilitychange');
  assert.equal(env.requests.length, 3, 'returning to a visible tab catches up with a GET');
  assert.notEqual(env.requests[2].options.method, 'POST');
  await finishDashboard(env, 2, dashboard(1));
}

async function testPollingNeverScansOrPreemptsActiveWork() {
  const env = makeEnvironment(), api = env.api;
  api.connectEvents();
  const source = env.eventSources[0];
  source.fail();
  const interval = activePollInterval(env);
  assert.ok(interval, 'a 30-second watchdog remains active during reconnects');

  const manual = api.refreshNow();
  const manualInFlight = api.state.manualRefreshPromise;
  assert.equal(env.requests.length, 1);
  assert.equal(env.requests[0].options.method, 'POST');
  interval.fire();
  assert.equal(env.requests.length, 1, 'polling waits while the manual scan owns the refresh');
  assert.equal(api.state.manualRefreshPromise, manualInFlight, 'polling does not replace the manual scan promise');

  env.requests[0].resolve({ health: { version: 2 } });
  await settle();
  assert.equal(env.requests.length, 2, 'the manual scan starts one dashboard GET after it completes');
  assert.equal(env.requests[1].url.split('?')[0], '/api/dashboard');
  assert.notEqual(env.requests[1].options.method, 'POST');
  const dashboardInFlight = api.state.dashboardPromise;
  const dashboardSignal = env.requests[1].options.signal;
  interval.fire();
  assert.equal(env.requests.length, 2, 'polling shares or skips an active dashboard read');
  assert.equal(api.state.dashboardPromise, dashboardInFlight, 'polling does not replace the active dashboard promise');
  assert.equal(dashboardSignal.aborted, false, 'polling does not abort the active dashboard GET');
  env.requests[1].resolve(dashboard(2));
  await manual;
  assert.equal(env.requests.filter(request => request.options.method === 'POST').length, 1,
    'automatic refresh never invokes the scan endpoint');
}

async function testDashboardTimeoutIsFailureAndWatchdogRetries() {
  const env = makeEnvironment(), api = env.api;
  api.connectEvents();
  const interval = activePollInterval(env);
  assert.ok(interval);

  const manual = api.refreshNow();
  assert.equal(env.requests[0].options.method, 'POST');
  env.requests[0].resolve({ health: { version: 5 } });
  await settle();
  assert.equal(env.requests.length, 2, 'manual scanning is followed by one dashboard GET');
  assert.equal(env.requests[1].url.split('?')[0], '/api/dashboard');
  const manualDashboard = api.state.dashboardPromise;
  const deadline = pendingTimeouts(env, 20000).find(timer => !timer.cleared);
  assert.ok(deadline, 'each dashboard GET has a 20-second deadline');
  fireTimeout(env, deadline);
  assert.equal(env.requests[1].options.signal.aborted, true, 'the deadline aborts the stalled GET');
  const abortError = new Error('dashboard GET timed out');
  abortError.name = 'AbortError';
  env.requests[1].reject(abortError);
  await manual;
  await manualDashboard;
  await settle();
  assert.equal(api.state.dashboardFailed, true, 'a timeout AbortError is recorded as a dashboard failure');
  assert.equal(api.state.manualRefreshPromise, null, 'the timed out refresh releases the manual gate');

  interval.fire();
  assert.equal(env.requests.length, 3, 'the next watchdog tick retries the dashboard GET');
  assert.notEqual(env.requests[2].options.method, 'POST');
  const retryDeadline = pendingTimeouts(env, 20000).find(timer => !timer.cleared);
  assert.ok(retryDeadline, 'the recovery GET also has a deadline');
  await finishDashboard(env, 2, dashboard(5));
  assert.equal(retryDeadline.cleared, true, 'successful GET completion clears its deadline');
  assert.equal(api.state.dashboardFailed, false);
  assert.equal(env.requests.filter(request => request.options.method === 'POST').length, 1,
    'watchdog recovery does not repeat the manual scan');
}

async function testStableVersionAndUrlRevalidateWithoutRedraw() {
  const env = makeEnvironment(), api = env.api;
  Object.assign(api.state, {
    range: 'custom', customStartDate: '2026-10-01', customEndDate: '2026-10-15',
    provider: 'provider-a', workspace: 'workspace-a', model: 'model-a'
  });
  const initial = api.fetchDashboard();
  const expectedUrl = '/api/dashboard?start_date=2026-10-01&end_date=2026-10-15&compare=1&provider=provider-a&workspace=workspace-a&model=model-a';
  assert.equal(env.requests[0].url, expectedUrl);
  env.requests[0].resolve(dashboard(10));
  await initial;
  await settle();
  assert.equal(env.window.__renders.length, 1);

  assert.equal(typeof api.pollDashboard, 'function', 'the foreground and watchdog paths share pollDashboard');
  const unchanged = api.pollDashboard();
  assert.equal(env.requests[1].url, expectedUrl, 'date and filters remain part of the poll URL');
  await finishDashboard(env, 1, dashboard(10, { generated_at: '2026-10-16T10:00:30Z' }));
  await unchanged;
  assert.equal(env.window.__renders.length, 1, 'same URL, local date, and data version are not redrawn');
  assert.equal(env.dispatchedEvents.filter(event => event.type === 'codex-dashboard:render').length, 1);
  assert.equal(env.dispatchedEvents.filter(event => event.type === 'codex-dashboard:revalidate').length, 1,
    'detail panels are notified to revalidate without a full redraw');

  const changed = api.pollDashboard();
  assert.equal(env.requests[2].url, expectedUrl);
  await finishDashboard(env, 2, dashboard(11));
  await changed;
  assert.equal(env.window.__renders.length, 2, 'a changed data version redraws the dashboard');
  const renderEvents = env.dispatchedEvents.filter(event => event.type === 'codex-dashboard:render');
  assert.equal(renderEvents.length, 2, 'the changed version notifies detail consumers');
  assert.equal(renderEvents[1].detail.data.dataVersion, 11);
}

async function run() {
  const tests = [
    ['same-version SSE retry', testSameSseVersionRetriesAfterFailedGet],
    ['initial GET and trailing SSE read', testInitialGetWaitsForSseAndQueuesOneTrailingRead],
    ['30-second open-SSE watchdog', testOpenSseStillHasThirtySecondGetWatchdog],
    ['reconnect, focus, and visibility catch-up', testReconnectFocusAndVisibleResumeCatchUp],
    ['polling concurrency and GET-only behavior', testPollingNeverScansOrPreemptsActiveWork],
    ['GET timeout and watchdog retry', testDashboardTimeoutIsFailureAndWatchdogRetries],
    ['version-aware redraw and detail notifications', testStableVersionAndUrlRevalidateWithoutRedraw]
  ];
  const failures = [];
  for (const [name, test] of tests) {
    try {
      await test();
      process.stdout.write(`PASS ${name}\n`);
    } catch (error) {
      failures.push({ name, error });
      process.stderr.write(`FAIL ${name}: ${error.message}\n`);
    }
  }
  if (failures.length) {
    throw new Error(`${failures.length} auto-refresh regression(s) failed: ${failures.map(item => item.name).join(', ')}`);
  }
  process.stdout.write('Automatic refresh regressions passed.\n');
}

run().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
