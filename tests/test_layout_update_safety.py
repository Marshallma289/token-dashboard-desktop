"""Behavioral regression tests for preserving customized layouts across updates."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop import DesktopBridge


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "web" / "layout.js"


class LayoutUpdateSafetyTests(unittest.TestCase):
    def run_node(self, source):
        result = subprocess.run(
            ["node", "-e", source, str(MODULE)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def test_new_module_is_appended_without_changing_old_order_or_dimensions(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const {defaultPages, normalizeLayout} = require(process.argv[1]);
const oldPages = defaultPages();
oldPages.usage = oldPages.usage.filter(item => item.id !== 'usage-sessions');
for (const [page, items] of Object.entries(oldPages)) {
  items.reverse();
  items.forEach((item, index) => {
    item.span = 3 + (index % 10);
    item.height = 300 + index * 40;
  });
}
const oldLayout = {version: 1, view: 'usage', usage: 'tasks', pricing: 'diagnostics', pages: oldPages};
const restored = normalizeLayout(oldLayout);
assert.ok(restored, 'a supported v1 layout remains readable after a module is added');
for (const [page, previous] of Object.entries(oldPages)) {
  const expectedOldItems = previous.map(({id, span, height}) => ({id, span, height}));
  assert.deepEqual(restored.pages[page].slice(0, previous.length), expectedOldItems,
    `${page}: every old module keeps its customized order, width, and height`);
  const appended = restored.pages[page].slice(previous.length);
  if (page === 'usage') {
    assert.deepEqual(appended, [{id: 'usage-sessions', span: 12, height: 0}],
      'the only newly introduced module is appended with its default size');
  } else {
    assert.deepEqual(appended, [], `${page}: no unexpected modules are added`);
  }
}
assert.equal(restored.view, 'usage');
assert.equal(restored.usage, 'tasks');
assert.equal(restored.pricing, 'diagnostics');
""")

    def test_failed_load_keeps_edits_and_focus_retry_merges_only_edited_width(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {defaultPages} = require(process.argv[1]);

function createNode(tag = 'div') {
  const element = {
    tagName: tag.toUpperCase(), id: '', className: '', dataset: {}, style: {setProperty() {}},
    attributes: {}, children: [], parentElement: null, hidden: false, value: '', textContent: '',
    classList: {toggle() {}, add() {}, remove() {}},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    appendChild(child) { return this.insertBefore(child, null); },
    insertBefore(child, before) {
      if (child.parentElement) {
        const oldIndex = child.parentElement.children.indexOf(child);
        if (oldIndex >= 0) child.parentElement.children.splice(oldIndex, 1);
      }
      const index = before ? this.children.indexOf(before) : -1;
      this.children.splice(index < 0 ? this.children.length : index, 0, child);
      child.parentElement = this;
      return child;
    },
    get firstChild() { return this.children[0] || null; },
    querySelector(selector) {
      for (const child of this.children) {
        if (matches(child, selector)) return child;
        const nested = child.querySelector(selector);
        if (nested) return nested;
      }
      return null;
    },
    closest(selector) {
      for (let current = this; current; current = current.parentElement) {
        if (selector.split(',').some(part => matches(current, part.trim()))) return current;
      }
      return null;
    },
    focus() {},
    getBoundingClientRect() { return {height: 400, width: 800, top: 0, left: 0}; },
    remove() {
      if (!this.parentElement) return;
      const index = this.parentElement.children.indexOf(this);
      if (index >= 0) this.parentElement.children.splice(index, 1);
      this.parentElement = null;
    }
  };
  return element;
}
function matches(element, selector) {
  if (selector === '[data-layout-id]') return Boolean(element.dataset.layoutId);
  if (selector === '[data-layout-action]') return Boolean(element.dataset.layoutAction);
  const action = selector.match(/^\[data-layout-action="([^"]+)"\]$/);
  if (action) return element.dataset.layoutAction === action[1];
  const id = selector.match(/^#([\w-]+)$/);
  if (id) return element.id === id[1];
  return false;
}
function createEnvironment(loadLayout) {
  const documentEvents = new Map(), windowEvents = new Map(), storage = new Map(), writes = [];
  const module = createNode('section');
  module.dataset.layoutId = 'overview-cache';
  const grid = createNode('div');
  grid.dataset.layoutGrid = 'overview';
  grid.appendChild(module);
  const knownNodes = new Map();
  const byId = id => {
    if (!knownNodes.has(id)) {
      const item = createNode();
      item.id = id;
      knownNodes.set(id, item);
    }
    return knownNodes.get(id);
  };
  const body = createNode('body');
  body.dataset = {};
  const document = {
    body,
    activeElement: null,
    getElementById: byId,
    createElement: tag => createNode(tag),
    querySelector(selector) {
      if (selector === '[data-layout-grid="overview"]') return grid;
      const panel = selector.match(/^\[data-layout-id="([^"]+)"\]$/);
      if (panel && panel[1] === module.dataset.layoutId) return module;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[data-layout-id]') return [module];
      if (selector === '.layout-module-controls, .layout-resize-handle') {
        return module.children.flatMap(child => child.className === 'layout-module-controls' ? [child, ...child.children.filter(item => item.dataset.layoutAction === 'resize')] : []);
      }
      return [];
    },
    addEventListener(name, callback) { documentEvents.set(name, callback); }
  };
  const window = {
    localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
    location: {hash: ''},
    addEventListener(name, callback) { windowEvents.set(name, callback); },
    dispatchEvent() {},
    requestAnimationFrame: callback => callback(),
    pywebview: {api: {load_layout: loadLayout, save_layout: async value => {
      writes.push(JSON.parse(JSON.stringify(value)));
      return {ok: true};
    }}}
  };
  vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window, document, Event: class {}, console, Set, Map, Promise});
  documentEvents.get('DOMContentLoaded')();
  const dispatch = (name, target) => documentEvents.get(name)({target, preventDefault() {}});
  return {window, module, documentEvents, windowEvents, writes, dispatch};
}
const flush = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };

