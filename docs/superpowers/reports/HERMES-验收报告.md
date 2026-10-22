> 归档说明：这是 Hermes 数据源接入阶段的历史验收记录（238 项），后续价格规则补全阶段的 249 项结果见 [pricing-validation.md](pricing-validation.md)。命令中的个人路径已替换为占位符。`work/` 中的原始数据库、日志、脚本和截图仅在本地保留，不随仓库分发；下文的本地路径不是仓库交付物。原文关于未提交、换行差异和未补价格的说明均对应当时阶段。

# Hermes 功能验收报告

功能代码、独立测试、README 已完成。未做 git commit，未改既有夹具。原有 tests/test_layout.py 最后一行换行差异保持原状。

已实现：独立只读 WAL 读取器、路径与 profiles 发现、旧库兼容升级、source_kind 筛选、api_call_count 完整加权、增量累计快照替换、费用参考估算与有效自报回退、请求级能力边界、HTTP/CLI/桌面参数、前端筛选与明细语义。

增量水位 MAX(last_seen)+COUNT 之外增加 DB/WAL size+mtime 签名，避免旧会话更新被全局最大时间掩盖。源缓存读取量超过 input 时保留源数值并显示比例未知。

## 最终全量测试

真实命令：`C:/path/to/user\AppData\Roaming\uv\python\cpython-3.14.7-windows-x86_64-none\python.exe -m unittest discover -s tests`

238 tests（原205项，加本次新增执行项），6 failures + 2 errors；类型和完整测试 ID 与批准的基线逐项一致。原命令退出码1来自这8项既有失败。

```text
Ran 238 tests in 37.034s
FAILED (failures=6, errors=2, skipped=1)
baseline_exact_match: true
```

精确失败列表：

```text
ERROR: test_followup_requirements.FollowupRequirementTests.test_timezone_converts_utc_boundary_to_local_date
ERROR: test_review_regressions.ReviewRegressionTests.test_timezone_change_invalidates_unchanged_file_cache
FAIL: test_backend.BackendTests.test_five_aggregates_and_percentiles
FAIL: test_backend.BackendTests.test_incremental_file_change_rebuilds_only_changed_file
FAIL: test_backend.BackendTests.test_scan_provider_switch_model_switch_dedup_and_legacy
FAIL: test_review_regressions.ReviewRegressionTests.test_duplicate_legacy_snapshot_is_counted_once
FAIL: test_review_regressions.ReviewRegressionTests.test_later_usage_correction_replaces_earlier_response
FAIL: test_review_regressions.ReviewRegressionTests.test_session_thread_state_prevents_cross_thread_model_leak
```

## 生产库只读扫描

真实扫描读入33行，加权1257次调用。规格的1196是预估；对当前只读快照独立执行 SUM(api_call_count) 的结果为1257，与导入结果一致。原始任务分组见 work/hermes-raw-counts.txt。

```text
COMMAND: C:/path/to/user\AppData\Roaming\uv\python\cpython-3.14.7-windows-x86_64-none\python.exe backend.py scan --db D:/path/to/token-dashboard-hermes\work\hermes-live-final.sqlite3 --root D:/path/to/token-dashboard-hermes\work\empty-codex --hermes-db D:/path/to/Hermes\state.db

{

  "files_removed": 0,

  "files_scanned": 1,

  "files_seen": 1,

  "files_unchanged": 0,

  "hermes_rows_read": 33,

  "hermes_unavailable": 0,

  "incomplete_files": 0,

  "parse_errors": 0,

  "records_added": 33,

  "records_skipped": 0,

  "unavailable_roots": 0

}

{

  "aggregate_rows": 33,

  "weighted_request_count": 1257,

  "total_tokens": 4873185,

  "request_size_samples": 0,

  "source_unchanged": true,

  "before": {

    "state.db": {

      "exists": true,

      "mtime_ns": 1792649612396904800,

      "size": 22810624,

      "sha256": "047ba2d7dd121923958f688e5c856027420e05256a24e93daef92ba8ae21f74f"

    },

    "state.db-wal": {

      "exists": true,

      "mtime_ns": 1792651085792440600,

      "size": 4243632,

      "sha256": "885785d9c39c13f94c2b22bbcf4d247086b63f4585f393c31d6947fa6e103a25"

    },

    "state.db-shm": {

      "exists": true,

      "mtime_ns": 1792598503689193100,

      "size": 32768,

      "sha256": "89a18223395a27e1c74077599b8b8e73eb32f06a8cbc4bdbed8435b274b4b2c1"

    }

  },

  "after": {

    "state.db": {

      "exists": true,

      "mtime_ns": 1792649612396904800,

      "size": 22810624,

      "sha256": "047ba2d7dd121923958f688e5c856027420e05256a24e93daef92ba8ae21f74f"

    },

    "state.db-wal": {

      "exists": true,

      "mtime_ns": 1792651085792440600,

      "size": 4243632,

      "sha256": "885785d9c39c13f94c2b22bbcf4d247086b63f4585f393c31d6947fa6e103a25"

    },

    "state.db-shm": {

      "exists": true,

      "mtime_ns": 1792598503689193100,

      "size": 32768,

      "sha256": "89a18223395a27e1c74077599b8b8e73eb32f06a8cbc4bdbed8435b274b4b2c1"

    }

  }

}


```

