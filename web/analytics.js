(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const nf = new Intl.NumberFormat('zh-CN');
  const compactNf = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 });
  const cacheMetrics = window.CodexCacheMetrics;
  const state = {
    snapshot: null,
    dashboard: null,
    requests: { data: [], total: 0, page: 1, page_size: 30 },
    tasks: [],
    diagnostics: null,
    pricingRules: [],
    catalog: {},
    selectedCatalogModel: null,
    requestPage: 1,
    requestPageSize: 30,
    queryKey: '',
    preview: null,
    loadingPricing: false,
    requestsContext: ''
  };

  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const str = (value, fallback = '') => value == null || String(value).trim() === '' ? fallback : String(value).trim();
  const numeric = value => {
    if (typeof value === 'number') return Number.isFinite(value) ? value : 0;
    if (typeof value === 'string') { const parsed = Number(value.replace(/[ ,]/g, '')); return Number.isFinite(parsed) ? parsed : 0; }
    return 0;
  };
  const get = (object, ...keys) => {
    if (!object || typeof object !== 'object') return undefined;
    for (const key of keys) if (object[key] !== undefined && object[key] !== null && object[key] !== '') return object[key];
    return undefined;
  };
  const number = value => nf.format(Math.round(Math.max(0, numeric(value))));
  const knownNumber = value => value === undefined || value === null ? '—' : number(value);
  const compact = value => numeric(value) >= 10000 ? compactNf.format(Math.max(0, numeric(value))) : number(value);
  const currency = value => {
    const amount = Math.max(0, numeric(value));
    const digits = amount >= 1 ? 2 : amount >= .01 ? 4 : 6;
    return `$${nf.format(Number(amount.toFixed(digits)))}`;
  };
  const currencyExact = value => `$${Math.max(0, numeric(value)).toFixed(6)}`;
  const cachePercent = (value, available) => cacheMetrics.formatPercent(value, available);
  const formatBytes = value => {
    const bytes = numeric(value);
    if (bytes < 1024) return `${number(bytes)} 字节`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let amount = bytes / 1024, index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return `${amount.toFixed(1)} ${units[index]}`;
  };
  const pricingStatusLabel = value => ({ priced: '已计价', free: '免费', unpriced: '未计价', partial: '部分计价' }[String(value || '').toLowerCase()] || str(value, '—'));
  const scanStateLabel = value => ({ ok: '正常', error: '错误', partial: '部分完成', starting: '启动中', degraded: '有异常' }[String(value || '').toLowerCase()] || str(value, '—'));
  const dateLabel = value => {
    const match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return match ? `${match[1]}/${match[2]}/${match[3]}` : str(value, '—');
  };
  const timestamp = value => {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? str(value, '—') : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium' }).format(date);
  };
  const totalOf = row => numeric(get(row, 'total', 'total_tokens', 'totalTokens', 'tokens')) || numeric(get(row, 'input', 'input_tokens', 'inputTokens')) + numeric(get(row, 'output', 'output_tokens', 'outputTokens'));
  const requestsOf = row => numeric(get(row, 'requests', 'request_count', 'requestCount', 'calls', 'count'));
  const currentCostKey = () => window.CodexDashboard && window.CodexDashboard.getCostMode() === 'before' ? 'costBefore' : 'costAfter';
  const currentCostLabel = () => currentCostKey() === 'costBefore' ? '折扣前' : '折扣后';

  async function readJson(url) {
    const response = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  async function writeJson(url, body) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Dashboard-Request': '1' },
      body: JSON.stringify(body)
    });
    let result = {};
    try { result = await response.json(); } catch (_) { /* empty body */ }
    if (!response.ok) throw new Error(str(get(result, 'error', 'message'), `HTTP ${response.status}`));
    return result;
  }

  function filterParams(snapshot = state.snapshot || (window.CodexDashboard && window.CodexDashboard.getState()) || {}) {
    const params = new URLSearchParams();
    if (snapshot.range === 'custom' && snapshot.startDate && snapshot.endDate) {
      params.set('start_date', snapshot.startDate);
      params.set('end_date', snapshot.endDate);
    } else if (snapshot.range === 'all') params.set('days', '0');
    else if (snapshot.range) params.set('days', snapshot.range);
    if (snapshot.provider && snapshot.provider !== 'all') params.set('provider', snapshot.provider);
    if (snapshot.workspace && snapshot.workspace !== 'all') params.set('workspace', snapshot.workspace);
    if (snapshot.model && snapshot.model !== 'all') params.set('model', snapshot.model);
    return params;
  }

  function queryKey(snapshot) {
    return filterParams(snapshot).toString();
  }

  function fullQuery(path, extra = {}) {
    const params = filterParams();
    Object.entries(extra).forEach(([key, value]) => params.set(key, String(value)));
    return `${path}?${params.toString()}`;
  }

  function requestCostMode() {
    return currentCostKey() === 'costBefore' ? 'before' : 'after';
  }

  function showStatus(node, message, error = false) {
    if (!node) return;
    node.textContent = message || '';
    node.classList.toggle('is-error', Boolean(error));
  }

  function bindCostMode() {
    const current = window.CodexDashboard && window.CodexDashboard.getCostMode ? window.CodexDashboard.getCostMode() : 'after';
    $$('#costModeControls [data-cost-mode]').forEach(button => {
      button.classList.toggle('is-active', button.dataset.costMode === current);
      button.setAttribute('aria-pressed', String(button.dataset.costMode === current));
    });
  }

  function mondayOf(date) {
    const value = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    value.setDate(value.getDate() - ((value.getDay() + 6) % 7));
    return value;
  }

  function localDateString(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  }

  function applyPreset(which) {
    const today = new Date();
    const start = which === 'month' ? new Date(today.getFullYear(), today.getMonth(), 1) : mondayOf(today);
    if (window.CodexDashboard) window.CodexDashboard.setDateRange(localDateString(start), localDateString(today));
  }

  function updateDateInputs(snapshot) {
    if (!snapshot) return;
    const start = $('#customStartDate'), end = $('#customEndDate');
    if (document.activeElement !== start && snapshot.range === 'custom') start.value = snapshot.startDate || '';
    if (document.activeElement !== end && snapshot.range === 'custom') end.value = snapshot.endDate || '';
  }

  function selectedCost(summary) {
    const key = currentCostKey() === 'costBefore' ? 'cost_before_discount_usd' : 'cost_after_discount_usd';
    const legacy = get(summary, 'estimated_cost_usd', 'estimated_cost', 'cost_usd');
    return numeric(get(summary, key, currentCostKey() === 'costBefore' ? 'costBeforeDiscountUsd' : 'costAfterDiscountUsd') ?? legacy);
  }

  function costFor(object, selected = currentCostKey()) {
    if (selected === 'costBefore') return numeric(get(object, 'costBefore', 'cost_before_discount_usd', 'costBeforeDiscountUsd', 'previous_cost_before_discount_usd'));
    return numeric(get(object, 'costAfter', 'cost_after_discount_usd', 'costAfterDiscountUsd', 'previous_cost_after_discount_usd', 'estimated_cost_usd'));
  }

  function renderComparison(data) {
    const comparison = data && data.comparison || {};
    const prior = comparison.summary || {};
    const current = data && data.summary || {};
    const start = comparison.start_date || comparison.startDate;
    const end = comparison.end_date || comparison.endDate;
    $('#comparisonMeta').textContent = start && end ? `${dateLabel(start)} — ${dateLabel(end)}` : '上一等长期间';
    const cacheAvailable = summary => {
      const count = get(summary, 'cache_read_available_count', 'cacheReadAvailableCount');
      return count === undefined ? get(summary, 'cache', 'cache_read_tokens', 'cached_input_tokens') !== undefined : numeric(count) > 0;
    };
    const curr = [
      { label: 'Token', value: numeric(get(current, 'total', 'total_tokens', 'totalTokens', 'tokens')), old: numeric(get(prior, 'total', 'total_tokens', 'totalTokens', 'tokens')), format: compact },
      { label: '请求数', value: numeric(get(current, 'requests', 'request_count', 'requestCount')), old: numeric(get(prior, 'requests', 'request_count', 'requestCount')), format: number },
      { label: `费用（${currentCostLabel()}）`, value: selectedCost(current), old: selectedCost(prior), format: currency },
      { label: '缓存读取', value: numeric(get(current, 'cache', 'cache_read_tokens', 'cached_input_tokens')), old: numeric(get(prior, 'cache', 'cache_read_tokens', 'cached_input_tokens')), format: compact, available: cacheAvailable(current), oldAvailable: cacheAvailable(prior) }
    ];
    $('#comparisonMetrics').innerHTML = curr.map(item => {
      const delta = item.value - item.old;
      const known = item.available !== false && item.oldAvailable !== false;
      const percent = !known ? '—' : item.old > 0 ? `${delta > 0 ? '+' : ''}${(delta / item.old * 100).toFixed(1)}%` : item.value > 0 ? '基期为 0' : '0.0%';
      const sign = delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat';
      return `<article class="analytics-metric"><span>${esc(item.label)}</span><strong>${esc(item.available === false ? '未提供' : item.format(item.value))}</strong><small>上期 ${esc(item.oldAvailable === false ? '未提供' : item.format(item.old))} · <i class="delta-${sign}">${esc(percent)}</i></small></article>`;
    }).join('');
  }

  function renderCacheMetrics(data) {
    const summary = data && data.summary || {};
    const input = numeric(get(summary, 'input', 'input_tokens', 'inputTokens'));
    const freshInput = get(summary, 'fresh_input_tokens', 'freshInputTokens');
    const cacheRead = get(summary, 'cache', 'cache_read_tokens', 'cached_input_tokens', 'cache_tokens');
    const cacheWrite = get(summary, 'cache_write', 'cache_write_tokens', 'cache_write_input_tokens');
    const readCountRaw = get(summary, 'cache_read_available_count', 'cacheReadAvailableCount');
    const writeCountRaw = get(summary, 'cache_write_available_count', 'cacheWriteAvailableCount');
    const readCount = numeric(readCountRaw);
    const writeCount = numeric(writeCountRaw);
    const requestCount = numeric(get(summary, 'requests', 'request_count', 'requestCount'));
    const readAvailable = readCountRaw === undefined ? cacheRead !== undefined : readCount > 0;
    const writeAvailable = writeCountRaw === undefined ? cacheWrite !== undefined : writeCount > 0;
    const hasReadInput = Object.prototype.hasOwnProperty.call(summary, 'cache_read_input_tokens') || Object.prototype.hasOwnProperty.call(summary, 'cacheReadInputTokens');
    const readInputRaw = Object.prototype.hasOwnProperty.call(summary, 'cache_read_input_tokens') ? summary.cache_read_input_tokens : summary.cacheReadInputTokens;
    const hitRateProvided = Object.prototype.hasOwnProperty.call(summary, 'cache_hit_rate') || Object.prototype.hasOwnProperty.call(summary, 'cacheHitRate');
    const hitRateRaw = Object.prototype.hasOwnProperty.call(summary, 'cache_hit_rate') ? summary.cache_hit_rate : summary.cacheHitRate;
    const rate = cacheMetrics.calculateRate({
      cachedTokens: cacheRead,
      inputTokens: input,
      cacheReadInputTokens: readInputRaw,
      denominatorProvided: hasReadInput,
      hitRate: hitRateRaw,
      hitRateProvided,
      availableCount: readCount,
      requestCount,
      availabilityKnown: readCountRaw !== undefined,
      available: readAvailable
    });
    const savingsBefore = get(summary, 'cache_savings_before_discount_usd', 'cacheSavingsBeforeDiscountUsd');
    const savingsAfter = get(summary, 'cache_savings_after_discount_usd', 'cacheSavingsAfterDiscountUsd');
    const selectedSavings = currentCostKey() === 'costBefore' ? savingsBefore : savingsAfter;
    const items = [
      ['未缓存输入', freshInput === undefined ? '未提供' : compact(freshInput), freshInput === undefined ? '日志未提供拆分' : `${number(input)} 输入 Token`],
      ['缓存读取', !readAvailable ? '未提供' : compact(cacheRead), !readAvailable ? '日志未提供' : `${number(readCount || requestCount)} / ${number(requestCount || readCount)} 条请求提供字段${requestCount > readCount && readCount > 0 ? ' · 部分记录可用' : ''}`],
      ['缓存写入', !writeAvailable ? '未提供' : compact(cacheWrite), !writeAvailable ? '日志未提供' : `${number(writeCount || requestCount)} 条请求提供字段`],
      ['缓存占比', cachePercent(rate, rate !== null && readAvailable), rate === null ? '缺少可用缓存读取样本或有效输入分母' : '缓存读取 Token ÷ 提供缓存字段样本的输入 Token'],
      [`缓存节省（${currentCostLabel()}）`, selectedSavings === undefined ? '未提供' : currency(selectedSavings), selectedSavings === undefined ? '缺少可用缓存价格' : '按当前价格规则估算']
    ];
    $('#cacheMetrics').innerHTML = items.map(item => `<article class="analytics-metric"><span>${esc(item[0])}</span><strong>${esc(item[1])}</strong><small>${esc(item[2])}</small></article>`).join('');
    const partial = requestCount && readCount > 0 && readCount < requestCount ? `缓存占比按 ${number(readCount)} / ${number(requestCount)} 条请求中提供缓存字段的样本统计，未将其余记录按缓存为 0 计入。` : '缓存字段未提供时会显示“未提供”，与实际为 0 区分。';
    $('#cacheNote').textContent = partial;
  }

  const exclusionLabels = {
    incomplete_turn: '轮次未完成', missing_timing: '缺少计时信息', missing_output: '缺少输出 Token',
    ambiguous_model: '轮内模型归属不明确', mixed_models: '轮内切换模型', missing_model: '缺少模型信息',
    invalid_timing: '计时值无效', incomplete: '轮次未完成', no_timing: '缺少计时信息'
  };

  function renderTtft(data) {
    const summary = data && data.summary || {};
    const median = get(summary, 'ttft_median_ms', 'ttftMedianMs');
    const p90 = get(summary, 'ttft_p90_ms', 'ttftP90Ms');
    const samples = numeric(get(summary, 'ttft_sample_count', 'ttftSampleCount'));
    const requests = numeric(get(summary, 'requests', 'request_count', 'requestCount'));
    const rawCoverage = get(summary, 'ttft_coverage', 'ttftCoverage');
    const coverage = rawCoverage === undefined ? (requests > 0 ? samples / requests : null) : numeric(rawCoverage);
    $('#ttftCoverage').textContent = `${cachePercent(coverage, coverage !== null)} 覆盖 · ${number(samples)} 个样本`;
    const items = [
      ['中位首字延迟', median === undefined || samples <= 0 ? '—' : `${number(median)} ms`, 'P50'],
      ['P90 首字延迟', p90 === undefined || samples <= 0 ? '—' : `${number(p90)} ms`, '90% 样本不超过'],
      ['有效样本', number(samples), requests ? `共 ${number(requests)} 次请求` : '请求数未提供']
    ];
    $('#ttftMetrics').innerHTML = items.map(item => `<article class="analytics-metric"><span>${esc(item[0])}</span><strong>${esc(item[1])}</strong><small>${esc(item[2])}</small></article>`).join('');
    const exclusions = data && data.speed_exclusions || {};
    const entries = Object.entries(exclusions).filter(([, count]) => numeric(count) > 0);
    $('#speedExclusions').innerHTML = entries.length
      ? `<span class="exclusion-heading">轮次速度未纳入样本的原因</span>${entries.map(([key, count]) => `<span class="exclusion-chip">${esc(exclusionLabels[key] || key)} · ${number(count)}</span>`).join('')}`
      : '<span class="exclusion-heading">暂无排除记录</span>';
  }

  function dailyRows(data) {
    const grouped = new Map();
    (data && data.dailyModelUsage || []).forEach(row => {
      const date = String(row.date || '');
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return;
      if (!grouped.has(date)) grouped.set(date, { date, total: 0, requests: 0, costBefore: 0, costAfter: 0, cacheSavingsBefore: 0, cacheSavingsAfter: 0, cache: 0, input: 0, cacheInput: 0, cacheReadAvailableCount: 0, cacheInputKnown: false, cacheAvailable: false });
      const item = grouped.get(date);
      item.total += numeric(row.total);
      item.requests += numeric(row.requests);
      item.costBefore += numeric(row.costBefore);
      item.costAfter += numeric(row.costAfter);
      item.cacheSavingsBefore += numeric(row.cacheSavingsBefore);
      item.cacheSavingsAfter += numeric(row.cacheSavingsAfter);
      item.cache += numeric(row.cache);
      item.input += numeric(row.input);
      const rowRequests = numeric(row.requests);
      const rowReadCount = row.cacheReadAvailableCount == null
        ? (row.cacheReadAvailable ? (rowRequests || 1) : 0)
        : numeric(row.cacheReadAvailableCount);
      item.cacheReadAvailableCount += rowReadCount;
      if (row.cacheReadInputProvided) {
        if (row.cacheReadInput != null) {
          item.cacheInput += numeric(row.cacheReadInput);
          item.cacheInputKnown = true;
        }
      } else if (rowReadCount > 0 && (!rowRequests || rowReadCount >= rowRequests)) {
        item.cacheInput += numeric(row.input);
        item.cacheInputKnown = true;
      }
      item.cacheAvailable = item.cacheAvailable || rowReadCount > 0;
    });
    return Array.from(grouped.values()).sort((a, b) => a.date.localeCompare(b.date));
  }

  function renderTrend() {
    const host = $('#analyticsTrendChart');
    const rows = dailyRows(state.dashboard);
    if (!rows.length) { host.innerHTML = '<div class="chart-empty"><div><strong>暂无趋势数据</strong><p>当前日期范围内没有按日统计。</p></div></div>'; return; }
    const metric = $('#trendMetric').value;
    const before = currentCostKey() === 'costBefore';
    const metricName = { tokens: 'Token', requests: '请求数', cost: `费用（${currentCostLabel()}）`, cache: '缓存读取 Token', cache_savings: `缓存节省（${currentCostLabel()}）` }[metric] || 'Token';
    const monetaryMetric = metric === 'cost' || metric === 'cache_savings';
    const value = row => metric === 'requests' ? row.requests : metric === 'cost' ? (before ? row.costBefore : row.costAfter) : metric === 'cache_savings' ? (before ? row.cacheSavingsBefore : row.cacheSavingsAfter) : metric === 'cache' ? row.cache : row.total;
    const values = rows.map(value);
    const max = Math.max(...values, 1);
    const width = Math.max(host.clientWidth || 680, 360), height = 195, left = 50, right = 13, top = 13, bottom = 31;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const x = index => rows.length === 1 ? left + plotWidth / 2 : left + index * plotWidth / (rows.length - 1);
    const y = amount => top + plotHeight - amount / max * plotHeight;
    let svg = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="每日${esc(metricName)}趋势"><title>每日${esc(metricName)}趋势</title>`;
    [0, .5, 1].forEach(ratio => { const yy = y(max * ratio); svg += `<line class="chart-grid-line" x1="${left}" y1="${yy}" x2="${width - right}" y2="${yy}"/><text class="chart-axis-label" x="${left - 7}" y="${yy + 3}" text-anchor="end">${esc(monetaryMetric ? currency(max * ratio) : compact(max * ratio))}</text>`; });
    const points = rows.map((row, index) => `${x(index).toFixed(1)},${y(value(row)).toFixed(1)}`).join(' ');
    svg += `<polyline points="${points}" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>`;
    rows.forEach((row, index) => {
      const amount = value(row);
      const cacheRate = cacheMetrics.calculateRate({ cachedTokens: row.cache, cacheReadInputTokens: row.cacheInput, denominatorProvided: row.cacheInputKnown });
      const tip = `${dateLabel(row.date)} · ${metricName}: ${monetaryMetric ? currency(amount) : number(amount)}${cacheRate === null ? '' : ` · 缓存占比 ${cachePercent(cacheRate, true)}`}`;
      svg += `<circle cx="${x(index)}" cy="${y(amount)}" r="3.5" fill="var(--surface)" stroke="var(--accent)" stroke-width="2"><title>${esc(tip)}</title></circle>`;
      const step = Math.max(1, Math.ceil(rows.length / 8));
      if (index % step === 0 || index === rows.length - 1) svg += `<text class="chart-axis-label" x="${x(index)}" y="${height - 8}" text-anchor="middle">${esc(row.date.slice(5))}</text>`;
    });
    svg += '</svg>';
    host.innerHTML = svg;
    $('#trendMetricNote').textContent = metric === 'cost' ? `费用趋势按${currentCostLabel()}显示；未计价记录不计入费用。` : metric === 'cache' ? '缓存趋势按可用的缓存读取 Token 汇总。' : metric === 'cache_savings' ? `缓存节省按${currentCostLabel()}显示，并依赖缓存单价配置。` : `当前显示每日${metricName}。`;
  }

  function showMissingCache(row, field, availableField, aliases) {
    const explicit = get(row, availableField);
    const value = get(row, ...aliases);
    if (explicit !== true || value === undefined) return '未提供';
    return number(value);
  }

  async function loadQueryData(force = false) {
    if (!window.CodexDashboard) return;
    const snapshot = window.CodexDashboard.getState();
    state.snapshot = snapshot;
    const key = queryKey(snapshot);
    if (!force && key === state.queryKey) return;
    state.queryKey = key;
    state.requestPage = 1;
    const requestsPromise = readJson(fullQuery('/api/requests', { page: state.requestPage, page_size: state.requestPageSize, sort: $('#requestSort').value, cost_mode: requestCostMode() }));
    const tasksPromise = readJson(fullQuery('/api/tasks'));
    const [requestsResult, tasksResult] = await Promise.allSettled([requestsPromise, tasksPromise]);
    if (key !== state.queryKey) return;
    if (requestsResult.status === 'fulfilled') {
      state.requests = requestsResult.value || { data: [], total: 0, page: 1, page_size: state.requestPageSize };
      state.requests.data = Array.isArray(state.requests.data) ? state.requests.data : [];
      renderRequests();
    } else {
      state.requests = { data: [], total: 0, page: 1, page_size: state.requestPageSize };
      renderRequests(requestsResult.reason && requestsResult.reason.message);
    }
    if (tasksResult.status === 'fulfilled') {
      const payload = tasksResult.value || {};
      state.tasks = Array.isArray(payload.data) ? payload.data : [];
      renderTasks();
    } else {
      state.tasks = [];
      renderTasks(tasksResult.reason && tasksResult.reason.message);
    }
  }

  async function loadRequestPage() {
    const url = fullQuery('/api/requests', { page: state.requestPage, page_size: state.requestPageSize, sort: $('#requestSort').value, cost_mode: requestCostMode() });
    try {
      const result = await readJson(url);
      state.requests = result || { data: [], total: 0, page: state.requestPage, page_size: state.requestPageSize };
      state.requests.data = Array.isArray(state.requests.data) ? state.requests.data : [];
      renderRequests();
    } catch (error) { renderRequests(error.message); }
  }

  function renderRequests(error = '') {
    const body = $('#requestsTable tbody');
    const rows = state.requests.data || [];
    $('#requestsEmpty').hidden = rows.length > 0;
    if (error) $('#requestsEmpty').textContent = `请求明细暂不可用（${error}）。`;
    else $('#requestsEmpty').textContent = '当前筛选范围没有请求明细。';
    const mode = currentCostKey();
    body.innerHTML = rows.map(row => {
      const id = str(get(row, 'request_id', 'id', 'requestId'));
      const pricing = str(get(row, 'pricing_status', 'pricingStatus'), 'priced');
      const price = get(row, mode === 'costBefore' ? 'cost_before_discount_usd' : 'cost_after_discount_usd', mode === 'costBefore' ? 'costBeforeDiscountUsd' : 'costAfterDiscountUsd', 'estimated_cost_usd');
      const fee = pricing === 'unpriced' ? '未计价' : price === undefined ? '—' : currency(price);
      const cacheRead = showMissingCache(row, 'cache_read_tokens', 'cache_read_available', ['cache_read_tokens', 'cached_input_tokens', 'cache']);
      const cacheWrite = showMissingCache(row, 'cache_write_tokens', 'cache_write_available', ['cache_write_tokens', 'cache_write_input_tokens', 'cache_write']);
      const detailAttrs = id ? `data-request-id="${esc(id)}"` : '';
      return `<tr class="request-row" tabindex="0" role="button" aria-label="打开请求统计详情" ${detailAttrs}><td>${esc(timestamp(get(row, 'timestamp', 'time', 'created_at')))}</td><td>${esc(get(row, 'provider_label', 'provider', 'provider_id'))}</td><td>${esc(get(row, 'workspace', 'workspace_id'))}</td><td>${esc(get(row, 'model', 'model_name'))}</td><td class="num">${esc(number(get(row, 'input_tokens', 'input')))}</td><td class="num">${esc(get(row, 'fresh_input_tokens') === undefined ? '未提供' : number(row.fresh_input_tokens))}</td><td class="num">${esc(cacheRead)}</td><td class="num">${esc(cacheWrite)}</td><td class="num">${esc(number(get(row, 'output_tokens', 'output')))}</td><td class="num" title="${esc(currentCostLabel())}">${esc(fee)}</td><td><span class="provider-pill" title="${esc(get(row, 'pricing_note', 'pricingNote'))}">${esc(pricingStatusLabel(pricing))}</span></td></tr>`;
    }).join('');
    const total = numeric(state.requests.total), page = numeric(state.requests.page) || state.requestPage;
    const pageSize = numeric(state.requests.page_size) || state.requestPageSize;
    const totalPages = Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
    $('#requestsPageSummary').textContent = total ? `共 ${number(total)} 条 · 第 ${page} / ${totalPages} 页` : error ? '接口暂不可用' : '当前范围无请求';
    $('#requestsPrev').disabled = !total || page <= 1;
    $('#requestsNext').disabled = !total || page >= totalPages;
    $('#requestsContext').textContent = state.requestsContext || '点击每日汇总行，可定位对应日期和模型的请求。';
    $('#exportRequests').disabled = !rows.length;
  }

  function cacheStateText(row, field, availability, aliases) {
    const provided = get(row, availability);
    const value = get(row, ...aliases);
    if (provided !== true || value === undefined) return '未提供';
    return `${number(value)} Token`;
  }

  function tokenSummary(summary) {
    return numeric(get(summary, 'total', 'total_tokens', 'tokens'));
  }

  function renderTasks(error = '') {
    const body = $('#tasksTable tbody');
    const tasks = state.tasks || [];
    $('#tasksEmpty').hidden = tasks.length > 0;
    $('#tasksEmpty').textContent = error ? `任务统计暂不可用（${error}）。` : '当前筛选范围没有任务归属数据。';
    const summary = state.dashboard && state.dashboard.summary || {};
    const totalCost = selectedCost(summary);
    const mode = currentCostKey();
    body.innerHTML = tasks.map((task, index) => {
      const metrics = task.summary || {};
      const cost = costFor(metrics, mode);
      const tokens = tokenSummary(metrics);
      const requests = requestsOf(metrics);
      const share = totalCost > 0 ? `${(cost / totalCost * 100).toFixed(1)}%` : '—';
      const agents = Array.isArray(task.agents) ? task.agents : [];
      const agentRows = agents.map(agent => {
        const agentSummary = agent.summary || {};
        const agentPrice = costFor(agentSummary, mode);
        const agentTokens = tokenSummary(agentSummary);
        const agentShare = cost > 0 ? `${(agentPrice / cost * 100).toFixed(1)}%` : '—';
        const role = get(agent, 'agent_role', 'role');
        const roleName = ({main:'主代理',subagent:'子代理',unknown:'未提供'})[role] || role;
        return `<tr><td>${esc(roleName)}</td><td>${esc(get(agent, 'model'))}</td><td title="${esc(get(agent, 'thread_key'))}">${esc(str(get(agent, 'thread_key'), '—').slice(0, 22))}</td><td class="num">${esc(compact(agentTokens))}</td><td class="num">${esc(currency(agentPrice))}</td><td class="num">${esc(agentShare)}</td></tr>`;
      }).join('');
      const detail = agents.length ? `<details class="agent-details"><summary>${number(agents.length)} 个代理</summary><div class="table-wrap nested-table"><table><thead><tr><th>角色</th><th>模型</th><th>线程</th><th class="num">Token</th><th class="num">费用 USD</th><th class="num">任务占比</th></tr></thead><tbody>${agentRows}</tbody></table></div></details>` : '—';
      const id = str(get(task, 'task_id', 'id'), `task-${index + 1}`);
      return `<tr><td title="${esc(id)}"><strong>${esc(id.length > 34 ? `${id.slice(0, 33)}…` : id)}</strong></td><td>${esc(get(task, 'workspace'))}</td><td class="num">${esc(number(requests))}</td><td class="num">${esc(compact(tokens))}</td><td class="num">${esc(currency(cost))}</td><td class="num">${esc(share)}</td><td>${detail}</td></tr>`;
    }).join('');
    $('#tasksMeta').textContent = tasks.length ? `${number(tasks.length)} 个任务 · 费用${currentCostLabel()}` : '当前筛选范围';
  }

  async function openRequestDetail(requestId) {
    if (!requestId) return;
    try {
      const query = new URLSearchParams({ id: requestId });
      const payload = await readJson(`/api/request?${query.toString()}`);
      const request = payload && payload.request || {};
      renderRequestDetail(request);
      $('#requestDetailDialog').showModal();
    } catch (error) {
      showStatus($('#requestsContext'), `无法读取请求详情：${error.message}`, true);
    }
  }

  function detailRows(request) {
    const before = get(request, 'cost_before_discount_usd', 'costBeforeDiscountUsd');
    const after = get(request, 'cost_after_discount_usd', 'costAfterDiscountUsd');
    const rateSnapshot = get(request, 'rate_snapshot', 'rateSnapshot') || {};
    const breakdown = get(request, 'cost_breakdown_before_discount', 'costBreakdownBeforeDiscount') || {};
    const rates = get(rateSnapshot, 'rates', 'rate_snapshot', 'token_rates') || rateSnapshot;
    const mode = currentCostKey();
    const currentCost = mode === 'costBefore' ? before : after;
    const detailInput = get(request, 'input_tokens', 'input');
    const detailCache = get(request, 'cache_read_tokens', 'cached_input_tokens', 'cache');
    const detailRate = cacheMetrics.calculateRate({ cachedTokens: detailCache, inputTokens: detailInput, available: get(request, 'cache_read_available') === true });
    const detailCacheRate = cachePercent(detailRate, detailRate !== null && get(request, 'cache_read_available') === true);
    const rows = [
      ['时间', timestamp(get(request, 'timestamp', 'time'))], ['供应商', get(request, 'provider_label', 'provider')],
      ['工作空间', get(request, 'workspace')], ['模型', get(request, 'model')], ['任务', get(request, 'task_id')],
      ['代理角色', ({ main: '主代理', subagent: '子代理', unknown: '未提供' })[get(request, 'agent_role')] || get(request, 'agent_role')], ['输入 Token', number(get(request, 'input_tokens', 'input'))],
      ['未缓存输入 Token', get(request, 'fresh_input_tokens') === undefined ? '未提供' : number(request.fresh_input_tokens)],
      ['缓存读取 Token', cacheStateText(request, 'cache_read_tokens', 'cache_read_available', ['cache_read_tokens', 'cached_input_tokens', 'cache'])],
      ['缓存占比', detailCacheRate],
      ['缓存写入 Token', cacheStateText(request, 'cache_write_tokens', 'cache_write_available', ['cache_write_tokens', 'cache_write_input_tokens', 'cache_write'])],
      ['输出 Token', number(get(request, 'output_tokens', 'output'))], ['Reasoning Token', number(get(request, 'reasoning_output_tokens', 'reasoning_tokens', 'reasoning'))],
      ['总 Token', number(get(request, 'total_tokens', 'total'))], [`费用（当前显示：${currentCostLabel()}）`, currentCost === undefined ? '—' : currencyExact(currentCost)],
      ['费用（折扣前）', before === undefined ? '—' : currencyExact(before)],
      ['费用（折扣后）', after === undefined ? '—' : currencyExact(after)], ['计价状态', pricingStatusLabel(get(request, 'pricing_status', 'pricingStatus'))],
      ['计价模型', get(request, 'pricing_model', 'pricingModel', 'model')],
      ['价格来源', get(request, 'pricing_source', 'price_source', 'source') ?? get(rateSnapshot, 'source', 'pricing_source')],
      ['价格规则 ID', get(request, 'pricing_rule_id', 'rule_id') ?? get(rateSnapshot, 'rule_id', 'id')],
      ['价格生效时间', get(request, 'pricing_effective_at', 'effective_at') ?? get(rateSnapshot, 'effective_at', 'effectiveAt')],
      ['计价档位', ({custom:'自定义', standard:'标准', short_context:'短上下文 / 标准', long_context:'长上下文', free:'免费', peak:'高峰', off_peak:'低峰', real_time:'实时'})[get(request, 'pricing_rate_band', 'rate_band', 'tier') ?? get(rateSnapshot, 'rate_band', 'tier')] || get(rateSnapshot, 'rate_band', 'tier')],
      ['价格倍率', get(request, 'cost_multiplier', 'multiplier', 'costMultiplier') ?? get(rateSnapshot, 'multiplier')],
      ['首字延迟', get(request, 'ttft_ms') === undefined ? '—' : `${number(request.ttft_ms)} ms`],
      ['轮次耗时', get(request, 'speed_duration_ms') === undefined ? '—' : `${number(request.speed_duration_ms)} ms`]
    ];
    const rateLabels = { input: '输入单价', cached_input: '缓存读取单价', cache_read: '缓存读取单价', cache_write: '缓存写入单价', output: '输出单价' };
    Object.entries(rateLabels).forEach(([key, label]) => {
      const value = get(rates, key, `${key}_usd_per_million`);
      if (value !== undefined && value !== null && Number.isFinite(Number(value))) rows.push([`${label}（USD / 百万 Token）`, currencyExact(value)]);
    });
    const breakdownLabels = { input: '未缓存输入', fresh_input: '未缓存输入', cached_input: '缓存读取', cache_read: '缓存读取', cache_write: '缓存写入', output: '输出' };
    Object.entries(breakdownLabels).forEach(([key, label]) => {
      const value = breakdown[key];
      if (value !== undefined && value !== null && Number.isFinite(Number(value))) rows.push([`折扣前费用拆分 · ${label}`, currencyExact(value)]);
    });
    return rows.filter(([, value]) => value !== undefined && value !== null && String(value) !== '');
  }

  function renderRequestDetail(request) {
    $('#requestDetailFacts').innerHTML = detailRows(request).map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  }

  function csvCell(value) {
    const source = String(value == null ? '' : value);
    const safe = typeof value === 'string' && /^[\s]*[=+@-]/.test(source) ? `'${source}` : source;
    return /[",\r\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
  }

  function downloadCsv(filename, fields, rows) {
    const csv = '\ufeff' + [fields.join(','), ...rows.map(row => fields.map(field => csvCell(row[field])).join(','))].join('\r\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  }

  function exportRequestPage() {
    const fields = ['request_id', 'timestamp', 'provider', 'workspace', 'model', 'input_tokens', 'fresh_input_tokens', 'cache_read_tokens', 'cache_read_input_tokens', 'cache_hit_rate', 'cache_write_input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens', 'cost_display_mode', 'displayed_cost_usd', 'estimated_cost_usd', 'cost_before_discount_usd', 'cost_after_discount_usd', 'pricing_status', 'task_id', 'agent_role'];
    const rows = (state.requests.data || []).map(row => ({
      request_id: get(row, 'request_id', 'id'), timestamp: get(row, 'timestamp', 'time'), provider: get(row, 'provider'), workspace: get(row, 'workspace'), model: get(row, 'model'),
      input_tokens: get(row, 'input_tokens', 'input'), fresh_input_tokens: get(row, 'fresh_input_tokens'), cache_read_tokens: get(row, 'cache_read_available') === true ? get(row, 'cache_read_tokens', 'cached_input_tokens', 'cache') : '未提供',
      cache_read_input_tokens: get(row, 'cache_read_available') === true ? get(row, 'input_tokens', 'input') : '',
      cache_hit_rate: get(row, 'cache_read_available') === true && numeric(get(row, 'input_tokens', 'input')) > 0 ? (numeric(get(row, 'cache_read_tokens', 'cached_input_tokens', 'cache')) / numeric(get(row, 'input_tokens', 'input'))).toFixed(6) : '',
      cache_write_input_tokens: get(row, 'cache_write_available') === true ? get(row, 'cache_write_tokens', 'cache_write_input_tokens', 'cache_write') : '未提供',
      output_tokens: get(row, 'output_tokens', 'output'), reasoning_tokens: get(row, 'reasoning_output_tokens', 'reasoning_tokens', 'reasoning'), total_tokens: totalOf(row),
      cost_display_mode: currentCostLabel(), displayed_cost_usd: costFor(row), estimated_cost_usd: costFor(row), cost_before_discount_usd: get(row, 'cost_before_discount_usd'), cost_after_discount_usd: get(row, 'cost_after_discount_usd'),
      pricing_status: get(row, 'pricing_status'), task_id: get(row, 'task_id'), agent_role: get(row, 'agent_role')
    }));
    downloadCsv(`codex-token-requests-${new Date().toISOString().slice(0, 10)}.csv`, fields, rows);
  }

  async function fetchDiagnostics() {
    try {
      state.diagnostics = await readJson('/api/diagnostics');
      renderDiagnostics();
    } catch (error) {
      state.diagnostics = null;
      renderDiagnostics(error.message);
    }
  }

  function renderDiagnostics(error = '') {
    const body = state.diagnostics || {};
    const health = body.scan || {};
    const scan = health.scan && typeof health.scan === 'object' ? health.scan : health;
    const lastScan = health.last_scan && typeof health.last_scan === 'object' ? health.last_scan : body.last_scan || {};
    const formats = body.formats || {};
    const roots = Array.isArray(body.roots) ? body.roots : [];
    const options = body.scan_options || {};
    const scanState = get(scan, 'state', 'status') ?? get(health, 'status');
    const lastSuccess = get(scan, 'last_success_at', 'lastSuccessAt') ?? get(lastScan, 'last_success_at', 'finished_at', 'completed_at');
    const lastStarted = get(lastScan, 'started_at', 'startedAt', 'at', 'finished_at', 'completed_at');
    const duration = get(scan, 'last_duration_ms', 'duration_ms') ?? get(lastScan, 'duration_ms', 'duration_ms_total');
    const explicitExceptions = get(scan, 'exception_count', 'error_count') ?? get(lastScan, 'exception_count', 'error_count', 'failed_files');
    const exceptionCount = explicitExceptions !== undefined ? explicitExceptions : ['parse_errors', 'incomplete_files', 'unavailable_roots'].reduce((sum, key) => sum + numeric(get(lastScan, key)), 0);
    $('#diagnosticsFacts').innerHTML = [
      ['扫描状态', scanStateLabel(scanState || (error ? 'error' : ''))],
      ['最近扫描', lastStarted ? timestamp(lastStarted) : '—'],
      ['最近成功', lastSuccess ? timestamp(lastSuccess) : '—'],
      ['扫描耗时', duration === undefined ? '—' : `${number(duration)} ms`],
      ['异常文件 / 错误', knownNumber(exceptionCount)],
      ['现代格式日志', knownNumber(get(formats, 'modern'))], ['旧版格式日志', knownNumber(get(formats, 'legacy'))]
    ].map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
    $('#diagnosticsRoots').innerHTML = roots.length ? `<h3>扫描目录</h3><ul>${roots.map(root => `<li><span>${esc(get(root, 'path'))}</span><b class="root-state ${root.available ? 'is-available' : 'is-unavailable'}">${root.available ? '可用' : '不可用'}</b></li>`).join('')}</ul>` : '<p class="analytics-muted">暂无扫描目录信息。</p>';
    if (document.activeElement !== $('#autoScanEnabled')) $('#autoScanEnabled').checked = Boolean(options.auto_scan);
    if (document.activeElement !== $('#scanInterval')) $('#scanInterval').value = get(options, 'interval_seconds') ?? '';
    const storage = body.storage && typeof body.storage === 'object' ? body.storage : {};
    const storageFacts = [];
    if (storage.mode != null) storageFacts.push(`存储方式：${storage.mode === 'durable' ? '本地持久存储' : str(storage.mode)}`);
    if (storage.database_path != null) storageFacts.push(`数据库文件：${storage.database_path}`);
    if (storage.database_bytes != null) storageFacts.push(`数据库大小：${formatBytes(storage.database_bytes)}`);
    if (storage.snapshots != null) storageFacts.push(`历史快照：${number(storage.snapshots)} 条`);
    if (storageFacts.length) $('#diagnosticsRoots').insertAdjacentHTML('beforeend', `<p class="analytics-muted">${storageFacts.map(esc).join(' · ')}</p>`);
    const unpriced = Array.isArray(body.unpriced_models) ? body.unpriced_models : [];
    $('#unpricedModels').innerHTML = unpriced.length ? `<h3>未计价模型</h3><ul>${unpriced.map(item => `<li>${esc(typeof item === 'string' ? item : get(item, 'model', 'name'))}</li>`).join('')}</ul>` : '<p class="analytics-muted">当前没有未计价模型。</p>';
  }

  function providerEntries() {
    const filters = state.dashboard && state.dashboard.filters || {};
    const entries = [];
    const add = item => {
      const id = typeof item === 'string' ? item : str(get(item, 'id', 'value', 'provider', 'provider_id'));
      const label = typeof item === 'string' ? item : str(get(item, 'label', 'name', 'provider_label'), id);
      if (id && !entries.some(existing => existing.id === id)) entries.push({ id, label });
    };
    (Array.isArray(state.dashboard && state.dashboard.providers) ? state.dashboard.providers : []).forEach(add);
    (Array.isArray(filters.providers) ? filters.providers : []).forEach(add);
    const catalogProviders = Array.isArray(state.catalog.providers) ? state.catalog.providers : Array.isArray(state.catalog.provider_options) ? state.catalog.provider_options : [];
    catalogProviders.forEach(add);
    const filterSelect = $('#providerFilter');
    if (filterSelect) Array.from(filterSelect.options).filter(option => option.value && option.value !== 'all').forEach(option => add({ id: option.value, label: option.textContent }));
    state.pricingRules.forEach(rule => { if (rule.provider !== '*') add({ id: rule.provider, label: rule.provider }); });
    return entries.filter(item => item.id);
  }

  function catalogEntries() {
    const metadata = state.catalog.metadata && typeof state.catalog.metadata === 'object' ? state.catalog.metadata : {};
    const pricingMeta = state.catalog.pricing && typeof state.catalog.pricing === 'object' ? state.catalog.pricing.metadata || state.catalog.pricing : {};
    const sources = [state.catalog.models, metadata.models, pricingMeta.models, state.catalog.built_in_models, state.catalog.prices, state.catalog.rules];
    const entries = [];
    sources.forEach(source => {
      if (!Array.isArray(source)) return;
      source.forEach(item => {
        if (typeof item === 'string') { entries.push({ model: item, provider: '*', variants: { standard: {} }, multiplier: 1 }); return; }
        if (!item || typeof item !== 'object') return;
        const rates = item.rates && typeof item.rates === 'object' ? item.rates : item.prices && typeof item.prices === 'object' ? item.prices : {};
        const model = str(get(item, 'model', 'model_id', 'model_name', 'name'));
        if (!model) return;
        const variantNames = ['short_context', 'standard', 'long_context', 'off_peak', 'peak', 'real_time'];
        const variants = {};
        variantNames.forEach(name => { if (item[name] && typeof item[name] === 'object') variants[name] = item[name]; });
        if (!Object.keys(variants).length) variants.standard = rates.input !== undefined || rates.output !== undefined ? rates : item;
        entries.push({
          provider: str(get(item, 'provider', 'provider_id'), '*'), model,
          variants,
          multiplier: get(item, 'multiplier') ?? 1,
          effective_at: get(item, 'effective_at', 'effectiveAt'), source: get(item, 'source'), note: get(item, 'note')
        });
      });
    });
    const uniqueEntries = new Map();
    entries.forEach(entry => uniqueEntries.set(`${entry.provider}|||${entry.model}`, entry));
    return Array.from(uniqueEntries.values());
  }

  async function loadPricing() {
    state.loadingPricing = true;
    try {
      const payload = await readJson('/api/pricing');
      state.pricingRules = Array.isArray(payload.rules) ? payload.rules : [];
      state.catalog = payload.catalog && typeof payload.catalog === 'object' ? payload.catalog : payload.metadata && typeof payload.metadata === 'object' ? payload.metadata : {};
      renderPricing();
      showStatus($('#pricingStatus'), '价格规则已载入。');
    } catch (error) {
      state.pricingRules = [];
      renderPricing(error.message);
    } finally { state.loadingPricing = false; }
  }

  function renderPricing(error = '') {
    const body = $('#pricingTable tbody');
    const rules = state.pricingRules;
    $('#pricingEmpty').hidden = rules.length > 0;
    if (error) $('#pricingEmpty').textContent = `价格规则暂不可用（${error}）。`;
    else $('#pricingEmpty').textContent = '暂无自定义价格规则。';
    body.innerHTML = rules.map(rule => `<tr><td>${esc(rule.provider === '*' ? '所有供应商（*）' : rule.provider)}</td><td>${esc(rule.model)}</td><td class="num">${esc(rule.input)}</td><td class="num">${esc(rule.cached_input)}</td><td class="num">${esc(rule.cache_write)}</td><td class="num">${esc(rule.output)}</td><td class="num">${esc(rule.multiplier)}</td><td>${esc(timestamp(rule.effective_at))}</td><td title="${esc([rule.source, rule.note].filter(Boolean).join(' · '))}">${esc([rule.source, rule.note].filter(Boolean).join(' · '))}</td><td><button class="table-action" type="button" data-edit-rule="${esc(rule.id)}">编辑</button><button class="table-action is-danger" type="button" data-delete-rule="${esc(rule.id)}">删除</button></td></tr>`).join('');
    refreshPricingProviderOptions();
    const catalog = catalogEntries();
    $('#pricingCatalogSelect').innerHTML = '<option value="">选择内置模型后自动填充价格</option>' + catalog.map((item, index) => `<option value="${index}">${esc(item.model)}${item.provider !== '*' ? ` · ${esc(item.provider)}` : ''}</option>`).join('');
  }

  function refreshPricingProviderOptions() {
    const select = $('#pricingProvider');
    const selected = select.value || '*';
    const providers = providerEntries();
    if (selected !== '*' && !providers.some(item => item.id === selected)) providers.unshift({ id: selected, label: selected });
    select.innerHTML = '<option value="*">所有供应商（*）</option>' + providers.map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join('');
    select.value = [...select.options].some(option => option.value === selected) ? selected : '*';
  }

  function localInputTime(value) {
    const date = value ? new Date(value) : new Date();
    if (Number.isNaN(date.getTime())) return '';
    const pad = number => String(number).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function openPricingForm(rule = null) {
    $('#pricingRuleForm').reset();
    $('#pricingRuleTitle').textContent = rule ? '编辑价格规则' : '新增价格规则';
    $('#pricingRuleId').value = rule ? str(rule.id) : '';
    $('#pricingProvider').value = rule ? str(rule.provider, '*') : '*';
    $('#pricingModel').value = rule ? str(rule.model) : '';
    $('#priceInput').value = rule ? rule.input : '';
    $('#priceCachedInput').value = rule ? rule.cached_input : '';
    $('#priceCacheWrite').value = rule ? rule.cache_write : '';
    $('#priceOutput').value = rule ? rule.output : '';
    $('#priceMultiplier').value = rule ? rule.multiplier : '1';
    $('#priceEffectiveAt').value = rule ? localInputTime(rule.effective_at) : localInputTime(new Date().toISOString());
    $('#priceSource').value = rule ? str(rule.source) : '';
    $('#priceNote').value = rule ? str(rule.note) : '';
    $('#pricingCatalogSelect').value = '';
    $('#pricingCatalogTierWrap').hidden = true;
    $('#pricingCatalogTier').innerHTML = '';
    state.selectedCatalogModel = null;
    showStatus($('#pricingFormError'), '');
    $('#pricingRuleDialog').showModal();
  }

  function selectCatalogModel(indexValue) {
    const index = Number(indexValue);
    const selected = Number.isInteger(index) && index >= 0 ? catalogEntries()[index] : null;
    state.selectedCatalogModel = selected;
    if (!selected) { $('#pricingCatalogTierWrap').hidden = true; return; }
    const provider = $('#pricingProvider');
    if (![...provider.options].some(option => option.value === selected.provider)) {
      const option = document.createElement('option'); option.value = selected.provider; option.textContent = selected.provider; provider.append(option);
    }
    if (selected.provider !== '*') provider.value = selected.provider;
    $('#pricingModel').value = selected.model;
    const variants = selected.variants && Object.keys(selected.variants).length ? selected.variants : { standard: {} };
    const labels = { short_context: '短上下文 / 标准', standard: '标准', long_context: '长上下文', off_peak: '低峰', peak: '高峰', real_time: '实时' };
    const keys = Object.keys(variants);
    $('#pricingCatalogTier').innerHTML = keys.map(key => `<option value="${esc(key)}">${esc(labels[key] || key)}</option>`).join('');
    $('#pricingCatalogTierWrap').hidden = keys.length <= 1;
    fillCatalogRates();
  }

  function fillCatalogRates() {
    const selected = state.selectedCatalogModel;
    if (!selected) return;
    const variantKey = $('#pricingCatalogTier').value || Object.keys(selected.variants || {})[0] || 'standard';
    const source = selected.variants && selected.variants[variantKey] || {};
    const rates = source.rates && typeof source.rates === 'object' ? source.rates : source;
    const cacheWriteRate = get(rates, 'cache_write', 'cache_write_price', 'cache_write_usd_per_million');
    const estimatedCacheWrite = cacheWriteRate === undefined && ['off_peak', 'peak'].includes(variantKey)
      ? get(rates, 'input', 'input_price', 'input_usd_per_million') : undefined;
    const values = {
      '#priceInput': get(rates, 'input', 'input_price', 'input_usd_per_million'),
      '#priceCachedInput': get(rates, 'cached_input', 'cache_read', 'cached_input_price', 'cache_read_usd_per_million'),
      '#priceCacheWrite': cacheWriteRate === undefined ? estimatedCacheWrite : cacheWriteRate,
      '#priceOutput': get(rates, 'output', 'output_price', 'output_usd_per_million')
    };
    Object.entries(values).forEach(([selector, value]) => { $(selector).value = value === undefined ? '' : value; });
    $('#priceMultiplier').value = get(selected, 'multiplier') ?? 1;
    $('#priceEffectiveAt').value = localInputTime(get(source, 'effective_at', 'effectiveAt') || get(selected, 'effective_at', 'effectiveAt') || new Date().toISOString());
    $('#priceSource').value = str(get(source, 'source'), str(get(selected, 'source')));
    const note = str(get(source, 'note'), str(get(selected, 'note')));
    const estimateNote = estimatedCacheWrite === undefined ? '' : '；该档位未单列缓存写入价，暂按输入价估算';
    const tierName = ({short_context:'短上下文 / 标准',standard:'标准',long_context:'长上下文',off_peak:'低峰',peak:'高峰',real_time:'实时'})[variantKey] || variantKey;
    $('#priceNote').value = note || `基于内置${tierName}价格档位填充；保存后将按单一倍率应用${estimateNote}。`;
  }

  function formRule() {
    const rate = id => Number($(id).value);
    const model = $('#pricingModel').value.trim();
    const multiplier = rate('#priceMultiplier');
    const effectiveLocal = $('#priceEffectiveAt').value;
    const rules = {
      id: $('#pricingRuleId').value || (window.crypto && crypto.randomUUID ? crypto.randomUUID() : `rule-${Date.now()}-${Math.random().toString(16).slice(2)}`),
      provider: $('#pricingProvider').value || '*', model,
      input: rate('#priceInput'), cached_input: rate('#priceCachedInput'), cache_write: rate('#priceCacheWrite'), output: rate('#priceOutput'),
      multiplier, effective_at: effectiveLocal ? new Date(effectiveLocal).toISOString() : '',
      source: $('#priceSource').value.trim(), note: $('#priceNote').value.trim()
    };
    const rateValues = [rules.input, rules.cached_input, rules.cache_write, rules.output];
    if (!model) throw new Error('请填写模型名称。');
    if (rateValues.some(value => !Number.isFinite(value) || value < 0)) throw new Error('四项价格必须是大于或等于 0 的数字。');
    if (!Number.isFinite(multiplier) || multiplier < 0 || multiplier > 100) throw new Error('倍率范围为 0–100，例如 0.8 表示八折。');
    if (!effectiveLocal || Number.isNaN(Date.parse(effectiveLocal))) throw new Error('请填写有效的生效时间。');
    return rules;
  }

  async function savePricingRule(event) {
    event.preventDefault();
    let rule;
    try { rule = formRule(); } catch (error) { showStatus($('#pricingFormError'), error.message, true); return; }
    const existingId = $('#pricingRuleId').value;
    const rules = existingId ? state.pricingRules.map(item => item.id === existingId ? rule : item) : [...state.pricingRules, rule];
    const submit = $('#pricingRuleForm').querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      const result = await writeJson('/api/pricing', { rules });
      state.pricingRules = Array.isArray(result.rules) ? result.rules : rules;
      renderPricing();
      $('#pricingRuleDialog').close();
      showStatus($('#pricingStatus'), '价格规则已保存。历史记录沿用原规则；需要时可预览重算。');
      if (window.CodexDashboard) window.CodexDashboard.refresh();
      renderTrend(); renderTasks();
    } catch (error) { showStatus($('#pricingFormError'), `保存失败：${error.message}`, true); }
    finally { submit.disabled = false; }
  }

  async function deletePricingRule(ruleId) {
    const rule = state.pricingRules.find(item => String(item.id) === String(ruleId));
    if (!rule || !window.confirm(`删除“${rule.model}”的价格规则？已冻结的历史费用不会因此改变。`)) return;
    const rules = state.pricingRules.filter(item => String(item.id) !== String(ruleId));
    try {
      const result = await writeJson('/api/pricing', { rules });
      state.pricingRules = Array.isArray(result.rules) ? result.rules : rules;
      renderPricing();
      showStatus($('#pricingStatus'), '价格规则已删除。');
    } catch (error) { showStatus($('#pricingStatus'), `删除失败：${error.message}`, true); }
  }

  function repricePayload() {
    const snapshot = window.CodexDashboard ? window.CodexDashboard.getState() : state.snapshot || {};
    const params = filterParams(snapshot);
    return Object.fromEntries(params.entries());
  }

  async function previewReprice() {
    const button = $('#previewReprice');
    button.disabled = true;
    try {
      const result = await writeJson(`/api/reprice/preview?${new URLSearchParams(repricePayload()).toString()}`, repricePayload());
      state.preview = result;
      const count = numeric(get(result, 'request_count'));
      $('#repricePreviewSummary').textContent = `将重算当前筛选范围内 ${number(count)} 条请求。请检查新旧费用后明确确认。`;
      const facts = [
        ['之前费用 · 折扣前', currency(get(result, 'previous_cost_before_discount_usd'))],
        ['预览费用 · 折扣前', currency(get(result, 'cost_before_discount_usd'))],
        ['之前费用 · 折扣后', currency(get(result, 'previous_cost_after_discount_usd'))],
        ['预览费用 · 折扣后', currency(get(result, 'cost_after_discount_usd'))]
      ];
      $('#repricePreviewFacts').innerHTML = facts.map(item => `<div class="analytics-metric"><dt>${esc(item[0])}</dt><dd>${esc(item[1])}</dd></div>`).join('');
      $('#repricePreview').hidden = false;
      $('#confirmReprice').disabled = !result.preview_token || count === 0;
      showStatus($('#pricingStatus'), '预览已生成，历史费用尚未修改。');
    } catch (error) { showStatus($('#pricingStatus'), `无法生成重算预览：${error.message}`, true); }
    finally { button.disabled = false; }
  }

  async function confirmReprice() {
    if (!state.preview || !state.preview.preview_token) return;
    const button = $('#confirmReprice');
    button.disabled = true;
    try {
      const result = await writeJson('/api/reprice', { preview_token: state.preview.preview_token });
      $('#repricePreview').hidden = true;
      state.preview = null;
      showStatus($('#pricingStatus'), `已重算 ${number(result.updated_count)} 条请求。${result.backup_path ? `备份：${result.backup_path}` : ''}`);
      if (window.CodexDashboard) window.CodexDashboard.refresh();
      state.queryKey = '';
      await loadQueryData(true);
    } catch (error) { showStatus($('#pricingStatus'), `重算失败：${error.message}`, true); }
    finally { button.disabled = false; }
  }

  async function saveScanOptions() {
    const button = $('#saveScanOptions');
    const interval = Number($('#scanInterval').value);
    if (!Number.isInteger(interval) || interval < 10 || interval > 86400) { showStatus($('#pricingStatus'), '扫描间隔须为 10–86400 秒。', true); return; }
    button.disabled = true;
    try {
      await writeJson('/api/scan/options', { auto_scan: $('#autoScanEnabled').checked, interval_seconds: interval });
      await fetchDiagnostics();
      showStatus($('#pricingStatus'), '扫描设置已保存。');
    } catch (error) { showStatus($('#pricingStatus'), `保存扫描设置失败：${error.message}`, true); }
    finally { button.disabled = false; }
  }

  async function scanNow() {
    const button = $('#scanNow');
    button.disabled = true; button.textContent = '同步中…';
    try {
      await writeJson('/api/scan/now', {});
      await Promise.all([fetchDiagnostics(), loadQueryData(true)]);
      if (window.CodexDashboard) window.CodexDashboard.refresh();
      showStatus($('#pricingStatus'), '扫描已完成。');
    } catch (error) { showStatus($('#pricingStatus'), `立即同步失败：${error.message}`, true); }
    finally { button.disabled = false; button.textContent = '立即同步'; }
  }

  function bindEvents() {
    $('#applyCustomRange').addEventListener('click', () => {
      const start = $('#customStartDate').value, end = $('#customEndDate').value;
      if (!start || !end || start > end) { $('#customStartDate').setAttribute('aria-invalid', 'true'); $('#customEndDate').setAttribute('aria-invalid', 'true'); return; }
      $('#customStartDate').removeAttribute('aria-invalid'); $('#customEndDate').removeAttribute('aria-invalid');
      if (window.CodexDashboard) window.CodexDashboard.setDateRange(start, end);
    });
    $('#rangeThisWeek').addEventListener('click', () => applyPreset('week'));
    $('#rangeThisMonth').addEventListener('click', () => applyPreset('month'));
    $$('#costModeControls [data-cost-mode]').forEach(button => button.addEventListener('click', () => {
      if (window.CodexDashboard) window.CodexDashboard.setCostMode(button.dataset.costMode);
    }));
    $('#trendMetric').addEventListener('change', renderTrend);
    $('#requestSort').addEventListener('change', () => { state.requestPage = 1; loadRequestPage(); });
    $('#requestsPrev').addEventListener('click', () => { if (state.requestPage > 1) { state.requestPage -= 1; loadRequestPage(); } });
    $('#requestsNext').addEventListener('click', () => { state.requestPage += 1; loadRequestPage(); });
    $('#requestsTable tbody').addEventListener('click', event => {
      const row = event.target.closest('[data-request-id]');
      if (row) openRequestDetail(row.dataset.requestId);
    });
    $('#requestsTable tbody').addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const row = event.target.closest('[data-request-id]');
      if (row) { event.preventDefault(); openRequestDetail(row.dataset.requestId); }
    });
    $('#dailyUsageTable tbody').addEventListener('click', event => {
      const row = event.target.closest('tr.usage-summary-row');
      if (!row || !window.CodexDashboard) return;
      state.requestsContext = `当前定位：${dateLabel(row.dataset.date)} · ${row.dataset.model} · ${row.dataset.provider}。`;
      if (window.CodexDashboardLayout) window.CodexDashboardLayout.showView('usage', 'requests');
      window.CodexDashboard.setFilters({ provider: row.dataset.provider, model: row.dataset.model, workspace: row.dataset.workspace || 'all', startDate: row.dataset.date, endDate: row.dataset.date });
      window.requestAnimationFrame(() => $('#requestsPanel').scrollIntoView({ behavior: 'smooth', block: 'start' }));
    });
    $('#dailyUsageTable tbody').addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const row = event.target.closest('tr.usage-summary-row');
      if (row) { event.preventDefault(); row.click(); }
    });
    $('#exportRequests').addEventListener('click', exportRequestPage);
    $('#diagnosticsRefresh').addEventListener('click', fetchDiagnostics);
    $('#saveScanOptions').addEventListener('click', saveScanOptions);
    $('#scanNow').addEventListener('click', scanNow);
    $('#addPricingRule').addEventListener('click', () => openPricingForm());
    $('#pricingRuleForm').addEventListener('submit', savePricingRule);
    $('#pricingCatalogSelect').addEventListener('change', event => selectCatalogModel(event.target.value));
    $('#pricingCatalogTier').addEventListener('change', () => fillCatalogRates());
    $('#pricingTable tbody').addEventListener('click', event => {
      const editButton = event.target.closest('[data-edit-rule]');
      if (editButton) { const rule = state.pricingRules.find(item => String(item.id) === editButton.dataset.editRule); if (rule) openPricingForm(rule); return; }
      const deleteButton = event.target.closest('[data-delete-rule]');
      if (deleteButton) deletePricingRule(deleteButton.dataset.deleteRule);
    });
    $('#previewReprice').addEventListener('click', previewReprice);
    $('#cancelReprice').addEventListener('click', () => { state.preview = null; $('#repricePreview').hidden = true; });
    $('#confirmReprice').addEventListener('click', confirmReprice);
    $$('[data-close-dialog]').forEach(button => button.addEventListener('click', () => { const dialog = document.getElementById(button.dataset.closeDialog); if (dialog && dialog.open) dialog.close(); }));
    [$('#requestDetailDialog'), $('#pricingRuleDialog')].forEach(dialog => dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); }));
    window.addEventListener('codex-dashboard:render', event => {
      state.dashboard = event.detail && event.detail.data || null;
      state.snapshot = event.detail && event.detail.state || (window.CodexDashboard && window.CodexDashboard.getState());
      if (!$('#pricingRuleDialog').open) refreshPricingProviderOptions();
      bindCostMode(); updateDateInputs(state.snapshot); renderComparison(state.dashboard); renderCacheMetrics(state.dashboard); renderTtft(state.dashboard); renderTrend(); renderTasks();
      loadQueryData();
    });
    window.addEventListener('codex-dashboard:cost-mode', () => { bindCostMode(); renderComparison(state.dashboard); renderCacheMetrics(state.dashboard); renderTrend(); renderRequests(); renderTasks(); if ($('#requestSort').value === 'cost_desc') loadRequestPage(); });
    window.addEventListener('resize', () => { if (state.dashboard) renderTrend(); });
  }

  function initialize() {
    bindEvents();
    bindCostMode();
    loadPricing();
    fetchDiagnostics();
    if (window.CodexDashboard && window.CodexDashboard.getData()) {
      state.dashboard = window.CodexDashboard.getData();
      state.snapshot = window.CodexDashboard.getState();
      renderComparison(state.dashboard); renderCacheMetrics(state.dashboard); renderTtft(state.dashboard); renderTrend(); loadQueryData(true);
    }
  }

  document.addEventListener('DOMContentLoaded', initialize, { once: true });
})();