(async () => {
  let resolveSuccessfulLoad;
  let loadCalls = 0;
  const env = createEnvironment(() => {
    loadCalls += 1;
    if (loadCalls < 5) return Promise.reject(new Error('native preferences unavailable'));
    return new Promise(resolve => { resolveSuccessfulLoad = resolve; });
  });
  await flush();
  assert.equal(loadCalls, 1, 'initial native load was attempted');

  env.window.CodexDashboardLayout.showView('trends');
  await flush();
  assert.equal(loadCalls, 2, 'navigation retries native loading');
  assert.equal(env.writes.length, 0, 'a failed read never writes the default layout');

  const clickTarget = {closest: selector => selector === '#layoutEditButton' ? {id: 'layoutEditButton'} : null};
  env.dispatch('click', clickTarget);
  await flush();
  assert.equal(loadCalls, 3, 'editing retries native loading while preserving browser state');
  assert.equal(env.writes.length, 0);

  const width = env.module.querySelector('[data-layout-action="width"]');
  assert.ok(width, 'the module has a real layout width control');
  width.value = '9';
  env.dispatch('change', width);
  await flush();
  assert.equal(loadCalls, 4, 'a width change retries the native read');
  assert.equal(env.writes.length, 0, 'the width edit remains local until a read succeeds');
  assert.equal(env.window.CodexDashboardLayout.getLayout().pages.overview.find(item => item.id === 'overview-cache').span, 9);

  const saved = {version: 1, view: 'pricing', usage: 'tasks', pricing: 'diagnostics', pages: defaultPages()};
  const cache = saved.pages.overview.find(item => item.id === 'overview-cache');
  cache.span = 6;
  cache.height = 840;
  saved.pages.overview.reverse();
  saved.pages.trends.reverse();
  Object.assign(saved.pages.trends[0], {span: 8, height: 560});
  env.windowEvents.get('focus')();
  assert.equal(loadCalls, 5, 'window focus retries native loading after repeated read failures');
  assert.equal(env.writes.length, 0, 'no native write starts while the successful retry is pending');
  resolveSuccessfulLoad({ok: true, layout: saved});
  await flush();

  assert.equal(env.writes.length, 1, 'the merged layout is saved after the read succeeds');
  const written = env.writes[0];
  assert.equal(written.view, 'trends');
  assert.equal(written.usage, 'tasks', 'an untouched subview keeps the native preference');
  assert.equal(written.pricing, 'diagnostics');
  assert.deepEqual(written.pages.overview.map(item => item.id), saved.pages.overview.map(item => item.id),
    'saved order survives while width changes');
  assert.deepEqual(written.pages.overview.find(item => item.id === 'overview-cache'),
    {id: 'overview-cache', span: 9, height: 840}, 'width edit merges without erasing saved height');
  assert.deepEqual(written.pages.trends, saved.pages.trends, 'untouched modules retain native order, width, and height');
  assert.deepEqual(written.pages.usage, saved.pages.usage);
  assert.deepEqual(written.pages.pricing, saved.pages.pricing);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_unknown_nonempty_native_layout_is_never_replaced_by_defaults(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const events = new Map(), writes = [], storage = new Map();
let loadCalls = 0;
const node = id => ({id, dataset: {}, classList: {toggle() {}, add() {}, remove() {}}, setAttribute() {}, addEventListener() {}, focus() {}});
const document = {body: {dataset: {}, classList: {add() {}, remove() {}}}, getElementById: node,
  querySelector: () => null, querySelectorAll: () => [], createElement: () => node(''),
  addEventListener: (name, callback) => events.set(name, callback)};
const window = {localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
  location: {hash: ''}, addEventListener() {}, dispatchEvent() {}, requestAnimationFrame: callback => callback(),
  pywebview: {api: {load_layout: async () => { loadCalls += 1; return {ok: true, layout: {version: 77, pages: {overview: []}, userData: 'preserve-me'}}; },
    save_layout: async value => { writes.push(value); return {ok: true}; }}}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window, document, Event: class {}, console, Set, Map, Promise});
events.get('DOMContentLoaded')();
(async () => {
  for (let i = 0; i < 24; i++) await Promise.resolve();
  assert.equal(loadCalls, 1);
  assert.equal(writes.length, 0);
  window.CodexDashboardLayout.showView('trends');
  for (let i = 0; i < 24; i++) await Promise.resolve();
  assert.ok(loadCalls >= 2, 'later local edits retry reading the unrecognized saved value');
  assert.equal(writes.length, 0, 'an unrecognized non-empty layout is never overwritten with defaults');
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_bridge_rejects_invalid_disk_layout_without_changing_original_bytes(self):
        original = b'{\n  "theme" : "dark",\n  "layout" : { "version" : 77, "pages" : { "overview" : [] } }\n}\n'
        with tempfile.TemporaryDirectory() as temporary, patch("desktop.local_data_dir", return_value=Path(temporary)):
            preferences = Path(temporary) / "preferences.json"
            preferences.write_bytes(original)
            result = DesktopBridge().load_layout()
            self.assertFalse(result["ok"], result)
            self.assertEqual(preferences.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
