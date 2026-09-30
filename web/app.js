(() => {
  'use strict';

  const API_URL = '/api/dashboard';
  const EVENTS_URL = '/api/events';
  const HEALTH_URL = '/api/health';
  const HEALTH_REFRESH_MS = 15000;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const nf = new Intl.NumberFormat('zh-CN');
  const compactNf = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 });
  const svgEscape = value => String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  const htmlEscape = svgEscape;
  const palette = ['#6f9fe7', '#91b6f2', '#83c4b1', '#efb286', '#d98fa3', '#aa9ad9', '#71b8ca', '#dfc477', '#7e94b8', '#df9789', '#98c39f', '#c095b7'];
  document.body.classList.toggle('is-desktop', new URLSearchParams(window.location.search).get('desktop') === '1');

  const state = {
    range: '30',
    provider: 'all',
    workspace: 'all',
    model: 'all',
    data: null,
    loading: false,
    eventSource: null,
    requestId: 0,
    dashboardController: null,
    healthController: null,
    pollTimer: null,
    refreshTimer: null,
    healthTimer: null,
    tablePage: 1,
    tablePageSize: 50,
    preferencesLoading: false,
    preferencesLoaded: false,
    theme: readTheme()
  };

  function readTheme() {
    try {
      const saved = localStorage.getItem('codex-token-theme');
      if (saved === 'light' || saved === 'dark') return saved;
    } catch (_) { /* localStorage may be unavailable in a locked-down browser */ }
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function desktopBridge() {
    return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
  }

  function setTheme(theme, { persist = false } = {}) {
    state.theme = theme === 'dark' ? 'dark' : 'light';
    document.body.dataset.theme = state.theme;
    if (!persist) return;
    try { localStorage.setItem('codex-token-theme', state.theme); } catch (_) { /* no-op */ }
    const bridge = desktopBridge();
    if (bridge && typeof bridge.save_preferences === 'function') {
      Promise.resolve(bridge.save_preferences({ theme: state.theme })).catch(() => {});
    }
  }

  async function loadDesktopPreferences() {
    const bridge = desktopBridge();
    if (!bridge || typeof bridge.load_preferences !== 'function' || state.preferencesLoading || state.preferencesLoaded) return;
    state.preferencesLoading = true;
    try {
      const preferences = await bridge.load_preferences();
      if (preferences && (preferences.theme === 'light' || preferences.theme === 'dark')) {
        setTheme(preferences.theme);
        if (state.data) renderAll(state.data);
      }
      state.preferencesLoaded = true;
    } catch (_) {
      // Browser storage remains the fallback when the desktop bridge is unavailable.
    } finally {
      state.preferencesLoading = false;
    }
  }

  function toNumber(value) {
    if (typeof value === 'number') return Number.isFinite(value) ? value : 0;
    if (typeof value === 'string') {
      const cleaned = value.replace(/[,\s]/g, '').replace(/万$/i, '0000');
      const parsed = Number(cleaned);
      return Number.isFinite(parsed) ? parsed : 0;
    }
    return value == null ? 0 : Number(value) || 0;
  }

  function pick(object, ...keys) {
    if (!object || typeof object !== 'object') return undefined;
    for (const key of keys) {
      if (object[key] !== undefined && object[key] !== null && object[key] !== '') return object[key];
    }
    return undefined;
  }

  function text(value, fallback = '') {
    const result = value == null ? '' : String(value).trim();
    return result || fallback;
  }

  function list(value) {
    if (Array.isArray(value)) return value;
    if (!value || typeof value !== 'object') return [];
    return Object.entries(value).map(([key, item]) => {
      if (item && typeof item === 'object' && !Array.isArray(item)) return { ...item, __key: key };
      return { __key: key, value: item };
    });
  }

  function unique(values) {
    const seen = new Set();
    return values.map(value => text(value)).filter(value => value && !seen.has(value) && seen.add(value));
  }

  function filterName(item) {
    if (typeof item === 'string' || typeof item === 'number') return String(item);
    return text(pick(item, 'value', 'name', 'label', 'id', 'provider', 'workspace', '__key'));
  }

  function dateKey(value) {
    if (value == null || value === '') return '';
    const raw = String(value);
    const match = raw.match(/(\d{4})[-\/.](\d{1,2})[-\/.](\d{1,2})/);
    if (match) return `${match[1]}-${String(match[2]).padStart(2, '0')}-${String(match[3]).padStart(2, '0')}`;
    const numeric = Number(value);
    const date = Number.isFinite(numeric) ? new Date(numeric < 1e12 ? numeric * 1000 : numeric) : new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  }

  function dateLabel(key, withYear = false) {
    if (!key) return '—';
    const parts = key.split('-');
    if (parts.length < 3) return key;
    return withYear ? `${parts[0]}/${parts[1]}/${parts[2]}` : `${parts[1]}/${parts[2]}`;
  }

  function formatNumber(value) {
    const number = Math.max(0, toNumber(value));
    return nf.format(Math.round(number));
  }

  function formatCompact(value) {
    const number = Math.max(0, toNumber(value));
    if (number < 10000) return formatNumber(number);
    return compactNf.format(number);
  }

  function formatPercent(value) {
    if (!Number.isFinite(toNumber(value))) return '—';
    return `${(toNumber(value) * 100).toFixed(toNumber(value) < .1 ? 1 : 0)}%`;
  }

  function formatCurrency(value) {
    const number = Math.max(0, toNumber(value));
    const decimals = number >= 1 ? 2 : number >= .01 ? 4 : 6;
    return `$${nf.format(Number(number.toFixed(decimals)))}`;
  }

  function formatCurrencyDetailed(value) {
    const number = Math.max(0, toNumber(value));
    return `$${number.toFixed(6)}`;
  }

  function readableTimestamp(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return text(value, '—');
    return `${date.getFullYear()}/${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  }

  function metricFrom(row, names) {
    return toNumber(pick(row, ...names));
  }

  function normalizeWorkspaceRows(raw) {
    return list(raw).map((item, index) => {
      const name = text(pick(item, 'workspace', 'workspace_name', 'workspaceId', 'name', 'label', '__key'), `工作空间 ${index + 1}`);
      const requests = metricFrom(item, ['requests', 'request_count', 'requestCount', 'calls', 'count']);
      const active = metricFrom(item, ['active', 'active_count', 'active_workspaces', 'active_sessions', 'active_requests', 'sessions', 'active_days']);
      const tokens = metricFrom(item, ['total', 'total_tokens', 'totalTokens', 'tokens', 'token_count']);
      const providerId = text(pick(item, 'provider_id', 'providerId', 'provider', 'vendor_id'));
      const providerLabel = text(pick(item, 'provider_label', 'providerLabel', 'provider_name', 'providerName', 'vendor'), providerId || '');
      return {
        name,
        provider: providerLabel,
        providerId,
        requests,
        active: active || requests,
        tokens,
        sessions: metricFrom(item, ['sessions', 'session_count', 'sessionCount']),
        activeDays: metricFrom(item, ['active_days', 'activeDays', 'days'])
      };
    }).filter(row => row.name);
  }

  function normalizeDailyActivity(raw) {
    const grouped = new Map();
    list(raw).forEach((item, index) => {
      const date = dateKey(pick(item, 'date', 'day', 'date_key', 'timestamp', '__key')) || `day-${index + 1}`;
      if (!grouped.has(date)) grouped.set(date, { date, active: 0, requests: 0, tokens: 0, sessions: 0 });
      const target = grouped.get(date);
      target.active += metricFrom(item, ['active', 'active_workspaces', 'activeWorkspaces', 'active_users', 'activeUsers', 'active_sessions', 'activeSessions']);
      target.requests += metricFrom(item, ['requests', 'request_count', 'requestCount', 'calls', 'count']);
      target.tokens += metricFrom(item, ['tokens', 'total_tokens', 'totalTokens', 'total']);
      target.sessions += metricFrom(item, ['sessions', 'session_count', 'sessionCount']);
    });
    return Array.from(grouped.values()).filter(row => row.date).sort((a, b) => a.date.localeCompare(b.date));
  }

  function normalizeHourly(raw) {
    return list(raw).map((item, index) => {
      const hourRaw = pick(item, 'hour', 'hour_of_day', 'hourOfDay', 'h', '__key');
      const parsedHour = Number(String(hourRaw).replace(/时$/, ''));
      return {
        date: dateKey(pick(item, 'date', 'day', 'date_key')),
        hour: Number.isFinite(parsedHour) ? Math.max(0, Math.min(23, parsedHour)) : index % 24,
        requests: metricFrom(item, ['requests', 'request_count', 'requestCount', 'calls', 'count']),
        tokens: metricFrom(item, ['tokens', 'total_tokens', 'totalTokens', 'total']),
        active: metricFrom(item, ['active', 'active_workspaces', 'activeWorkspaces', 'active_users', 'activeUsers'])
      };
    });
  }

  function normalizeRequestDistribution(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    let rawBins = pick(source, 'bins', 'buckets', 'histogram', 'distribution');
    if (!rawBins && Array.isArray(raw)) rawBins = raw;
    if (!rawBins && Object.keys(source).some(key => /^\d/.test(key))) rawBins = Object.entries(source).filter(([key]) => /^\d/.test(key)).map(([key, value]) => ({ label: key, count: value }));
    const bins = list(rawBins).map((item, index) => {
      const min = metricFrom(item, ['min', 'min_tokens', 'from', 'start', 'lower', 'lo']);
      const max = metricFrom(item, ['max', 'max_tokens', 'to', 'end', 'upper', 'hi']);
      const count = metricFrom(item, ['count', 'requests', 'request_count', 'frequency', 'value', 'n']);
      const label = text(pick(item, 'label', 'name', 'bucket', '__key'), max ? `${formatCompact(min)}–${formatCompact(max)}` : `${formatCompact(min)}`);
      return { label, min, max: max || min, count };
    }).filter(bin => bin.count > 0 || bin.min > 0);
    return {
      bins,
      p50: metricFrom(source, ['p50', 'median', 'percentile50', 'percentile_50']),
      p90: metricFrom(source, ['p90', 'percentile90', 'percentile_90']),
      p99: metricFrom(source, ['p99', 'percentile99', 'percentile_99'])
    };
  }

  function detailRow(item, fallbackDate = '') {
    const input = metricFrom(item, ['input', 'input_tokens', 'inputTokens', 'prompt_tokens', 'promptTokens']);
    const output = metricFrom(item, ['output', 'output_tokens', 'outputTokens', 'completion_tokens', 'completionTokens']);
    const cache = metricFrom(item, ['cache', 'cached', 'cache_tokens', 'cached_input_tokens', 'cacheTokens', 'cached_tokens', 'cachedTokens', 'prompt_cache_hit_tokens']);
    const cacheWrite = metricFrom(item, ['cache_write', 'cache_write_input_tokens', 'cacheWriteInputTokens', 'cache_creation_input_tokens']);
    const reasoning = metricFrom(item, ['reasoning', 'reasoning_tokens', 'reasoningTokens']);
    const totalValue = metricFrom(item, ['total', 'total_tokens', 'totalTokens', 'tokens']);
    const providerId = text(pick(item, 'provider_id', 'providerId', 'provider', 'vendor_id'));
    const providerLabel = text(pick(item, 'provider_label', 'providerLabel', 'provider_name', 'providerName', 'vendor'), providerId || '未知供应商');
    return {
      date: dateKey(pick(item, 'date', 'day', 'date_key', 'timestamp')) || fallbackDate,
      model: text(pick(item, 'model', 'model_name', 'modelName', 'model_id', 'modelId', 'name'), '未知模型'),
      provider: providerLabel,
      providerId,
      input,
      output,
      cache,
      cacheWrite,
      reasoning,
      total: totalValue || input + output,
      requests: metricFrom(item, ['requests', 'request_count', 'requestCount', 'calls', 'count']),
      cost: metricFrom(item, ['estimated_cost_usd', 'estimated_cost', 'cost_usd']),
      pricingStatus: text(pick(item, 'pricing_status', 'pricingStatus')),
      pricingModel: text(pick(item, 'pricing_model', 'pricingModel')),
      pricingRateBand: text(pick(item, 'pricing_rate_band', 'pricingRateBand')),
      pricingNote: text(pick(item, 'pricing_note', 'pricingNote'))
    };
  }

  function flattenDailyUsage(raw) {
    const rows = [];
    if (Array.isArray(raw)) return raw.map(item => detailRow(item)).filter(row => row.date || row.total || row.requests);
    if (!raw || typeof raw !== 'object') return rows;
    Object.entries(raw).forEach(([day, value]) => {
      const normalizedDay = dateKey(day);
      if (Array.isArray(value)) {
        value.forEach(item => rows.push(detailRow(item, normalizedDay)));
        return;
      }
      if (value && typeof value === 'object') {
        const looksLikeMetric = ['input', 'output', 'total', 'tokens', 'requests', 'input_tokens', 'total_tokens'].some(key => value[key] !== undefined);
        if (looksLikeMetric) {
          rows.push(detailRow(value, normalizedDay));
          return;
        }
        Object.entries(value).forEach(([modelKey, modelValue]) => {
          if (modelValue && typeof modelValue === 'object') rows.push(detailRow({ ...modelValue, model: modelValue.model || modelKey }, normalizedDay));
          else rows.push(detailRow({ model: modelKey, total: modelValue }, normalizedDay));
        });
      }
    });
    return rows.filter(row => row.date || row.total || row.requests);
  }

  function summarizeRows(rows) {
    const grouped = new Map();
    rows.forEach(row => {
      const key = `${row.model}|||${row.provider}`;
      if (!grouped.has(key)) grouped.set(key, { model: row.model, provider: row.provider, providerId: row.providerId || row.provider, input: 0, output: 0, cache: 0, reasoning: 0, total: 0, requests: 0, cost: 0, pricingStatuses: new Set() });
      const result = grouped.get(key);
      ['input', 'output', 'cache', 'reasoning', 'total', 'requests', 'cost'].forEach(metric => { result[metric] += toNumber(row[metric]); });
      if (row.pricingStatus) result.pricingStatuses.add(row.pricingStatus);
    });
    return Array.from(grouped.values()).map(row => ({
      ...row,
      pricingStatus: row.pricingStatuses.size === 1 ? Array.from(row.pricingStatuses)[0] : row.pricingStatuses.size ? 'partial' : ''
    })).sort((a, b) => b.total - a.total);
  }

  function normalizeFilters(rawFilters, source) {
    const filters = rawFilters && typeof rawFilters === 'object' ? rawFilters : {};
    const rawProviders = list(pick(filters, 'providers', 'provider', 'vendors'));
    const providerOptions = [];
    const addProvider = (id, label) => {
      const providerId = text(id || label);
      const providerLabel = text(label || id, providerId);
      if (!providerId || providerOptions.some(option => option.id === providerId)) return;
      providerOptions.push({ id: providerId, label: providerLabel });
    };
    rawProviders.forEach(item => {
      if (typeof item === 'string' || typeof item === 'number') addProvider(item, item);
      else addProvider(pick(item, 'id', 'value', 'provider_id', 'providerId', 'provider', '__key'), pick(item, 'label', 'name', 'provider_label', 'providerLabel', 'provider_name'));
    });
    source.dailyModelUsage.forEach(row => addProvider(row.providerId || row.provider, row.provider));
    source.workspaceRows.forEach(row => { if (row.providerId || row.provider) addProvider(row.providerId || row.provider, row.provider); });
    const providers = providerOptions;
    const workspaces = unique([
      ...list(pick(filters, 'workspaces', 'workspace')).map(filterName),
      ...source.workspaceRows.map(row => row.name)
    ]);
    return { providers, workspaces, models: unique(list(filters.models).map(filterName)) };
  }

  function normalize(payload) {
    const body = payload && typeof payload === 'object' ? payload : {};
    const summary = body.summary && typeof body.summary === 'object' ? body.summary : {};
    const workspaceRows = normalizeWorkspaceRows(pick(body, 'workspace_distribution', 'workspaceDistribution', 'workspace_active_distribution') || []);
    const dailyActive = pick(body, 'daily_activity', 'dailyActivity', 'daily_active', 'daily_active_distribution') || [];
    const hourlyActive = pick(body, 'hourly_activity', 'hourlyActivity') || dailyActive;
    const dailyUsageRaw = pick(body, 'daily_model_usage', 'dailyModelUsage', 'daily_model_distribution', 'model_daily_distribution') || pick(summary, 'daily_model_usage', 'dailyModelUsage');
    const dailyModelUsage = flattenDailyUsage(dailyUsageRaw);
    const dailyActivity = normalizeDailyActivity(dailyActive);
    const hourlyActivity = normalizeHourly(hourlyActive);
    const requestDistribution = normalizeRequestDistribution(pick(body, 'request_distribution', 'requestDistribution', 'request_token_distribution', 'request_token_histogram') || {});
    const dailyModelSummaryRaw = pick(body, 'daily_model_summary', 'dailyModelSummary', 'daily_model_distribution', 'model_daily_distribution') || [];
    const normalizedSummary = flattenDailyUsage(dailyModelSummaryRaw);
    const dailyModelSummary = normalizedSummary.length ? normalizedSummary : summarizeRows(dailyModelUsage);
    const source = { workspaceRows, dailyModelUsage, dailyModelSummary };
    const filterPayload = { ...(pick(body, 'filters') || {}) };
    if (body.providers !== undefined) filterPayload.providers = body.providers;
    if (body.workspaces !== undefined) filterPayload.workspaces = body.workspaces;
    return {
      summary,
      filters: normalizeFilters(filterPayload, source),
      pricing: body.pricing && typeof body.pricing === 'object' ? body.pricing : {},
      workspaceRows,
      dailyActivity,
      hourlyActivity,
      requestDistribution,
      dailyModelUsage,
      dailyModelSummary,
      localDate: dateKey(pick(body, 'local_date', 'localDate')),
      generatedAt: pick(body, 'generated_at', 'generatedAt', 'updated_at', 'updatedAt') || pick(summary, 'generated_at', 'generatedAt') || new Date().toISOString()
    };
  }

  function rowsForView(rows) {
    return rows.filter(row => {
      const providerOkay = state.provider === 'all' || (!row.providerId && !row.provider) || row.providerId === state.provider || row.provider === state.provider;
      return providerOkay;
    });
  }

  function summaryMetric(summary, names, fallback = 0) {
    const result = metricFrom(summary, names);
    return result || fallback;
  }

  function viewMetrics(data) {
    const modelRows = rowsForView(data.dailyModelUsage);
    const workspaceRows = rowsForView(data.workspaceRows);
    const total = summaryMetric(data.summary, ['total', 'total_tokens', 'totalTokens', 'tokens'], modelRows.reduce((sum, row) => sum + row.total, 0));
    const input = summaryMetric(data.summary, ['input', 'input_tokens', 'inputTokens'], modelRows.reduce((sum, row) => sum + row.input, 0));
    const output = summaryMetric(data.summary, ['output', 'output_tokens', 'outputTokens'], modelRows.reduce((sum, row) => sum + row.output, 0));
    const requests = summaryMetric(data.summary, ['requests', 'request_count', 'requestCount', 'calls'], modelRows.reduce((sum, row) => sum + row.requests, 0));
    const active = summaryMetric(data.summary, ['active_workspaces', 'activeWorkspaces', 'workspaces'], workspaceRows.filter(row => row.active > 0).length);
    const models = summaryMetric(data.summary, ['models', 'model_count', 'modelCount'], new Set(modelRows.map(row => `${row.model}|||${row.provider}`)).size);
    const cache = summaryMetric(data.summary, ['cache', 'cached', 'cache_tokens', 'cached_input_tokens', 'cacheTokens', 'cached_tokens'], modelRows.reduce((sum, row) => sum + row.cache, 0));
    const cost = summaryMetric(data.summary, ['estimated_cost_usd', 'estimated_cost', 'cost_usd'], modelRows.reduce((sum, row) => sum + row.cost, 0));
    const pricedTokens = summaryMetric(data.summary, ['priced_tokens', 'pricedTokens'], modelRows.reduce((sum, row) => sum + (row.pricingStatus === 'unpriced' ? 0 : row.total), 0));
    const unpricedTokens = summaryMetric(data.summary, ['unpriced_tokens', 'unpricedTokens'], modelRows.reduce((sum, row) => sum + (row.pricingStatus === 'unpriced' ? row.total : 0), 0));
    const coverageTokens = pricedTokens + unpricedTokens;
    const coverage = summaryMetric(data.summary, ['pricing_coverage', 'pricingCoverage'], coverageTokens ? pricedTokens / coverageTokens : 0);
    return { total, input, output, requests, active, models, cache, cacheRate: input > 0 ? cache / input : 0, cost, pricedTokens, unpricedTokens, coverage };
  }

  function setConnection(status, message) {
    const badge = $('#connectionBadge');
    const label = $('#connectionText');
    if (!badge || !label) return;
    badge.classList.remove('is-connecting', 'is-offline', 'is-error', 'is-scanning');
    if (status === 'connecting' || status === 'reconnecting') badge.classList.add('is-connecting');
    if (status === 'offline') badge.classList.add('is-offline');
    label.textContent = message || ({ online: '实时连接', connecting: '连接中', reconnecting: '正在重连', offline: '连接不可用' }[status] || '连接中');
    badge.title = `实时数据更新状态：${label.textContent}`;
  }

  function setScanStatus(status, message, title) {
    const badge = $('#scanBadge');
    const label = $('#scanText');
    if (!badge || !label) return;
    badge.classList.remove('is-connecting', 'is-offline', 'is-error', 'is-scanning');
    if (status === 'starting') badge.classList.add('is-scanning');
    if (status === 'error' || status === 'offline') badge.classList.add(status === 'error' ? 'is-error' : 'is-offline');
    label.textContent = message || ({ ok: '数据已更新', starting: '扫描中', error: '扫描异常', offline: '状态不可用' }[status] || '扫描检查中');
    badge.title = title || `数据扫描状态：${label.textContent}`;
  }

  function healthStatus(payload) {
    const body = payload && typeof payload === 'object' ? payload : {};
    const scan = body.scan && typeof body.scan === 'object' ? body.scan : {};
    if (body.app_version) $('#appVersion').textContent = `v${body.app_version}`;
    const scanState = text(pick(scan, 'state'), text(pick(body, 'status'))).toLowerCase();
    const lastSuccess = pick(scan, 'last_success_at', 'lastSuccessAt');
    const lastError = scan.last_error && typeof scan.last_error === 'object' ? scan.last_error : null;
    const failureCount = toNumber(pick(scan, 'consecutive_failures', 'consecutiveFailures'));
    const timestamp = lastSuccess ? `最近成功扫描：${readableTimestamp(lastSuccess)}` : '';
    if (scanState === 'partial') return { status: 'starting', message: '部分数据未更新', title: '部分目录不可用或记录读取异常，已保留可用缓存；后台会自动重试。' };
    if (scanState === 'error' || scanState === 'degraded' || text(pick(body, 'status')).toLowerCase() === 'degraded') {
      const errorAt = lastError && lastError.at ? ` · 最近异常：${readableTimestamp(lastError.at)}` : '';
      const failures = failureCount ? ` · 连续失败 ${formatNumber(failureCount)} 次` : '';
      return { status: 'error', message: '扫描异常', title: `数据扫描状态异常${timestamp ? ` · ${timestamp}` : ''}${failures}${errorAt}` };
    }
    if (scanState === 'starting' || scanState === 'scanning' || scanState === 'running') {
      return { status: 'starting', message: '扫描中', title: timestamp ? `数据扫描进行中 · ${timestamp}` : '数据扫描进行中' };
    }
    return { status: 'ok', message: '数据已更新', title: timestamp || '数据扫描状态正常' };
  }

  async function fetchHealth() {
    if (state.healthController) state.healthController.abort();
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    state.healthController = controller;
    try {
      const response = await fetch(HEALTH_URL, {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
        signal: controller ? controller.signal : undefined
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (state.healthController !== controller) return;
      const health = healthStatus(payload);
      setScanStatus(health.status, health.message, health.title);
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      if (state.healthController !== controller) return;
      setScanStatus('offline', '状态不可用', '暂时无法读取数据扫描状态');
    } finally {
      if (state.healthController === controller) state.healthController = null;
    }
  }

  function startHealthPolling() {
    if (state.healthTimer) window.clearInterval(state.healthTimer);
    fetchHealth();
    state.healthTimer = window.setInterval(fetchHealth, HEALTH_REFRESH_MS);
  }

  function showError(message) {
    const banner = $('#errorBanner');
    if (!banner) return;
    banner.hidden = !message;
    if (message) $('#errorText').textContent = message;
  }

  function updateUpdated(value) {
    $('#lastUpdated').textContent = value ? `更新于 ${readableTimestamp(value)}` : '等待数据';
  }

  function updateRangeDescription(data) {
    const node = $('#rangeDescription');
    if (!node) return;
    const end = (data && data.localDate) || dateKey(new Date());
    if (state.range === 'all') {
      node.textContent = `全部历史数据，截止 ${dateLabel(end, true)}`;
      return;
    }
    const days = Math.max(1, toNumber(state.range));
    const startDate = new Date(`${end}T12:00:00`);
    startDate.setDate(startDate.getDate() - days + 1);
    const start = dateKey(startDate);
    node.textContent = `${dateLabel(start, true)} — ${dateLabel(end, true)} · 截止今日`;
  }

  function emptyChart(host, title = '暂无数据', description = '当前筛选范围内没有可展示的记录。') {
    if (!host) return;
    host.innerHTML = `<div class="chart-empty"><div><strong>${htmlEscape(title)}</strong><p>${htmlEscape(description)}</p></div></div>`;
  }

  function renderFilterOptions(data) {
    const provider = $('#providerFilter');
    const workspace = $('#workspaceFilter');
    const options = (select, values, allLabel, selected) => {
      const current = selected;
      const entries = values.map(value => typeof value === 'object' ? { value: text(value.id), label: text(value.label, value.id) } : { value: String(value), label: String(value) });
      if (current !== 'all' && current && !entries.some(entry => entry.value === current)) entries.unshift({ value: current, label: current });
      select.innerHTML = `<option value="all">${allLabel}</option>` + entries.map(entry => `<option value="${htmlEscape(entry.value)}">${htmlEscape(entry.label)}</option>`).join('');
      select.value = entries.some(entry => entry.value === current) ? current : 'all';
    };
    options(provider, data.filters.providers, '全部供应商', state.provider);
    options(workspace, data.filters.workspaces, '全部工作空间', state.workspace);
    options($('#modelFilter'), data.filters.models || [], '全部模型', state.model);
    state.provider = provider.value;
    state.workspace = workspace.value;
    state.model = $('#modelFilter').value;
  }

  function renderKpis(data) {
    const m = viewMetrics(data);
    const todayKey = data.localDate || dateKey(new Date());
    const todayRows = rowsForView(data.dailyModelUsage).filter(row => row.date === todayKey);
    const today = todayRows.reduce((sum, row) => sum + row.total, 0);
    const unpriced = (data.pricing && Array.isArray(data.pricing.unpriced_model_usage) ? data.pricing.unpriced_model_usage : [])
      .map(item => text(pick(item, 'model')))
      .filter(Boolean)
      .filter((model, index, models) => models.indexOf(model) === index);
    const costSub = m.unpricedTokens
      ? `${formatCompact(m.unpricedTokens)} token 未计价${unpriced.length ? ` · ${unpriced.slice(0, 2).join('、')}${unpriced.length > 2 ? '等' : ''}` : ''}`
      : '全部 Token 已按已配置官方价计价';
    const cards = [
      ['预估费用 USD', formatCurrency(m.cost), costSub, '$', m.cost],
      ['总 Token', formatCompact(m.total), 'Input + Output（Reasoning 为子集）', '◈', m.total],
      ['今日 Token', formatCompact(today), todayRows.length ? `${todayRows.length} 条模型记录` : '今日暂无记录', '◷', today],
      ['请求数', formatCompact(m.requests), '当前筛选范围', '↗', m.requests],
      ['活跃工作空间', formatNumber(m.active), '有请求记录的空间', '⌘', m.active],
      ['模型 / 供应商', formatNumber(m.models), '去重后的组合数', '✦', m.models],
      ['缓存占比', m.input ? formatPercent(m.cacheRate) : '—', m.input ? `${formatCompact(m.cache)} cache tokens` : '暂无缓存数据', '▣', m.cacheRate],
      ['计价覆盖率', formatPercent(m.coverage), m.unpricedTokens ? `${formatCompact(m.pricedTokens)} / ${formatCompact(m.total)} token` : '全部 Token 已覆盖', '✓', m.coverage]
    ];
    $('#kpiGrid').innerHTML = cards.map(card => {
      const title = card[0].startsWith('预估费用') ? formatCurrencyDetailed(card[4]) : card[0].includes('率') || card[0].includes('占比') ? card[1] : typeof card[4] === 'number' ? formatNumber(card[4]) : card[1];
      return `<article class="kpi-card"><div class="kpi-label"><span>${htmlEscape(card[0])}</span><span class="kpi-icon" aria-hidden="true">${card[3]}</span></div><div class="kpi-value" title="${htmlEscape(title)}">${htmlEscape(card[1])}</div><div class="kpi-sub">${htmlEscape(card[2])}</div></article>`;
    }).join('');
  }

  function renderWorkspace(data) {
    const host = $('#workspaceChart');
    const rows = rowsForView(data.workspaceRows).filter(row => state.workspace === 'all' || row.name === state.workspace).sort((a, b) => (b.active || b.requests || b.tokens) - (a.active || a.requests || a.tokens)).slice(0, 12);
    $('#workspaceMeta').textContent = rows.length ? `${rows.length} 个工作空间` : '无数据';
    if (!rows.length) { emptyChart(host); return; }
    const metric = rows.some(row => row.active > 0) ? 'active' : rows.some(row => row.requests > 0) ? 'requests' : 'tokens';
    const max = Math.max(...rows.map(row => row[metric]), 1);
    const width = Math.max(host.clientWidth || 620, 360);
    const rowHeight = 30;
    const height = rows.length * rowHeight + 26;
    const labelWidth = Math.min(145, Math.max(82, width * .24));
    const barWidth = width - labelWidth - 74;
    let markup = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="工作空间活跃分布，按${metric === 'active' ? '活跃次数' : metric === 'requests' ? '请求数' : 'Token'}排序"><title>工作空间活跃分布</title>`;
    [0, .5, 1].forEach(ratio => {
      const x = labelWidth + 2 + barWidth * ratio;
      markup += `<line class="chart-grid-line" x1="${x}" y1="8" x2="${x}" y2="${height - 15}"/><text class="chart-axis-label" x="${x}" y="${height - 2}" text-anchor="middle">${formatCompact(max * ratio)}</text>`;
    });
    rows.forEach((row, index) => {
      const y = 11 + index * rowHeight;
      const value = row[metric] || 0;
      const fillWidth = Math.max(value ? 2 : 0, barWidth * value / max);
      const label = row.name.length > 19 ? `${row.name.slice(0, 18)}…` : row.name;
      markup += `<text class="workspace-row-label" x="0" y="${y + 12}" title="${htmlEscape(row.name)}">${htmlEscape(label)}</text><rect class="workspace-track" x="${labelWidth}" y="${y + 2}" width="${barWidth}" height="13" rx="6.5"/><rect class="workspace-fill bar-hover" x="${labelWidth}" y="${y + 2}" width="${fillWidth}" height="13" rx="6.5"><title>${htmlEscape(row.name)}：${formatNumber(value)}</title></rect><text class="workspace-value-label" x="${width - 1}" y="${y + 13}" text-anchor="end">${formatCompact(value)}</text>`;
    });
    host.innerHTML = `${markup}</svg>`;
  }

  function renderDailyTrend(data) {
    const host = $('#dailyTrendChart');
    const rows = data.dailyActivity.filter(row => state.workspace === 'all' || !row.workspace || row.workspace === state.workspace).slice(-120);
    if (!rows.length) { emptyChart(host); return; }
    const useActive = rows.some(row => row.active > 0);
    const metricKey = useActive ? 'active' : rows.some(row => row.requests > 0) ? 'requests' : 'tokens';
    const metricName = metricKey === 'active' ? '活跃数' : metricKey === 'requests' ? '请求数' : 'Token';
    const values = rows.map(row => Math.max(0, row[metricKey] || 0));
    const max = Math.max(...values, 1);
    const width = Math.max(host.clientWidth || 620, 360);
    const height = 165;
    const left = 39, right = 10, top = 10, bottom = 27;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const x = index => rows.length === 1 ? left + plotWidth / 2 : left + index * plotWidth / (rows.length - 1);
    const y = value => top + plotHeight - value / max * plotHeight;
    let markup = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="每日${metricName}趋势"><title>每日${metricName}趋势</title>`;
    [0, .5, 1].forEach(ratio => {
      const yy = y(max * ratio);
      markup += `<line class="chart-grid-line" x1="${left}" y1="${yy}" x2="${width - right}" y2="${yy}"/><text class="chart-axis-label" x="${left - 6}" y="${yy + 3}" text-anchor="end">${formatCompact(max * ratio)}</text>`;
    });
    const points = rows.map((row, index) => `${x(index).toFixed(1)},${y(row[metricKey] || 0).toFixed(1)}`).join(' ');
    const area = `${left},${top + plotHeight} ${points} ${x(rows.length - 1)},${top + plotHeight}`;
    markup += `<polygon points="${area}" fill="var(--accent-soft)" opacity=".8"/><polyline points="${points}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    rows.forEach((row, index) => {
      const xx = x(index), yy = y(row[metricKey] || 0);
      markup += `<circle cx="${xx}" cy="${yy}" r="4" fill="var(--surface)" stroke="var(--accent)" stroke-width="2"/><rect x="${xx - Math.max(8, plotWidth / rows.length / 2)}" y="${top}" width="${Math.max(16, plotWidth / rows.length)}" height="${plotHeight}" fill="transparent"><title>${dateLabel(row.date, true)}\n请求数：${formatNumber(row.requests)}\nToken：${formatNumber(row.tokens)}</title></rect>`;
    });
    const step = Math.max(1, Math.ceil(rows.length / 7));
    rows.forEach((row, index) => { if (index % step === 0 || index === rows.length - 1) markup += `<text class="chart-axis-label" x="${x(index)}" y="${height - 7}" text-anchor="middle">${htmlEscape(dateLabel(row.date))}</text>`; });
    markup += `<text class="chart-axis-label" x="${width - right}" y="${top + 1}" text-anchor="end">${htmlEscape(metricName)}</text></svg>`;
    host.innerHTML = markup;
    $('#activityMeta').textContent = `${rows.length} 天 · ${metricName}`;
  }

  function renderHeatmap(data) {
    const host = $('#hourlyHeatmap');
    if (!data.hourlyActivity.length) { host.innerHTML = '<div class="empty-state">暂无小时分布数据。</div>'; return; }
    const hasDate = data.hourlyActivity.some(row => row.date);
    const matrix = new Map();
    data.hourlyActivity.forEach(row => {
      const key = hasDate ? row.date || '未知日期' : '全部日期';
      if (!matrix.has(key)) matrix.set(key, Array.from({ length: 24 }, () => ({ requests: 0, tokens: 0 })));
      matrix.get(key)[row.hour].requests += row.requests;
      matrix.get(key)[row.hour].tokens += row.tokens;
    });
    // Newest date first: the most recent days sit at the top of the panel,
    // still limited to the 45 most recent days.
    const isDated = key => /^\d{4}-\d{2}-\d{2}$/.test(key);
    const entries = Array.from(matrix.entries())
      .sort((a, b) => {
        if (isDated(a[0]) && isDated(b[0])) return b[0].localeCompare(a[0]);
        return (isDated(b[0]) ? 1 : 0) - (isDated(a[0]) ? 1 : 0);
      })
      .slice(0, 45);
    const max = Math.max(...entries.flatMap(([, values]) => values.map(value => value.requests)), 1);
    let html = '<div class="heat-hour"></div>' + Array.from({ length: 24 }, (_, hour) => `<div class="heat-hour">${hour % 3 === 0 ? `${hour}时` : ''}</div>`).join('');
    entries.forEach(([day, values]) => {
      html += `<div class="heat-label" title="${htmlEscape(day)}">${htmlEscape(day === '全部日期' ? day : dateLabel(day))}</div>`;
      values.forEach((value, hour) => {
        const ratio = value.requests / max;
        const level = value.requests <= 0 ? 0 : Math.min(5, Math.max(1, Math.ceil(Math.sqrt(ratio) * 5)));
        html += `<div class="heat-cell" data-level="${level}" title="${htmlEscape(day)} ${hour}:00–${hour + 1}:00\n请求数：${formatNumber(value.requests)}\nToken：${formatNumber(value.tokens)}"></div>`;
      });
    });
    host.innerHTML = html;
  }

  function renderActivity(data) {
    renderDailyTrend(data);
    renderHeatmap(data);
  }

  function renderRequestDistribution(data) {
    const distribution = data.requestDistribution;
    const rows = distribution.bins;
    const allValues = rows.flatMap(row => [row.min, row.max]).filter(value => value > 0);
    const inferred = allValues.length ? allValues : [distribution.p50, distribution.p90, distribution.p99].filter(value => value > 0);
    const fallbackPercentile = inferred.length ? inferred[Math.floor(inferred.length / 2)] : 0;
    const percentiles = { p50: distribution.p50 || fallbackPercentile, p90: distribution.p90 || fallbackPercentile, p99: distribution.p99 || fallbackPercentile };
    $('#requestStats').innerHTML = [['p50', percentiles.p50, '中位请求'], ['p90', percentiles.p90, '90% 请求不超过'], ['p99', percentiles.p99, '极端请求上界']].map(item => `<div class="percentile-card ${item[0]}"><span>${item[0].toUpperCase()} · ${item[2]}</span><strong title="${htmlEscape(formatNumber(item[1]))}">${htmlEscape(formatCompact(item[1]))}</strong></div>`).join('');
    $('#requestMeta').textContent = rows.length ? `${formatNumber(rows.reduce((sum, row) => sum + row.count, 0))} 次请求` : '暂无分布';
    const host = $('#requestChart');
    if (!rows.length) { emptyChart(host, '暂无直方图', percentiles.p50 ? '接口返回了分位数，但还没有直方图分桶。' : '当前筛选范围内没有请求数据。'); return; }
    const width = Math.max(host.clientWidth || 620, 360), height = 220, left = 41, right = 12, top = 18, bottom = 34;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const maxCount = Math.max(...rows.map(row => row.count), 1);
    const maxValue = Math.max(...rows.map(row => row.max || row.min), percentiles.p99, 1);
    const barWidth = plotWidth / rows.length;
    const xFor = value => {
      const index = rows.findIndex((row, i) => value < row.max || i === rows.length - 1);
      const row = rows[Math.max(0, index)];
      const upper = row.max > row.min ? row.max : maxValue;
      return left + (Math.max(0, index) + Math.max(0, Math.min(1, (value - row.min) / Math.max(1, upper - row.min)))) * barWidth;
    };
    const yFor = value => top + plotHeight - value / maxCount * plotHeight;
    let markup = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="单次请求 Token 大小直方图"><title>单次请求 Token 大小分布</title>`;
    [0, .5, 1].forEach(ratio => { const yy = yFor(maxCount * ratio); markup += `<line class="chart-grid-line" x1="${left}" y1="${yy}" x2="${width - right}" y2="${yy}"/><text class="chart-axis-label" x="${left - 6}" y="${yy + 3}" text-anchor="end">${formatCompact(maxCount * ratio)}</text>`; });
    rows.forEach((row, index) => {
      const x = left + index * barWidth + 1, barHeight = Math.max(row.count ? 1 : 0, row.count / maxCount * plotHeight);
      markup += `<rect class="bar-hover" x="${x}" y="${top + plotHeight - barHeight}" width="${Math.max(1, barWidth - 2)}" height="${barHeight}" rx="2" fill="var(--accent)"><title>${htmlEscape(row.label)}：${formatNumber(row.count)} 次</title></rect>`;
      if (rows.length <= 14 || index % Math.ceil(rows.length / 8) === 0 || index === rows.length - 1) markup += `<text class="chart-axis-label" x="${x + barWidth / 2 - 1}" y="${height - 11}" text-anchor="middle">${htmlEscape(row.label)}</text>`;
    });
    [['p50', percentiles.p50, 'var(--accent)'], ['p90', percentiles.p90, 'var(--green)'], ['p99', percentiles.p99, 'var(--amber)']].forEach(([label, value, color]) => {
      if (!value) return;
      const x = xFor(value);
      markup += `<line x1="${x}" y1="${top - 3}" x2="${x}" y2="${top + plotHeight}" stroke="${color}" stroke-width="1.5" stroke-dasharray="4 3"/><text x="${Math.min(width - 8, x + 4)}" y="${top + 2}" fill="${color}" font-size="9">${label}</text>`;
    });
    markup += `<line class="chart-axis-line" x1="${left}" y1="${top + plotHeight}" x2="${width - right}" y2="${top + plotHeight}"/><text class="chart-axis-label" x="${width - right}" y="${height - 11}" text-anchor="end">Token 数量</text></svg>`;
    host.innerHTML = markup;
  }

  function modelKey(row) { return `${row.model}|||${row.provider}`; }
  function modelLabel(key) { const [model, provider] = key.split('|||'); return provider && provider !== '未知供应商' ? `${model} · ${provider}` : model; }

  function renderModelUsage(data) {
    const host = $('#modelUsageChart');
    const rows = rowsForView(data.dailyModelUsage).filter(row => state.workspace === 'all' || !row.workspace || row.workspace === state.workspace);
    const dates = unique(rows.map(row => row.date)).sort();
    if (!rows.length || !dates.length) { $('#modelLegend').innerHTML = ''; $('#modelSummary').innerHTML = ''; emptyChart(host); $('#modelMeta').textContent = '无数据'; return; }
    const totalsByKey = new Map();
    rows.forEach(row => totalsByKey.set(modelKey(row), (totalsByKey.get(modelKey(row)) || 0) + row.total));
    const keys = Array.from(totalsByKey.keys()).sort((a, b) => totalsByKey.get(b) - totalsByKey.get(a));
    const displayKeys = keys.slice(0, 9);
    const hasOther = keys.length > displayKeys.length;
    if (hasOther) displayKeys.push('__other__');
    const seriesName = key => key === '__other__' ? '其他模型 / 供应商' : modelLabel(key);
    const color = key => key === '__other__' ? 'var(--chart-muted)' : palette[Math.max(0, keys.indexOf(key)) % palette.length];
    $('#modelLegend').innerHTML = displayKeys.map(key => `<span class="legend-item"><i class="legend-swatch" style="background:${color(key)}"></i><span>${htmlEscape(seriesName(key))}</span></span>`).join('');
    const grouped = new Map(dates.map(date => [date, new Map()]));
    rows.forEach(row => {
      const key = displayKeys.includes(modelKey(row)) ? modelKey(row) : '__other__';
      const day = grouped.get(row.date);
      if (day) day.set(key, (day.get(key) || 0) + row.total);
    });
    const dateTotals = dates.map(date => Array.from(grouped.get(date).values()).reduce((sum, value) => sum + value, 0));
    const maxTotal = Math.max(...dateTotals, 1);
    const width = Math.max(host.clientWidth || 620, 360), height = 285, left = 45, right = 10, top = 10, bottom = 37;
    const plotWidth = width - left - right, plotHeight = height - top - bottom, slot = plotWidth / dates.length, barWidth = Math.max(3, Math.min(34, slot * .64));
    const yFor = value => top + plotHeight - value / maxTotal * plotHeight;
    let markup = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="模型与供应商每日 Token 用量堆叠图"><title>模型与供应商每日 Token 用量</title>`;
    [0, .5, 1].forEach(ratio => { const yy = yFor(maxTotal * ratio); markup += `<line class="chart-grid-line" x1="${left}" y1="${yy}" x2="${width - right}" y2="${yy}"/><text class="chart-axis-label" x="${left - 6}" y="${yy + 3}" text-anchor="end">${formatCompact(maxTotal * ratio)}</text>`; });
    dates.forEach((date, index) => {
      const values = grouped.get(date), x = left + index * slot + (slot - barWidth) / 2;
      let cumulative = 0;
      displayKeys.forEach(key => {
        const value = values.get(key) || 0;
        if (!value) return;
        const yTop = yFor(cumulative + value), yBottom = yFor(cumulative);
        markup += `<rect class="bar-hover" x="${x}" y="${yTop}" width="${barWidth}" height="${Math.max(1, yBottom - yTop)}" rx="2" fill="${color(key)}"><title>${htmlEscape(dateLabel(date, true))} · ${htmlEscape(seriesName(key))}：${formatNumber(value)}</title></rect>`;
        cumulative += value;
      });
      const labelStep = Math.max(1, Math.ceil(dates.length / 9));
      if (index % labelStep === 0 || index === dates.length - 1) markup += `<text class="chart-axis-label" x="${x + barWidth / 2}" y="${height - 13}" text-anchor="middle">${htmlEscape(dateLabel(date))}</text>`;
    });
    markup += `<line class="chart-axis-line" x1="${left}" y1="${top + plotHeight}" x2="${width - right}" y2="${top + plotHeight}"/><text class="chart-axis-label" x="${width - right}" y="${height - 1}" text-anchor="end">日期</text></svg>`;
    host.innerHTML = markup;
    const summaryRows = data.dailyModelSummary.length ? rowsForView(data.dailyModelSummary) : summarizeRows(rows);
    const summary = new Map();
    summaryRows.forEach(row => { const key = modelKey(row); if (!summary.has(key)) summary.set(key, { total: 0, requests: 0 }); summary.get(key).total += row.total; summary.get(key).requests += row.requests; });
    $('#modelSummary').innerHTML = Array.from(summary.entries()).sort((a, b) => b[1].total - a[1].total).slice(0, 6).map(([key, value]) => `<div class="summary-row"><span class="summary-name" title="${htmlEscape(modelLabel(key))}"><i class="legend-swatch" style="display:inline-block;background:${color(key)};margin-right:5px"></i>${htmlEscape(modelLabel(key))}</span><strong title="${htmlEscape(formatNumber(value.total))}">${htmlEscape(formatCompact(value.total))}</strong></div>`).join('');
    $('#modelMeta').textContent = `${dates.length} 天 · ${displayKeys.length}${hasOther ? '+' : ''} 个组合`;
  }

  function renderDailyTable(data) {
    const tbody = $('#dailyUsageTable tbody');
    const rows = rowsForView(data.dailyModelUsage).filter(row => state.workspace === 'all' || !row.workspace || row.workspace === state.workspace).sort((a, b) => b.date.localeCompare(a.date) || b.total - a.total);
    const totalPages = Math.max(1, Math.ceil(rows.length / state.tablePageSize));
    state.tablePage = Math.min(Math.max(1, state.tablePage), totalPages);
    const pageStart = (state.tablePage - 1) * state.tablePageSize;
    const pageRows = rows.slice(pageStart, pageStart + state.tablePageSize);
    $('#detailMeta').textContent = rows.length ? `共 ${formatNumber(rows.length)} 条 · 第 ${state.tablePage}/${totalPages} 页` : '无数据';
    $('#tableEmpty').hidden = rows.length > 0;
    tbody.innerHTML = pageRows.map(row => {
      const cost = row.pricingStatus === 'unpriced'
        ? '<span class="provider-pill" title="官方价格未公开或尚未配置">未计价</span>'
        : `<strong title="${htmlEscape(formatCurrencyDetailed(row.cost))}">${htmlEscape(formatCurrency(row.cost))}${row.pricingStatus === 'partial' ? '*' : ''}</strong>`;
      const pricingTitle = row.pricingStatus === 'unpriced'
        ? (row.pricingNote || '未计价')
        : `${row.pricingModel || row.model}${row.pricingRateBand ? ` · ${row.pricingRateBand}` : ''}`;
      return `<tr><td>${htmlEscape(dateLabel(row.date, true))}</td><td><strong title="${htmlEscape(row.model)}">${htmlEscape(row.model)}</strong></td><td><span class="provider-pill" title="${htmlEscape(row.provider)}">${htmlEscape(row.provider)}</span></td><td class="num">${htmlEscape(formatCompact(row.input))}</td><td class="num">${htmlEscape(formatCompact(row.output))}</td><td class="num">${htmlEscape(formatCompact(row.cache))}</td><td class="num">${htmlEscape(formatCompact(row.reasoning))}</td><td class="num"><strong title="${htmlEscape(formatNumber(row.total))}">${htmlEscape(formatCompact(row.total))}</strong></td><td class="num" title="${htmlEscape(pricingTitle)}">${cost}</td><td class="num">${htmlEscape(formatCompact(row.requests))}</td></tr>`;
    }).join('');
    $('#tablePaginationSummary').textContent = rows.length
      ? `共 ${formatNumber(rows.length)} 条，每页 ${state.tablePageSize} 条 · 第 ${state.tablePage} / ${totalPages} 页`
      : '当前筛选范围内没有记录';
    $('#prevTablePage').disabled = !rows.length || state.tablePage <= 1;
    $('#nextTablePage').disabled = !rows.length || state.tablePage >= totalPages;
  }

  function renderAll(data) {
    renderFilterOptions(data);
    updateRangeDescription(data);
    renderKpis(data);
    renderWorkspace(data);
    renderActivity(data);
    renderRequestDistribution(data);
    renderModelUsage(data);
    renderDailyTable(data);
    const models = summarizeRows(rowsForView(data.dailyModelUsage));
    const total = models.reduce((sum, row) => sum + row.total, 0);
    $('#modelTotals').innerHTML = models.length ? models.map((row, index) => {
      const costLabel = row.pricingStatus === 'unpriced'
        ? '未计价'
        : `预估 ${formatCurrency(row.cost)}${row.pricingStatus === 'partial' ? ' *' : ''}`;
      return `<article class="model-stat"><div class="model-stat-heading"><strong>${htmlEscape(row.model)}</strong><span class="provider-pill">${htmlEscape(row.provider)}</span></div><div class="model-stat-value" title="${formatNumber(row.total)} Token">${formatCompact(row.total)} <small>Token</small></div><div class="model-stat-bar"><i style="width:${total ? row.total / total * 100 : 0}%;background:${palette[index % palette.length]}"></i></div><div class="model-stat-meta">${formatPercent(total ? row.total / total : 0)} · ${formatNumber(row.requests)} 次请求 · ${htmlEscape(costLabel)}</div><div class="model-stat-meta">输入 ${formatNumber(row.input)} · 输出 ${formatNumber(row.output)}</div></article>`;
    }).join('') : '<div class="empty-state">当前组合暂无模型用量，可直接切换任一筛选项。</div>';
    enhanceChartTooltips();
  }

  function setRange(value) {
    state.range = String(value);
    state.tablePage = 1;
    $$('#rangeControls button').forEach(button => button.classList.toggle('is-active', button.dataset.range === state.range));
    fetchDashboard();
  }

  function buildUrl() {
    const params = new URLSearchParams();
    params.set('days', state.range === 'all' ? '0' : state.range);
    if (state.provider !== 'all') params.set('provider', state.provider);
    if (state.workspace !== 'all') params.set('workspace', state.workspace);
    if (state.model !== 'all') params.set('model', state.model);
    const query = params.toString();
    return query ? `${API_URL}?${query}` : API_URL;
  }

  async function fetchDashboard(options = {}) {
    if (state.dashboardController) state.dashboardController.abort();
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    state.dashboardController = controller;
    const requestId = ++state.requestId;
    state.loading = true;
    if (!options.silent) setConnection('connecting', '读取数据');
    try {
      const response = await fetch(buildUrl(), {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
        signal: controller ? controller.signal : undefined
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (requestId !== state.requestId || state.dashboardController !== controller) return;
      state.data = normalize(payload);
      renderAll(state.data);
      updateUpdated(state.data.generatedAt);
      showError('');
      if (!state.eventSource || state.eventSource.readyState === EventSource.OPEN) setConnection('online', '实时连接');
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      if (requestId !== state.requestId || state.dashboardController !== controller) return;
      const message = error && error.message ? error.message : '网络请求失败';
      showError(`无法读取看板数据（${message}）。请确认服务已启动后重试。`);
      if (!state.data) renderEmptyDashboard();
      setConnection('offline', '暂时离线');
    } finally {
      if (requestId === state.requestId && state.dashboardController === controller) {
        state.loading = false;
        state.dashboardController = null;
      }
    }
  }

  function renderEmptyDashboard() {
    const empty = { summary: {}, filters: { providers: [], workspaces: [] }, workspaceRows: [], dailyActivity: [], hourlyActivity: [], requestDistribution: { bins: [], p50: 0, p90: 0, p99: 0 }, dailyModelUsage: [], dailyModelSummary: [], generatedAt: '' };
    renderAll(empty);
    updateUpdated('');
  }

  function scheduleRefresh() {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(() => fetchDashboard({ silent: true }), 300);
  }

  function connectEvents() {
    if (!window.EventSource) {
      setConnection('reconnecting', '轮询更新');
      state.pollTimer = window.setInterval(() => fetchDashboard({ silent: true }), 30000);
      return;
    }
    try {
      const source = new EventSource(EVENTS_URL);
      state.eventSource = source;
      source.onopen = () => { setConnection('online', '实时连接'); };
      source.onmessage = event => {
        if (!event.data) return;
        try {
          const message = JSON.parse(event.data);
          if (message.type === 'ping' || message.event === 'ping') return;
        } catch (_) { /* any non-empty event is treated as a refresh signal */ }
        scheduleRefresh();
      };
      ['dashboard', 'dashboard_updated', 'dashboard-update', 'usage.updated', 'update', 'refresh'].forEach(eventName => source.addEventListener(eventName, scheduleRefresh));
      source.onerror = () => {
        setConnection('reconnecting', '重连中');
        if (!state.pollTimer) state.pollTimer = window.setInterval(() => { if (!source || source.readyState !== EventSource.OPEN) fetchDashboard({ silent: true }); }, 30000);
      };
    } catch (_) {
      setConnection('offline', '事件流不可用');
      state.pollTimer = window.setInterval(() => fetchDashboard({ silent: true }), 30000);
    }
  }

  function bindEvents() {
    $$('#rangeControls button').forEach(button => button.addEventListener('click', () => setRange(button.dataset.range)));
    $('#providerFilter').addEventListener('change', event => { state.provider = event.target.value; state.tablePage = 1; fetchDashboard(); });
    $('#workspaceFilter').addEventListener('change', event => { state.workspace = event.target.value; state.tablePage = 1; fetchDashboard(); });
    $('#modelFilter').addEventListener('change', event => { state.model = event.target.value; state.tablePage = 1; fetchDashboard(); });
    $('#resetFilters').addEventListener('click', () => { state.provider = state.workspace = state.model = 'all'; setRange('30'); });
    $('#refreshButton').addEventListener('click', () => fetchDashboard());
    $('#exportButton').addEventListener('click', exportCsv);
    $('#retryButton').addEventListener('click', () => fetchDashboard());
    $('#themeButton').addEventListener('click', () => { setTheme(state.theme === 'dark' ? 'light' : 'dark', { persist: true }); if (state.data) renderAll(state.data); });
    $('#prevTablePage').addEventListener('click', () => {
      if (!state.data || state.tablePage <= 1) return;
      state.tablePage -= 1;
      renderDailyTable(state.data);
      $('.table-wrap').scrollTop = 0;
    });
    $('#nextTablePage').addEventListener('click', () => {
      if (!state.data) return;
      state.tablePage += 1;
      renderDailyTable(state.data);
      $('.table-wrap').scrollTop = 0;
    });
    window.addEventListener('resize', () => { if (!state.data) return; clearTimeout(window.__codexResize); window.__codexResize = setTimeout(() => renderAll(state.data), 130); });
  }

  function csvCell(value) {
    const text = String(value == null ? '' : value);
    const raw = typeof value === 'string' && /^[\s]*[=+@-]/.test(text) ? "'" + text : text;
    return /[",\r\n]/.test(raw) ? `"${raw.replace(/"/g, '""')}"` : raw;
  }

  async function exportCsv() {
    if (!state.data) return;
    const fields = ['date', 'model', 'provider', 'provider_label', 'input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'reasoning_output_tokens', 'total_tokens', 'estimated_cost_usd', 'pricing_status', 'pricing_model', 'pricing_rate_band', 'request_count'];
    const rows = rowsForView(state.data.dailyModelUsage)
      .filter(row => state.workspace === 'all' || !row.workspace || row.workspace === state.workspace)
      .sort((a, b) => b.date.localeCompare(a.date) || b.total - a.total)
      .map(row => ({
        date: row.date,
        model: row.model,
        provider: row.providerId,
        provider_label: row.provider,
        input_tokens: row.input,
        output_tokens: row.output,
        cached_input_tokens: row.cache,
        cache_write_input_tokens: row.cacheWrite,
        reasoning_output_tokens: row.reasoning,
        total_tokens: row.total,
        estimated_cost_usd: row.pricingStatus === 'unpriced' ? '' : row.cost.toFixed(8),
        pricing_status: row.pricingStatus,
        pricing_model: row.pricingModel || row.model,
        pricing_rate_band: row.pricingRateBand,
        request_count: row.requests
      }));
    const csv = '\ufeff' + [fields.join(','), ...rows.map(row => fields.map(field => csvCell(row[field])).join(','))].join('\r\n');
    const filename = `codex-token-${dateKey(new Date())}.csv`;
    const button = $('#exportButton');
    const original = button.querySelector('span').textContent;
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.save_csv) {
        const result = await window.pywebview.api.save_csv(filename, csv);
        if (result && result.ok) {
          button.querySelector('span').textContent = '已导出';
          window.setTimeout(() => { button.querySelector('span').textContent = original; }, 1800);
        } else if (result && result.error) {
          showError(`导出失败：${result.error}`);
        }
        return;
      }
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
    } catch (error) {
      showError(`导出失败：${error && error.message ? error.message : '未知错误'}`);
    }
  }

  function enhanceChartTooltips() {
    const tooltip = $('#chartTooltip');
    tooltip.hidden = true;
    $$('.chart-host svg title').forEach(title => {
      const target = title.parentElement;
      if (target.tagName.toLowerCase() === 'svg') return;
      target.dataset.tooltip = title.textContent;
      target.setAttribute('tabindex', '0');
      target.setAttribute('aria-label', title.textContent);
      title.remove();
    });
    $$('.heat-cell').forEach(cell => {
      cell.dataset.tooltip = cell.title;
      cell.removeAttribute('title');
      cell.setAttribute('tabindex', '0');
      cell.setAttribute('aria-label', cell.dataset.tooltip);
    });
    $$('[data-tooltip]').forEach(target => {
      const show = event => {
        const bounds = target.getBoundingClientRect();
        tooltip.textContent = target.dataset.tooltip;
        tooltip.hidden = false;
        const x = event.clientX ?? bounds.left;
        const y = event.clientY ?? bounds.top;
        tooltip.style.left = `${Math.max(8, Math.min(x + 14, window.innerWidth - tooltip.offsetWidth - 8))}px`;
        tooltip.style.top = `${Math.max(8, Math.min(y + 16, window.innerHeight - tooltip.offsetHeight - 8))}px`;
      };
      target.addEventListener('pointermove', show);
      target.addEventListener('focus', show);
      target.addEventListener('pointerleave', () => { tooltip.hidden = true; });
      target.addEventListener('blur', () => { tooltip.hidden = true; });
    });
  }

  window.addEventListener('pywebviewready', loadDesktopPreferences);
  loadDesktopPreferences();
  startHealthPolling();
  setTheme(state.theme);
  bindEvents();
  renderEmptyDashboard();
  fetchDashboard();
  connectEvents();
})();
