'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const fixture = fs.readFileSync(path.join(__dirname, 'frontend_request_refresh_harness.js'), 'utf8');
const setup = fixture.slice(fixture.indexOf('function environment() {'), fixture.indexOf('async function settle()'));
const env = new Function('vm', 'cacheMetrics', setup + '\nreturn environment();')(vm, require(path.join(root, 'web/cache-metrics.js')));
let app = fs.readFileSync(path.join(root, 'web/app.js'), 'utf8');
app = app.replace('getData: () => state.data,', 'getData: () => state.data, __renderTest: payload => renderAll(normalize(payload)),');
vm.runInContext(app, env.context);
env.node('#sourceKindFilter').dispatch('change', { target: { value: 'hermes' } });
assert.equal(env.window.CodexDashboard.getState().sourceKind, 'hermes', 'source filter must update dashboard state');
assert.ok(env.requests.some(r => /source_kind=hermes/.test(r.url)), 'dashboard request must carry source_kind');
env.window.CodexDashboard.setFilters({ sourceKind: 'codex' });
assert.equal(env.window.CodexDashboard.getState().sourceKind, 'codex');

let analytics = fs.readFileSync(path.join(root, 'web/analytics.js'), 'utf8');
analytics = analytics.replace("document.addEventListener('DOMContentLoaded', initialize, { once: true });", `
  window.__hermes = { filterParams, state, renderRequests, detailRows, renderContextWindow, requestSpeedInfo, renderTtft, renderTasks };
`);
vm.runInContext(analytics, env.context);
assert.equal(env.window.__hermes.filterParams().get('source_kind'), 'codex', 'detail queries must share source filter');
if (process.argv.includes('--semantics')) {
  const api = env.window.__hermes;
  const row = { source_kind: 'hermes', api_call_count: 7, request_id: 'x', timestamp: '2026-10-22T12:00:00Z',
    input_tokens: 1000, output_tokens: 200, total_tokens: 1200, provider_label: 'Hermes 自定义供应商',
    workspace: 'weixin', workspace_source: 'sessions.source', pricing_status: 'unpriced',
    cost_status: 'unknown', cost_source: 'none', hermes_estimated_cost_usd: 0,
    rate_snapshot: { cost_origin: 'unpriced', note: '无法判断是否触发长上下文阶梯价' } };
  api.state.requests = { data: [row], total: 1, request_count: 7, page: 1, page_size: 30 };
  api.renderRequests();
  assert.match(env.node('#requestsTitle').textContent, /聚合/);
  assert.match(env.node('#requestsTable tbody').innerHTML, /聚合记录.*7/s);
  assert.match(env.node('#requestsPageSummary').textContent, /7.*调用/);
  assert.match(api.requestSpeedInfo(row).text, /不适用/);
  api.renderContextWindow(row);
  assert.match(env.node('#requestContextWindow').innerHTML, /不适用/);
  const details = api.detailRows(row).map(r => r.join(':')).join('\n');
  assert.match(details, /Hermes.*自报/);
  assert.match(details, /unknown/);
  assert.match(details, /无法判断是否触发长上下文阶梯价/);
  env.window.CodexDashboard.__renderTest({daily_model_usage:[{source_kind:'hermes', model:'test', provider:'custom',
    provider_label:'Hermes 自定义供应商', input_tokens:1000,output_tokens:200,total_tokens:1200,request_count:7}],
    request_metrics:{scope:'not_applicable',reason:'Hermes 未提供'}});
  assert.match(env.node('#modelTotals').innerHTML, /轮次平均输出速度 不适用/);
  assert.doesNotMatch(env.node('#modelTotals').innerHTML, /0 个完整轮次样本/);
  assert.equal(require(path.join(root, 'web/cache-metrics.js')).calculateRate({cachedTokens:2000,inputTokens:1000}), null,
    'incompatible source counts must not produce a cache percentage above 100%');
}
if (process.argv.includes('--pricing')) {
  env.window.CodexDashboard.__renderTest({daily_model_usage:[{source_kind:'hermes',
    model:'deepseek-v4.1-flash',provider:'custom',input_tokens:1000,output_tokens:100,
    cached_input_tokens:20000,total_tokens:1100,request_count:7,pricing_status:'priced',
    cost_before_discount_usd:0.0002775,cost_after_discount_usd:0.0002775,
    estimated_cost_usd:0.0002775,priced_tokens:1100,unpriced_tokens:0}],
    request_metrics:{scope:'not_applicable',reason:'Hermes 未提供'}});
  const cards = env.node('#kpiGrid').innerHTML;
  assert.match(cards, /预估费用 USD/);
  assert.doesNotMatch(cards, /未计价/);
  assert.match(cards, /100(?:\.0)?%/);
  assert.match(env.node('#apiHint').textContent, /DeepSeek.*官方参考价/);
  assert.match(env.node('#apiHint').textContent, /中转站.*折扣.*倍率/);
  assert.match(env.node('#apiHint').textContent, /价格规则/);
  env.window.CodexDashboard.__renderTest({daily_model_usage:[]});
  assert.doesNotMatch(env.node('#apiHint').textContent, /DeepSeek/);
}
console.log('Hermes frontend ' + (process.argv.includes('--pricing') ? 'pricing' : process.argv.includes('--semantics') ? 'semantics' : 'source filter') + ': OK');
