"""Regression checks for saved layouts, migration safety, and native save ordering."""

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "web" / "layout.js"


class LayoutHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.modules = {}
        self.ids = set()
        self.errors = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            if attrs["id"] in self.ids:
                self.errors.append("duplicate DOM id: " + attrs["id"])
            self.ids.add(attrs["id"])
        module_id = attrs.get("data-layout-id")
        if module_id:
            grid = next((item[1]["data-layout-grid"] for item in reversed(self.stack) if "data-layout-grid" in item[1]), None)
            self.modules[module_id] = grid
        # HTML permits omitted option end tags (the cleanup datalist uses them).
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr", "path", "circle", "option"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        elif tag not in {"path", "circle", "option"}:
            self.errors.append("unbalanced closing element: " + tag)


class LayoutRegressionTests(unittest.TestCase):
    def run_node(self, source):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for frontend layout logic tests")
        result = subprocess.run([node, "-e", source, str(MODULE)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def test_bad_preferences_cannot_hide_or_duplicate_modules(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const { normalizeLayout, defaultPages } = require(process.argv[1]);
for (const invalid of [null, false, [], {}, {version: 99, pages: {}}, {version: 1, pages: []}]) {
  assert.equal(normalizeLayout(invalid), null);
}
const normalized = normalizeLayout({version: 1, view: '__proto__', usage: '__proto__', pages: {
  overview: [
    {id: 'unknown', span: 12, height: 500},
    {id: 'overview-cache', span: 7, height: 640},
    {id: 'overview-cache', span: 3, height: 800},
    {id: 'overview-trend', span: '6', height: 99999999},
    {id: 'overview-kpis', span: -1, height: -1},
    {id: 'overview-models', span: 6.5, height: 200.5}
  ],
  trends: [{id: 'trends-activity', span: Infinity, height: NaN}],
  usage: 'damaged', pricing: null
}});
assert.equal(normalized.view, 'overview');
assert.equal(normalized.usage, 'daily');
assert.deepEqual(normalized.pages.overview[0], {id: 'overview-cache', span: 7, height: 640});
assert.deepEqual(normalized.pages.overview[1], {id: 'overview-trend', span: 12, height: 0});
for (const [page, items] of Object.entries(defaultPages())) {
  assert.deepEqual(new Set(normalized.pages[page].map(item => item.id)), new Set(items.map(item => item.id)));
  assert.equal(normalized.pages[page].length, items.length);
  for (const item of normalized.pages[page]) {
    assert(item.span >= 3 && item.span <= 12 && Number.isInteger(item.span));
    assert(item.height === 0 || item.height >= 200 && item.height <= 2400);
  }
}
""")

    def test_round_trip_retains_order_size_and_all_modules_views(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const {normalizeLayout, defaultPages} = require(process.argv[1]);
const value = {version: 1, view: 'usage', usage: 'all', pricing: 'all', pages: defaultPages()};
value.pages.overview.reverse();
value.pages.overview[0].span = 8;
value.pages.overview[0].height = 780;
value.pages.usage.reverse();
const saved = JSON.parse(JSON.stringify(value));
const restored = normalizeLayout(saved);
assert.deepEqual(restored, value);
restored.pages.overview[0].height = 900;
assert.equal(saved.pages.overview[0].height, 780, 'normalization must not mutate stored preferences');
""")

    def test_late_native_load_keeps_new_navigation_and_saved_modules(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
(async () => {
  const events = new Map(), storage = new Map(), writes = [], nodes = new Map();
  let resolveNative;
  const native = new Promise(resolve => { resolveNative = resolve; });
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {classList:{toggle(){},add(){},remove(){}}, dataset:{},
      setAttribute(){}, textContent:'', addEventListener(){}});
    return nodes.get(id);
  };
  const document = {getElementById:node,querySelector:()=>null,querySelectorAll:()=>[],
    body:{dataset:{},classList:{add(){},remove(){}}},
    addEventListener:(name,callback)=>events.set(name,callback)};
  const window = {localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    location:{hash:''},addEventListener(){},dispatchEvent(){},requestAnimationFrame:callback=>callback(),
    pywebview:{api:{load_layout:()=>native,save_layout:async value=>{
      writes.push(JSON.parse(JSON.stringify(value))); return {ok:true};
    }}}};
  vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'), {window,document,Event:class{},console,Set,Map,Promise});
  events.get('DOMContentLoaded')();
  window.CodexDashboardLayout.showView('usage','requests');
  resolveNative({ok:true,layout:{version:1,view:'pricing',usage:'daily',pricing:'diagnostics',pages:{
    overview:[{id:'overview-cache',span:7,height:640}],
    trends:[{id:'trends-models',span:8,height:800}]
  }}});
  for (let i=0; i<20; i++) await Promise.resolve();
  const saved = writes.at(-1);
  assert.equal(saved.view, 'usage');
  assert.equal(saved.usage, 'requests');
  assert.equal(saved.pricing, 'diagnostics', 'untouched subview preference must still load');
  assert.deepEqual(saved.pages.overview[0], {id:'overview-cache',span:7,height:640});
  assert.deepEqual(saved.pages.trends[0], {id:'trends-models',span:8,height:800});
  assert.deepEqual(JSON.parse(JSON.stringify(window.CodexDashboardLayout.getLayout())), saved);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_native_merge_replays_only_edited_dimensions_and_order(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const {defaultPages, mergeLoadedLayout} = require(process.argv[1]);
const saved = {version:1,view:'pricing',usage:'daily',pricing:'diagnostics',pages:defaultPages()};
const cache = saved.pages.overview.splice(3,1)[0];
Object.assign(cache, {span:7,height:640});
saved.pages.overview.unshift(cache);
Object.assign(saved.pages.trends[0], {span:9,height:500});
saved.pages.usage.reverse();
saved.pages.usage[0].height = 880;
const local = {version:1,view:'overview',usage:'requests',pricing:'rules',pages:defaultPages()};
const result = mergeLoadedLayout(saved,local,{navigation:['view','usage'],operations:[
  {type:'size',page:'overview',id:'overview-cache',span:9},
  {type:'move',page:'overview',id:'overview-ttft',before:'overview-trend'}
]});
assert.equal(result.view, 'overview');
assert.equal(result.usage, 'requests');
assert.equal(result.pricing, 'diagnostics');
assert.deepEqual(result.pages.overview[0], {id:'overview-cache',span:9,height:640}, 'width edit must not erase saved height');
const overviewIds = result.pages.overview.map(item=>item.id);
assert.equal(overviewIds.indexOf('overview-ttft')+1, overviewIds.indexOf('overview-trend'));
assert.deepEqual(result.pages.trends, saved.pages.trends, 'another page must retain all native sizes');
assert.deepEqual(result.pages.usage, saved.pages.usage, 'another page must retain native order');
assert.deepEqual(saved.pages.overview[0], {id:'overview-cache',span:7,height:640}, 'merge must not mutate the loaded snapshot');
const reset = mergeLoadedLayout(saved,local,{navigation:[],operations:[
  {type:'reset',page:'overview'},
  {type:'size',page:'overview',id:'overview-trend',height:600}
]});
assert.deepEqual(reset.pages.overview.map(item=>item.id), defaultPages().overview.map(item=>item.id));
assert.deepEqual(reset.pages.overview.find(item=>item.id==='overview-cache'), {id:'overview-cache',span:4,height:0});
assert.equal(reset.pages.overview.find(item=>item.id==='overview-trend').height, 600);
assert.deepEqual(reset.pages.trends, saved.pages.trends);
assert.deepEqual(reset.pages.usage, saved.pages.usage);
""")

    def test_native_saves_are_serial_and_keep_the_latest_queued_layout(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const {createSerialSaver} = require(process.argv[1]);
(async () => {
  const started = [], completed = [], resolvers = [], results = [];
  let inFlight = 0;
  const saver = createSerialSaver(async value => {
    inFlight += 1;
    assert.equal(inFlight, 1, 'native saves must never overlap');
    started.push(value);
    await new Promise(resolve => resolvers.push(resolve));
    completed.push(value);
    inFlight -= 1;
    return {ok:true};
  }, ok => results.push(ok));
  saver.enqueue({size: 300});
  saver.enqueue({size: 400});
  saver.enqueue({size: 500});
  assert.deepEqual(started, [{size:300}]);
  resolvers.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(started, [{size:300}, {size:500}]);
  resolvers.shift()();
  await saver.whenIdle();
  assert.deepEqual(completed, [{size:300}, {size:500}]);
  assert.deepEqual(results, [true, true]);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_native_save_failure_does_not_block_later_edits(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const {createSerialSaver} = require(process.argv[1]);
(async () => {
  let calls = 0;
  const results = [];
  const saver = createSerialSaver(async value => {
    calls += 1;
    if (calls === 1) throw new Error('temporary failure');
    return {ok: true};
  }, ok => results.push(ok));
  await saver.enqueue({size:300});
  await saver.enqueue({size:600});
  assert.equal(calls, 2);
  assert.deepEqual(results, [false, true]);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_every_module_is_present_once_in_its_own_page_grid(self):
        expected = json.loads(self.run_node("console.log(JSON.stringify(require(process.argv[1]).defaultPages()))"))
        parser = LayoutHTML()
        parser.feed((ROOT / "web" / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(parser.errors, [])
        self.assertEqual(parser.stack, [])
        self.assertEqual(parser.modules, {item["id"]: page for page, items in expected.items() for item in items})
        for original_id in ["kpiGrid", "comparisonMetrics", "analyticsTrendChart", "cacheMetrics", "ttftMetrics", "modelTotals", "workspaceChart", "dailyTrendChart", "hourlyHeatmap", "requestChart", "modelUsageChart", "dailyUsageTable", "requestsTable", "tasksTable", "pricingTable", "diagnosticsPanel"]:
            self.assertIn(original_id, parser.ids)


if __name__ == "__main__":
    unittest.main()
