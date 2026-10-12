(() => {
  'use strict';

  const STORAGE_KEY = 'codex-token-dashboard-layout-v1';
  const viewNames = new Set(['overview', 'trends', 'usage', 'pricing']);
  const viewTabs = new Map([
    ['overview', 'viewTabOverview'],
    ['trends', 'viewTabTrends'],
    ['usage', 'viewTabUsage'],
    ['pricing', 'viewTabPricing']
  ]);
  const subviewTabs = {
    usage: { daily: 'usageTabDaily', requests: 'usageTabRequests', tasks: 'usageTabTasks' },
    pricing: { rules: 'pricingTabRules', diagnostics: 'pricingTabDiagnostics' }
  };
  const state = { view: 'overview', usage: 'daily', pricing: 'rules' };
  let initialized = false;
  let pendingSelection = false;
  let resizeQueued = false;

  function nextFrame(callback) {
    if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(callback);
    else window.setTimeout(callback, 0);
  }

  function readSavedState() {
    try {
      const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || 'null');
      if (!saved || typeof saved !== 'object') return;
      if (viewNames.has(saved.view)) state.view = saved.view;
      if (Object.prototype.hasOwnProperty.call(subviewTabs.usage, saved.usage)) state.usage = saved.usage;
      if (Object.prototype.hasOwnProperty.call(subviewTabs.pricing, saved.pricing)) state.pricing = saved.pricing;
    } catch (_) {
      // Layout preferences are optional; storage can be disabled or contain an old value.
    }
  }

  function saveState() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (_) {
      // The selected view still works when local storage is unavailable.
    }
  }

  function setTabState(tab, selected) {
    if (!tab) return;
    tab.classList.toggle('is-selected', selected);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }

  function renderState(previousView) {
    document.querySelectorAll('[data-view-panel]').forEach(panel => {
      panel.hidden = panel.dataset.viewPanel !== state.view;
    });
    viewTabs.forEach((id, view) => setTabState(document.getElementById(id), view === state.view));

    Object.entries(subviewTabs).forEach(([view, tabs]) => {
      const activeSubview = state[view];
      document.querySelectorAll(`[data-subview-panel]`).forEach(panel => {
        const parentView = panel.closest('[data-view-panel]');
        if (parentView && parentView.dataset.viewPanel === view) panel.hidden = panel.dataset.subviewPanel !== activeSubview;
      });
      Object.entries(tabs).forEach(([subview, id]) => setTabState(document.getElementById(id), subview === activeSubview));
    });

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
    const previousView = state.view;
    state.view = view;
    if (view === 'usage' && subview && Object.prototype.hasOwnProperty.call(subviewTabs.usage, subview)) state.usage = subview;
    if (view === 'pricing' && subview && Object.prototype.hasOwnProperty.call(subviewTabs.pricing, subview)) state.pricing = subview;
    pendingSelection = true;
    if (initialized) {
      renderState(previousView);
      pendingSelection = false;
    }
    return true;
  }

  function tablistFor(tab) {
    return tab.closest('[role="tablist"]');
  }

  function activateTab(tab) {
    const view = tab.dataset.view;
    if (view) return showView(view);
    const subview = tab.dataset.subview;
    if (!subview) return false;
    const section = tab.closest('#view-usage, #view-pricing');
    if (!section) return false;
    return section.id === 'view-usage' ? showView('usage', subview) : showView('pricing', subview);
  }

  function moveTabFocus(event) {
    const tab = event.target.closest('[role="tab"]');
    if (!tab) return;
    const list = tablistFor(tab);
    if (!list) return;
    const tabs = Array.from(list.querySelectorAll('[role="tab"]')).filter(item => !item.disabled);
    if (!tabs.length) return;
    const index = tabs.indexOf(tab);
    let nextIndex;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = tabs.length - 1;
    else return;
    event.preventDefault();
    const nextTab = tabs[nextIndex];
    nextTab.focus();
    activateTab(nextTab);
  }

  function bindNavigation() {
    document.addEventListener('click', event => {
      const tab = event.target.closest('[role="tab"]');
      if (tab && tablistFor(tab)) {
        activateTab(tab);
        return;
      }
      const detailLink = event.target.closest('a[href="#detailTitle"]');
      if (!detailLink) return;
      event.preventDefault();
      showView('usage', 'daily');
      nextFrame(() => {
        const detail = document.getElementById('detailTitle');
        if (detail) detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
    document.addEventListener('keydown', moveTabFocus);
  }

  readSavedState();
  window.CodexDashboardLayout = Object.freeze({
    showView,
    getState: () => ({ view: state.view, usage: state.usage, pricing: state.pricing })
  });
  document.addEventListener('DOMContentLoaded', () => {
    bindNavigation();
    initialized = true;
    renderState(pendingSelection || state.view !== 'overview' ? null : state.view);
    pendingSelection = false;
    if (window.location.hash === '#detailTitle') showView('usage', 'daily');
  }, { once: true });
})();
