(() => {
  'use strict';

  const STORAGE_KEY = 'codex-token-dashboard-layout-v1';
  const PANEL_STORAGE_KEY = 'codex-token-dashboard-panels-v1';
  const PAGE_MODULES = Object.freeze({
    overview: [
      { id: 'overview-kpis', span: 12, label: '关键指标' },
      { id: 'overview-trend', span: 12, label: '每日费用与 Token 趋势' },
      { id: 'overview-comparison', span: 4, label: '期间对比' },
      { id: 'overview-cache', span: 4, label: '缓存与输入拆分' },
      { id: 'overview-ttft', span: 4, label: '首字延迟' },
      { id: 'overview-models', span: 12, label: '模型 Token 统计' }
    ],
    trends: [
      { id: 'trends-workspace', span: 6, label: '工作空间活跃分布' },
      { id: 'trends-activity', span: 6, label: '日活分布' },
      { id: 'trends-requests', span: 6, label: '单次请求 Token 大小分布' },
      { id: 'trends-models', span: 6, label: '模型日使用分布汇总' }
    ],
    usage: [
      { id: 'usage-daily', span: 12, label: '每日模型用量明细' },
      { id: 'usage-requests', span: 12, label: '单次请求明细' },
      { id: 'usage-sessions', span: 12, label: '会话明细' },
      { id: 'usage-tasks', span: 12, label: '任务与代理消耗' }
    ],
    pricing: [
      { id: 'pricing-rules', span: 12, label: '价格规则' },
      { id: 'pricing-diagnostics', span: 12, label: '数据来源与扫描诊断' }
    ]
  });
  const viewNames = new Set(Object.keys(PAGE_MODULES));
  const pageLabels = { overview: '总览', trends: '趋势分析', usage: '用量明细', pricing: '价格与数据' };
  const viewTabs = new Map([
    ['overview', 'viewTabOverview'], ['trends', 'viewTabTrends'],
    ['usage', 'viewTabUsage'], ['pricing', 'viewTabPricing']
  ]);
  const subviewTabs = {
    usage: { daily: 'usageTabDaily', requests: 'usageTabRequests', sessions: 'usageTabSessions', tasks: 'usageTabTasks', all: 'usageTabAll' },
    pricing: { rules: 'pricingTabRules', diagnostics: 'pricingTabDiagnostics', all: 'pricingTabAll' }
  };
  const isRecord = value => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

  function defaultPages() {
    return Object.fromEntries(Object.entries(PAGE_MODULES).map(([page, items]) =>
      [page, items.map(item => ({ id: item.id, span: item.span, height: 0 }))]));
  }

  // Only known modules survive deserialization. Missing modules are appended, never lost.
  function normalizeLayout(value) {
    if (!isRecord(value) || value.version !== 1 || !isRecord(value.pages)) return null;
    const result = { version: 1, view: 'overview', usage: 'daily', pricing: 'rules', pages: defaultPages() };
    if (viewNames.has(value.view)) result.view = value.view;
    for (const page of ['usage', 'pricing']) {
      if (Object.prototype.hasOwnProperty.call(subviewTabs[page], value[page])) result[page] = value[page];
    }
    for (const [page, defaults] of Object.entries(result.pages)) {
      const known = new Map(defaults.map(item => [item.id, item]));
      const seen = new Set();
      const ordered = [];
      const candidates = Array.isArray(value.pages[page]) ? value.pages[page].slice(0, 64) : [];
      for (const item of candidates) {
        if (!isRecord(item) || !known.has(item.id) || seen.has(item.id)) continue;
        const fallback = known.get(item.id);
        const span = Number.isInteger(item.span) && item.span >= 3 && item.span <= 12 ? item.span : fallback.span;
        const height = Number.isInteger(item.height) && (item.height === 0 || (item.height >= 200 && item.height <= 2400)) ? item.height : 0;
        ordered.push({ id: item.id, span, height });
        seen.add(item.id);
      }
      result.pages[page] = ordered.concat(defaults.filter(item => !seen.has(item.id)));
    }
    return result;
  }

  // Native storage is authoritative for untouched modules. Replay only changes made
  // in this window while it was loading, so navigation cannot replace saved panels.
  function mergeLoadedLayout(saved, current, changes) {
    const result = normalizeLayout(saved);
    const local = normalizeLayout(current);
    if (!result || !local) return result || local;
    for (const field of changes.navigation || []) {
      if (['view', 'usage', 'pricing'].includes(field)) result[field] = local[field];
    }
    for (const operation of changes.operations || []) {
      const items = result.pages[operation.page];
      if (!items) continue;
      if (operation.type === 'reset') {
        result.pages[operation.page] = defaultPages()[operation.page];
        continue;
      }
      const index = items.findIndex(item => item.id === operation.id);
      if (index < 0) continue;
      if (operation.type === 'size') {
        for (const field of ['span', 'height']) {
          if (Object.prototype.hasOwnProperty.call(operation, field)) items[index][field] = operation[field];
        }
      } else if (operation.type === 'move') {
        const item = items.splice(index, 1)[0];
        const before = operation.before ? items.findIndex(item => item.id === operation.before) : -1;
        items.splice(before < 0 ? items.length : before, 0, item);
      }
    }
    return normalizeLayout(result);
  }

  // Coalesce pending updates and await each write so an older save cannot finish last.
  function createSerialSaver(save, onResult = () => {}) {
    let queued = null;
    let running = false;
    let active = Promise.resolve();
    async function drain() {
      try {
        while (queued) {
          const current = queued;
          queued = null;
          try {
            const result = await save(current);
            onResult(Boolean(result && result.ok), result);
          } catch (error) { onResult(false, error); }
        }
      } finally { running = false; }
    }
    return {
      enqueue(value) {
        queued = value;
        if (!running) { running = true; active = drain(); }
        return active;
      },
      whenIdle: () => active
    };
  }

  if (typeof module === 'object' && module.exports) module.exports = { PAGE_MODULES, normalizeLayout, defaultPages, mergeLoadedLayout, createSerialSaver };
  if (typeof document === 'undefined' || typeof window === 'undefined') return;

  const state = { view: 'overview', usage: 'daily', pricing: 'rules' };
  let pages = defaultPages();
  let initialized = false;
  let pendingSelection = false;
  let resizeQueued = false;
  let editing = false;
  let revision = 0;
  let interaction = null;
  let bridgeConnecting = false;
  let bridgeLoaded = false;
  let bridgeSaver = null;
  let pendingNativeLoad = null;
  const navigationEdits = new Set();
  const layoutEdits = [];
  let localStorageAvailable = true;
  let statusMessage = '布局自动保存在本机。';

  const byId = id => document.getElementById(id);
  function nextFrame(callback) {
    if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(callback);
    else window.setTimeout(callback, 0);
  }
  function snapshot() {
    return { version: 1, ...state, pages: Object.fromEntries(Object.entries(pages).map(([page, items]) => [page, items.map(item => ({ ...item }))])) };
  }
  function acceptLayout(layout) {
    pages = layout.pages;
    state.view = layout.view;
    state.usage = layout.usage;
    state.pricing = layout.pricing;
  }
  function readSavedState() {
    try {
      const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || 'null');
      if (isRecord(saved)) {
        if (viewNames.has(saved.view)) state.view = saved.view;
        for (const page of ['usage', 'pricing']) {
          if (Object.prototype.hasOwnProperty.call(subviewTabs[page], saved[page])) state[page] = saved[page];
        }
      }
    } catch (_) { /* Storage is optional, including blocked or damaged browser storage. */ }
    try {
      const layout = normalizeLayout(JSON.parse(window.localStorage.getItem(PANEL_STORAGE_KEY) || 'null'));
      if (layout) acceptLayout(layout);
    } catch (_) { /* A damaged legacy navigation value must not prevent loading a valid layout. */ }
  }
  function announce(message) {
    statusMessage = message;
    const status = byId('layoutSaveStatus');
    if (status) status.textContent = message;
  }
  function saveState() {
    const layout = snapshot();
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
      window.localStorage.setItem(PANEL_STORAGE_KEY, JSON.stringify(layout));
      localStorageAvailable = true;
    } catch (_) { localStorageAvailable = false; }
    if (bridgeLoaded && bridgeSaver) {
      announce('正在保存布局…');
      bridgeSaver.enqueue(layout);
    } else if (window.pywebview && window.pywebview.api) {
      announce('正在读取已保存布局；读取成功前不会覆盖本机设置。');
      connectBridge();
    } else if (!localStorageAvailable) {
      announce('当前存储不可用，布局仅在本次窗口内保留。');
    } else {
      announce('布局已保存在此浏览器；客户端启动后同步保存到本机。');
    }
  }
  async function connectBridge() {
    const api = window.pywebview && window.pywebview.api;
    if (!initialized || bridgeConnecting || bridgeLoaded || !api || typeof api.load_layout !== 'function' || typeof api.save_layout !== 'function') return;
    bridgeConnecting = true;
    const loadRevision = revision;
    let loadedSuccessfully = false;
    bridgeSaver = createSerialSaver(value => api.save_layout(value), ok => {
      announce(ok ? '布局已保存到本机，关闭后重开仍会保留。' : '本机布局保存失败；当前布局仍可使用，请稍后再次调整以重试。');
    });
    try {
      const result = await api.load_layout();
      const saved = result && result.ok ? normalizeLayout(result.layout) : null;
      // A non-empty layout that this version cannot read is not a first run.
      // Preserve it on disk instead of replacing it with this build's defaults.
      loadedSuccessfully = Boolean(result && result.ok && (result.layout == null || saved));
      completeNativeLoad(saved, loadedSuccessfully, loadRevision);
      return;
    } catch (_) { /* Local browser preferences remain usable when the bridge fails. */ }
    completeNativeLoad(null, false, loadRevision);
  }
  function completeNativeLoad(saved, loadedSuccessfully, loadRevision) {
    if (!loadedSuccessfully) {
      bridgeConnecting = false;
      announce('暂时无法读取本机布局，已保留原设置；再次调整或回到窗口时重试。');
      return;
    }
    // Reparenting a module during pointer capture would interrupt a live gesture.
    if (saved && interaction) {
      pendingNativeLoad = { saved, loadedSuccessfully, loadRevision };
      return;
    }
    if (saved) {
      acceptLayout(mergeLoadedLayout(saved, snapshot(), { navigation: navigationEdits, operations: layoutEdits }));
      applyAllPages();
      renderState(null);
    }
    bridgeLoaded = true;
    bridgeConnecting = false;
    navigationEdits.clear();
    layoutEdits.length = 0;
    saveState();
  }
  function setTabState(tab, selected) {
    if (!tab) return;
    tab.classList.toggle('is-selected', selected);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  function renderState(previousView) {
    if (editing && subviewTabs[state.view]) {
      state[state.view] = 'all';
      if (!bridgeLoaded) navigationEdits.add(state.view);
    }
    document.querySelectorAll('[data-view-panel]').forEach(panel => { panel.hidden = panel.dataset.viewPanel !== state.view; });
    viewTabs.forEach((id, view) => setTabState(byId(id), view === state.view));
    Object.entries(subviewTabs).forEach(([view, tabs]) => {
      document.querySelectorAll('[data-subview-panel]').forEach(panel => {
        const parentView = panel.closest('[data-view-panel]');
        if (parentView && parentView.dataset.viewPanel === view) panel.hidden = state[view] !== 'all' && panel.dataset.subviewPanel !== state[view];
      });
      Object.entries(tabs).forEach(([subview, id]) => setTabState(byId(id), subview === state[view]));
    });
    const pageName = byId('layoutPageName');
    if (pageName) pageName.textContent = `编辑${pageLabels[state.view]}布局`;
    saveState();
    if (previousView !== state.view) requestVisibleCharts();
  }
  function requestVisibleCharts() {
    if (resizeQueued) return;
    resizeQueued = true;
    nextFrame(() => {
      resizeQueued = false;
      window.dispatchEvent(new Event('resize'));
    });
  }
  function showView(view, subview) {
    if (!viewNames.has(view)) return false;
    finishInteraction(false);
    const previousView = state.view;
    state.view = view;
    if (!bridgeLoaded) navigationEdits.add('view');
    if (subviewTabs[view] && subview && Object.prototype.hasOwnProperty.call(subviewTabs[view], subview)) {
      state[view] = subview;
      if (!bridgeLoaded) navigationEdits.add(view);
    }
    revision += 1;
    pendingSelection = true;
    if (initialized) { renderState(previousView); pendingSelection = false; }
    return true;
  }
  function tablistFor(tab) { return tab.closest('[role="tablist"]'); }
  function activateTab(tab) {
    if (tab.dataset.view) return showView(tab.dataset.view);
    if (!tab.dataset.subview) return false;
    const section = tab.closest('#view-usage, #view-pricing');
    if (!section) return false;
    return showView(section.id === 'view-usage' ? 'usage' : 'pricing', tab.dataset.subview);
  }
  function moveTabFocus(event) {
    const tab = event.target.closest('[role="tab"]');
    if (!tab) return;
    const list = tablistFor(tab);
    if (!list) return;
    const tabs = Array.from(list.querySelectorAll('[role="tab"]')).filter(item => !item.disabled);
    const index = tabs.indexOf(tab);
    if (index < 0) return;
    let nextIndex;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = tabs.length - 1;
    else return;
    event.preventDefault();
    tabs[nextIndex].focus();
    activateTab(tabs[nextIndex]);
  }
  function moduleInfo(id) {
    for (const [page, items] of Object.entries(pages)) {
      const index = items.findIndex(item => item.id === id);
      if (index >= 0) return { page, index, item: items[index], label: PAGE_MODULES[page].find(item => item.id === id).label };
    }
    return null;
  }
  function moduleElement(id) { return document.querySelector(`[data-layout-id="${id}"]`); }
  function makeControl(tag, className, text) {
    const control = document.createElement(tag);
    control.className = className;
    if (tag === 'button') control.type = 'button';
    if (text) control.textContent = text;
    return control;
  }
  function addModuleControls(module, info) {
    const controls = makeControl('div', 'layout-module-controls');
    controls.setAttribute('aria-label', `${info.label}布局设置`);
    controls.hidden = true;
    const drag = makeControl('button', 'layout-drag-handle', '⠿');
    drag.dataset.layoutAction = 'drag';
    drag.setAttribute('aria-label', `拖动${info.label}排序；也可按方向键移动`);
    drag.title = '拖动排序；方向键前后移动，Home / End 移到开头或末尾';
    controls.appendChild(drag);
    const name = makeControl('span', 'layout-module-name', info.label);
    name.title = info.label;
    controls.appendChild(name);
    for (const [action, text, title] of [['previous', '←', '前移一个位置'], ['next', '→', '后移一个位置']]) {
      const button = makeControl('button', 'layout-step-button', text);
      button.dataset.layoutAction = action;
      button.setAttribute('aria-label', `${info.label}${title}`);
      button.title = title;
      controls.appendChild(button);
    }
    const widthLabel = makeControl('label', 'layout-size-field', '宽');
    const width = makeControl('select', 'layout-width-select');
    width.dataset.layoutAction = 'width';
    width.setAttribute('aria-label', `${info.label}宽度`);
    for (let span = 3; span <= 12; span += 1) {
      const option = document.createElement('option');
      option.value = String(span);
      option.textContent = span === 12 ? '整行' : `${span} / 12`;
      width.appendChild(option);
    }
    widthLabel.appendChild(width);
    controls.appendChild(widthLabel);
    const heightLabel = makeControl('label', 'layout-size-field', '高');
    const height = makeControl('input', 'layout-height-input');
    height.type = 'number';
    height.min = '200'; height.max = '2400'; height.step = '20';
    height.placeholder = '自动';
    height.dataset.layoutAction = 'height';
    height.setAttribute('aria-label', `${info.label}高度，像素，留空为自动`);
    heightLabel.appendChild(height);
    controls.appendChild(heightLabel);
    const auto = makeControl('button', 'layout-step-button layout-auto-button', '自动高');
    auto.dataset.layoutAction = 'auto-height';
    auto.title = '恢复内容自动高度';
    auto.setAttribute('aria-label', `${info.label}恢复自动高度`);
    controls.appendChild(auto);
    module.insertBefore(controls, module.firstChild);
    const resize = makeControl('button', 'layout-resize-handle', '↘');
    resize.dataset.layoutAction = 'resize';
    resize.hidden = true;
    resize.title = '拖动调整宽高；方向键调整，Shift 加快高度调整';
    resize.setAttribute('aria-label', `调整${info.label}大小；左右键调整宽度，上下键调整高度`);
    module.appendChild(resize);
  }
  function applyPage(page) {
    const grid = document.querySelector(`[data-layout-grid="${page}"]`);
    if (!grid) return;
    pages[page].forEach((item, index) => {
      const module = moduleElement(item.id);
      if (!module) return;
      // Avoid detaching unchanged nodes: a live resize must retain pointer capture and focus.
      if (grid.children[index] !== module) grid.insertBefore(module, grid.children[index] || null);
      module.style.setProperty('--layout-span', String(item.span));
      module.style.setProperty('--layout-medium-span', item.span >= 8 ? '2' : '1');
      module.style.height = item.height ? `${item.height}px` : '';
      module.classList.toggle('has-custom-height', Boolean(item.height));
      module.dataset.layoutSpan = String(item.span);
      module.dataset.layoutHeight = String(item.height);
      const width = module.querySelector('[data-layout-action="width"]');
      const height = module.querySelector('[data-layout-action="height"]');
      if (width) width.value = String(item.span);
      if (height && document.activeElement !== height) height.value = item.height ? String(item.height) : '';
      const previous = module.querySelector('[data-layout-action="previous"]');
      const next = module.querySelector('[data-layout-action="next"]');
      if (previous) previous.disabled = index === 0;
      if (next) next.disabled = index === pages[page].length - 1;
    });
  }
  function applyAllPages() { Object.keys(pages).forEach(applyPage); }
  function changeSize(id, span, height) {
    const info = moduleInfo(id);
    if (!info) return;
    info.item.span = clamp(Math.round(span), 3, 12);
    info.item.height = height === 0 ? 0 : clamp(Math.round(height), 200, 2400);
    applyPage(info.page);
  }
  function commitChange(operation) {
    if (operation && !bridgeLoaded) layoutEdits.push(operation);
    revision += 1;
    saveState();
    requestVisibleCharts();
  }
  function moveModule(id, targetIndex) {
    const info = moduleInfo(id);
    if (!info) return;
    const target = clamp(targetIndex, 0, pages[info.page].length - 1);
    if (target === info.index) return;
    pages[info.page].splice(info.index, 1);
    pages[info.page].splice(target, 0, info.item);
    applyPage(info.page);
    commitChange({ type: 'move', page: info.page, id, before: pages[info.page][target + 1]?.id || null });
    nextFrame(() => moduleElement(id).querySelector('[data-layout-action="drag"]').focus({ preventScroll: true }));
  }
  function setEditing(value) {
    finishInteraction(false);
    editing = Boolean(value);
    revision += 1;
    if (!bridgeLoaded) navigationEdits.add('view');
    document.body.dataset.layoutEditing = String(editing);
    const button = byId('layoutEditButton');
    button.textContent = editing ? '完成布局' : '编辑布局';
    button.setAttribute('aria-pressed', String(editing));
    byId('layoutToolbar').hidden = !editing;
    document.querySelectorAll('.layout-module-controls, .layout-resize-handle').forEach(control => { control.hidden = !editing; });
    renderState(state.view);
    requestVisibleCharts();
  }
  function resetPage() {
    finishInteraction(false);
    pages[state.view] = defaultPages()[state.view];
    applyPage(state.view);
    commitChange({ type: 'reset', page: state.view });
  }
  function updateControl(event) {
    const control = event.target.closest('[data-layout-action]');
    if (!editing || !control) return;
    const module = control.closest('[data-layout-id]');
    const info = module && moduleInfo(module.dataset.layoutId);
    if (!info) return;
    const operation = { type: 'size', page: info.page, id: info.item.id };
    if (control.dataset.layoutAction === 'width') {
      changeSize(info.item.id, Number(control.value), info.item.height);
      operation.span = info.item.span;
    }
    else if (control.dataset.layoutAction === 'height') {
      const raw = control.value.trim();
      const height = raw === '' ? 0 : Number(raw);
      if (!Number.isFinite(height)) { control.value = info.item.height || ''; return; }
      changeSize(info.item.id, info.item.span, height);
      control.value = info.item.height || '';
      operation.height = info.item.height;
    } else return;
    commitChange(operation);
  }
  function layoutKeydown(event) {
    if (!editing) return;
    if (event.key === 'Escape' && !event.target.closest('dialog[open]')) {
      event.preventDefault();
      if (interaction) finishInteraction(false);
      else { setEditing(false); byId('layoutEditButton').focus(); }
      return;
    }
    const control = event.target.closest('[data-layout-action="drag"], [data-layout-action="resize"]');
    if (!control) return;
    const info = moduleInfo(control.closest('[data-layout-id]').dataset.layoutId);
    if (!info) return;
    const key = event.key;
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(key)) return;
    event.preventDefault();
    if (control.dataset.layoutAction === 'drag') {
      const next = key === 'Home' ? 0 : key === 'End' ? pages[info.page].length - 1 : info.index + (key === 'ArrowLeft' || key === 'ArrowUp' ? -1 : 1);
      moveModule(info.item.id, next);
    } else {
      let { span, height } = info.item;
      if (key === 'ArrowLeft') span -= 1;
      if (key === 'ArrowRight') span += 1;
      if (key === 'ArrowUp' || key === 'ArrowDown') height = (height || control.closest('[data-layout-id]').getBoundingClientRect().height) + (key === 'ArrowUp' ? -1 : 1) * (event.shiftKey ? 100 : 20);
      if (key === 'Home') { span = PAGE_MODULES[info.page].find(item => item.id === info.item.id).span; height = 0; }
      if (key === 'End') span = 12;
      changeSize(info.item.id, span, height);
      const operation = { type: 'size', page: info.page, id: info.item.id };
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key)) operation.span = info.item.span;
      if (['ArrowUp', 'ArrowDown', 'Home'].includes(key)) operation.height = info.item.height;
      commitChange(operation);
      control.focus({ preventScroll: true });
    }
  }
  function beginInteraction(event) {
    const control = event.target.closest('[data-layout-action="drag"], [data-layout-action="resize"]');
    if (!editing || !control || (event.button != null && event.button !== 0)) return;
    const module = control.closest('[data-layout-id]');
    const info = moduleInfo(module.dataset.layoutId);
    if (!info) return;
    event.preventDefault();
    control.focus({ preventScroll: true });
    finishInteraction(false);
    const bounds = module.getBoundingClientRect();
    interaction = { action: control.dataset.layoutAction, id: info.item.id, page: info.page, control,
      pointerId: event.pointerId, x: event.clientX, y: event.clientY, bounds,
      original: { ...info.item }, started: false, target: null, after: false, lastResize: 0 };
    if (typeof control.setPointerCapture === 'function') control.setPointerCapture(event.pointerId);
    document.body.classList.add('is-layout-interacting');
  }
  function moveInteraction(event) {
    const active = interaction;
    if (!active || active.pointerId !== event.pointerId) return;
    const dx = event.clientX - active.x;
    const dy = event.clientY - active.y;
    if (!active.started && Math.hypot(dx, dy) < 5) return;
    event.preventDefault();
    active.started = true;
    const module = moduleElement(active.id);
    if (active.action === 'resize') {
      const grid = module.parentElement;
      const gap = parseFloat(window.getComputedStyle(grid).columnGap) || 14;
      const column = (grid.getBoundingClientRect().width + gap) / 12;
      const span = window.innerWidth <= 680 ? active.original.span : active.original.span + Math.round(dx / column);
      changeSize(active.id, span, active.bounds.height + dy);
      // Chart renderers already listen for resize; throttle during live pointer motion.
      if (Date.now() - active.lastResize > 180) { active.lastResize = Date.now(); requestVisibleCharts(); }
      return;
    }
    module.classList.add('is-layout-dragging');
    if (!active.ghost) {
      active.ghost = makeControl('div', 'layout-drag-ghost', `移动 ${moduleInfo(active.id).label}`);
      active.ghost.setAttribute('aria-hidden', 'true');
      document.body.appendChild(active.ghost);
    }
    active.ghost.style.left = `${clamp(event.clientX + 12, 8, window.innerWidth - 180)}px`;
    active.ghost.style.top = `${event.clientY + 12}px`;
    document.querySelectorAll('.layout-drop-target').forEach(target => target.classList.remove('layout-drop-target'));
    const point = document.elementFromPoint(event.clientX, event.clientY);
    const target = point && point.closest('[data-layout-id]');
    active.target = null;
    if (target && target !== module && target.parentElement === module.parentElement) {
      const bounds = target.getBoundingClientRect();
      active.target = target.dataset.layoutId;
      active.after = Math.abs(active.bounds.top - bounds.top) < 30 ? event.clientX > bounds.left + bounds.width / 2 : event.clientY > bounds.top + bounds.height / 2;
      target.dataset.layoutDrop = active.after ? 'after' : 'before';
      target.classList.add('layout-drop-target');
    }
    if (event.clientY < 80) window.scrollBy(0, -18);
    else if (event.clientY > window.innerHeight - 70) window.scrollBy(0, 18);
  }
  function finishInteraction(commit) {
    const active = interaction;
    if (!active) return;
    interaction = null;
    const module = moduleElement(active.id);
    if (module) module.classList.remove('is-layout-dragging');
    if (active.ghost) active.ghost.remove();
    document.querySelectorAll('.layout-drop-target').forEach(target => target.classList.remove('layout-drop-target'));
    document.body.classList.remove('is-layout-interacting');
    try { active.control.releasePointerCapture(active.pointerId); } catch (_) { /* A cancelled pointer may already be released. */ }
    if (active.action === 'resize') {
      if (!commit || !active.started) changeSize(active.id, active.original.span, active.original.height);
      else {
        const item = moduleInfo(active.id).item;
        const operation = { type: 'size', page: active.page, id: active.id };
        if (item.span !== active.original.span) operation.span = item.span;
        if (item.height !== active.original.height) operation.height = item.height;
        commitChange(operation);
      }
    } else if (commit && active.started && active.target) {
      const items = pages[active.page];
      const from = items.findIndex(item => item.id === active.id);
      const current = items.splice(from, 1)[0];
      const target = items.findIndex(item => item.id === active.target);
      items.splice(target + (active.after ? 1 : 0), 0, current);
      applyPage(active.page);
      const position = items.findIndex(item => item.id === active.id);
      commitChange({ type: 'move', page: active.page, id: active.id, before: items[position + 1]?.id || null });
    }
    if (pendingNativeLoad) {
      const pending = pendingNativeLoad;
      pendingNativeLoad = null;
      completeNativeLoad(pending.saved, pending.loadedSuccessfully, pending.loadRevision);
    }
    requestVisibleCharts();
  }
  function bindNavigation() {
    document.addEventListener('click', event => {
      const tab = event.target.closest('[role="tab"]');
      if (tab && tablistFor(tab)) { activateTab(tab); return; }
      if (event.target.closest('#layoutEditButton')) { setEditing(!editing); return; }
      if (event.target.closest('#layoutResetButton')) { resetPage(); return; }
      const control = event.target.closest('[data-layout-action]');
      if (editing && control) {
        const info = moduleInfo(control.closest('[data-layout-id]').dataset.layoutId);
        if (!info) return;
        if (control.dataset.layoutAction === 'previous') moveModule(info.item.id, info.index - 1);
        else if (control.dataset.layoutAction === 'next') moveModule(info.item.id, info.index + 1);
        else if (control.dataset.layoutAction === 'auto-height') {
          changeSize(info.item.id, info.item.span, 0);
          commitChange({ type: 'size', page: info.page, id: info.item.id, height: 0 });
        }
        return;
      }
      if (!event.target.closest('a[href="#detailTitle"]')) return;
      event.preventDefault();
      showView('usage', 'daily');
      nextFrame(() => { const detail = byId('detailTitle'); if (detail) detail.scrollIntoView({ behavior: 'smooth', block: 'start' }); });
    });
    document.addEventListener('keydown', moveTabFocus);
    document.addEventListener('keydown', layoutKeydown);
    document.addEventListener('change', updateControl);
    document.addEventListener('pointerdown', beginInteraction);
    document.addEventListener('pointermove', moveInteraction, { passive: false });
    document.addEventListener('pointerup', event => { if (interaction && interaction.pointerId === event.pointerId) finishInteraction(true); });
    document.addEventListener('pointercancel', () => finishInteraction(false));
    window.addEventListener('blur', () => finishInteraction(false));
  }

  readSavedState();
  window.CodexDashboardLayout = Object.freeze({
    showView,
    getState: () => ({ ...state }),
    getLayout: snapshot,
    isEditing: () => editing
  });
  window.addEventListener('pywebviewready', connectBridge);
  window.addEventListener('focus', connectBridge);
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-layout-id]').forEach(module => {
      const info = moduleInfo(module.dataset.layoutId);
      if (info) addModuleControls(module, info);
    });
    bindNavigation();
    applyAllPages();
    initialized = true;
    renderState(pendingSelection || state.view !== 'overview' ? null : state.view);
    pendingSelection = false;
    if (window.location.hash === '#detailTitle') showView('usage', 'daily');
    connectBridge();
    announce(statusMessage);
  }, { once: true });
})();