## 真实前后端联动

在仓库目录执行以下已实际运行的命令；它启动本地HTTP服务和真实headless Chromium，切换数据源并断言DOM、API、聚合调用数、样本范围及浏览器异常：

```powershell
& 'C:/path/to/user/AppData/Roaming/uv/python/cpython-3.14.7-windows-x86_64-none/python.exe' work/verify_frontend.py
```

```json
{
  "frontend_backend_link": "PASS",
  "source_filter": "Hermes → Codex → 全部",
  "weighted_request_count": 1257,
  "request_size_samples": 0,
  "page_summary": "共 33 条记录 · 1,257 次调用 · 第 1 / 2 页",
  "browser_errors": 0,
  "screenshots": [
    "work/hermes-overview.png",
    "work/hermes-requests.png"
  ]
}

```

已检查1440px总览和980px明细截图：work/hermes-overview.png、work/hermes-requests.png。

## TDD、审计与独立复核

逐任务真实RED见 HERMES-TDD-RED.md；各GREEN日志见work/*green.txt；逐个计数位置审计见work/hermes-count-audit.md。

独立审查发现的水位漏更新、冻结价格说明重复追加、模型汇总丢失sourceKind三项均已修复，并经独立复核通过。追加审计用例覆盖调用数单独更新时使重计价预览失效、原始空task_id和正确session first_seen。

未扩展模型参考价表：实际 deepseek-v4.1-flash 未命中现有参考价且自报状态unknown时保持未计价。未生成新的桌面安装包；本任务验收为源码、测试、真实本地HTTP/浏览器运行。

## 全量测试原始完整输出

```text
..........F.FF...Archive: C:/path/to/user\AppData\Local\Temp\tmpf94jz9bm\release\CodexTokenDashboard-macOS-arm64-1.2.0-20261022-064352-hnzkm5ij.zip

Manifest: C:/path/to/user\AppData\Local\Temp\tmpf94jz9bm\release\CodexTokenDashboard-macOS-arm64-1.2.0-20261022-064352-hnzkm5ij.build-manifest.json

Source digest: e8896b7ede38bada341bea23002f5ca6932066ca42b3ee27679ded74e361a580

....Archive: C:/path/to/user\AppData\Local\Temp\tmp_b2o423o\release\CodexTokenDashboard-Windows-Portable-x64-1.2.0-20261022-064353-jfhyqdec.zip

Manifest: C:/path/to/user\AppData\Local\Temp\tmp_b2o423o\release\CodexTokenDashboard-Windows-Portable-x64-1.2.0-20261022-064353-jfhyqdec.build-manifest.json

Source digest: e8896b7ede38bada341bea23002f5ca6932066ca42b3ee27679ded74e361a580

.............C:/path/to/user\AppData\Roaming\uv\python\cpython-3.14.7-windows-x86_64-none\Lib\tempfile.py:484: ResourceWarning: Implicitly cleaning up <HTTPError 403: 'Forbidden'>

  _warnings.warn(self.warn_message, ResourceWarning)

C:/path/to/user\AppData\Roaming\uv\python\cpython-3.14.7-windows-x86_64-none\Lib\tempfile.py:484: ResourceWarning: Implicitly cleaning up <HTTPError 400: 'Bad Request'>

  _warnings.warn(self.warn_message, ResourceWarning)

..C:/path/to/user\AppData\Roaming\uv\python\cpython-3.14.7-windows-x86_64-none\Lib\tempfile.py:484: ResourceWarning: Implicitly cleaning up <HTTPError 409: 'Conflict'>

  _warnings.warn(self.warn_message, ResourceWarning)

..........E...........................................................................................................................FFFE...............................s................................

======================================================================

ERROR: test_timezone_converts_utc_boundary_to_local_date (test_followup_requirements.FollowupRequirementTests.test_timezone_converts_utc_boundary_to_local_date)

----------------------------------------------------------------------

Traceback (most recent call last):

  File "D:/path/to/token-dashboard-hermes\tests\test_followup_requirements.py", line 66, in test_timezone_converts_utc_boundary_to_local_date

    self.assertEqual(data["daily_active"][0]["date"], "2026-09-19")

                     ~~~~~~~~~~~~~~~~~~~~^^^

IndexError: list index out of range



======================================================================

ERROR: test_timezone_change_invalidates_unchanged_file_cache (test_review_regressions.ReviewRegressionTests.test_timezone_change_invalidates_unchanged_file_cache)

----------------------------------------------------------------------

Traceback (most recent call last):

  File "D:/path/to/token-dashboard-hermes\tests\test_review_regressions.py", line 37, in test_timezone_change_invalidates_unchanged_file_cache

    self.assertEqual(first.dashboard(30)["daily_activity"][0]["date"], "2026-09-19")

                     ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^

IndexError: list index out of range



======================================================================

FAIL: test_five_aggregates_and_percentiles (test_backend.BackendTests.test_five_aggregates_and_percentiles)

----------------------------------------------------------------------

Traceback (most recent call last):

  File "D:/path/to/token-dashboard-hermes\tests\test_backend.py", line 73, in test_five_aggregates_and_percentiles

    self.assertTrue(data["workspace_distribution"])

    ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

AssertionError: [] is not true



======================================================================

FAIL: test_incremental_file_change_rebuilds_only_changed_file (test_backend.BackendTests.test_incremental_file_change_rebuilds_only_changed_file)

----------------------------------------------------------------------

Traceback (most recent call last):

  File "D:/path/to/token-dashboard-hermes\tests\test_backend.py", line 102, in test_incremental_file_change_rebuilds_only_changed_file

    self.assertEqual(self.db.dashboard(days=30)["summary"]["total_tokens"], 510)

    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

AssertionError: 0 != 510



======================================================================

FAIL: test_scan_provider_switch_model_switch_dedup_and_legacy (test_backend.BackendTests.test_scan_provider_switch_model_switch_dedup_and_legacy)

----------------------------------------------------------------------

Traceback (most recent call last):

  File "D:/path/to/token-dashboard-hermes\tests\test_backend.py", line 40, in test_scan_provider_switch_model_switch_dedup_and_legacy

    self.assertEqual(data["summary"]["request_count"], 3)

    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

AssertionError: 0 != 3



======================================================================

FAIL: test_duplicate_legacy_snapshot_is_counted_once (test_review_regressions.ReviewRegressionTests.test_duplicate_legacy_snapshot_is_counted_once)

----------------------------------------------------------------------

Traceback (most recent call last):

  File "D:/path/to/token-dashboard-hermes\tests\test_review_regressions.py", line 60, in test_duplicate_legacy_snapshot_is_counted_once

    self.assertEqual(db.dashboard(30)["summary"]["total_tokens"], 100)

    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

AssertionError: 0 != 100



======================================================================

FAIL: test_later_usage_correction_replaces_earlier_response (test_review_regressions.ReviewRegressionTests.test_later_usage_correction_replaces_earlier_response)

----------------------------------------------------------------------

Traceback (most recent call last):

  File "D:/path/to/token-dashboard-hermes\tests\test_review_regressions.py", line 94, in test_later_usage_correction_replaces_earlier_response

    self.assertEqual(db.dashboard(30)["summary"]["total_tokens"], 120)

    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

AssertionError: 0 != 120



======================================================================

FAIL: test_session_thread_state_prevents_cross_thread_model_leak (test_review_regressions.ReviewRegressionTests.test_session_thread_state_prevents_cross_thread_model_leak)

----------------------------------------------------------------------

Traceback (most recent call last):

  File "D:/path/to/token-dashboard-hermes\tests\test_review_regressions.py", line 78, in test_session_thread_state_prevents_cross_thread_model_leak

    self.assertEqual([(row["provider"], row["model"], row["total_tokens"]) for row in rows_b], [("provider-b", "model-b", 200)])

    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

AssertionError: Lists differ: [] != [('provider-b', 'model-b', 200)]



Second list contains 1 additional elements.

First extra element 0:

('provider-b', 'model-b', 200)



- []

+ [('provider-b', 'model-b', 200)]



----------------------------------------------------------------------

Ran 238 tests in 37.034s



FAILED (failures=6, errors=2, skipped=1)


```