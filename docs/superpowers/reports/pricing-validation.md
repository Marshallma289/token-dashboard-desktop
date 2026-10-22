> 归档说明：这是价格规则补全阶段的历史验收快照，金额随源库采样时间变化；表内金额保留当时实测值。个人路径已替换为占位符；原始数据库、日志、脚本和截图仅在本地 `work/` 保留，不随仓库分发。下文“未做 git commit”指当时验收阶段。

# Hermes Flash 计价修复验收

## 1.8.0 收尾复验

本次收尾重新运行全量测试：249 tests，6 failures + 2 errors，skipped=1；八项失败的完整测试 ID 和类型与保存的基线逐项一致。基线 ID 见 [Hermes 接入阶段验收报告](HERMES-验收报告.md) 的“精确失败列表”。本次只整理版本、文档与 Git 忽略规则，不重写历史真实库金额，也不重新访问个人源库。

干净检出后，在仓库根目录可复跑：

```sh
python -c "import unittest; unittest.main(module=None, argv=['unittest', 'discover', '-s', 'tests'])"
node --check web/app.js
node --check web/analytics.js
node --check web/cache-metrics.js
node --check tests/frontend_hermes_harness.js
```

全量测试因既有八项基线失败返回 1；本次四项 JavaScript 语法检查均返回 0。下文保留原验收记录。

已补 `deepseek-v4.1-flash`、`deepseek/deepseek-v4.1-flash` → `deepseek-flash` 两条别名，并修复 Hermes 独立输入桶传入价格引擎时的口径。未修改 Codex 的 input 包含缓存口径、单价或保护条件，未做 git commit。

## 真实库与手算

只读读取 `D:/path/to/Hermes\state.db`，使用独立的本地临时统计库验收；未对用户现有统计库应用历史重新计价。

| 指标 | 结果 |
|---|---:|
| 聚合记录 | 34 |
| API 调用数 | 1,372 |
| 总 Token（沿用 input + output 展示口径） | 5,298,887 |
| 计价覆盖率 | 100% |
| 估算总费用，未舍入 | 2.888373204 USD |
| 独立 Decimal 手算 | 2.888373204 USD |
| 未舍入程序总额与手算绝对误差 | 0 USD |
| API 返回值（8 位小数） | 2.88837320 USD |
| API 舍入误差 | 约 4 × 10⁻⁹ USD |
| 浏览器费用卡片 | $2.89，计价覆盖率 100% · 全部已计价 |

按 [DeepSeek 官方参考价](https://api-docs.deepseek.com/quick_start/pricing/) 独立计算。以下输入、缓存读、缓存写为并列独立量：

| 时段 | 行数 | 输入 | 缓存读 | 缓存写 | 输出 | 费用 USD |
|---|---:|---:|---:|---:|---:|---:|
| 低谷 | 17 | 1,624,427 | 51,436,928 | 0 | 347,513 | 0.606482634 |
| 高峰 | 17 | 2,722,837 | 123,351,245 | 0 | 604,110 | 2.281890570 |

低谷每百万 Token 价格：输入 0.15、缓存读 0.003、缓存写 0.15、输出 0.60 USD；高峰乘 2。逐行计算后求和，不把累计用量乘 API 调用次数。

真实单行样本，`last_seen=2026-10-21T13:32:53.731086+00:00`，低谷：

```text
input=108093, cache_read=5692032, cache_write=0, output=39026
(108093×0.15 + 5692032×0.003 + 0×0.15 + 39026×0.60) / 1000000
= 0.056705646 USD
程序单行结果 = 0.056705646 USD
```

`_quote_for_usage(aggregate=True)` 把 input + cache_read + cache_write 作为价格引擎的输入总量。引擎原有 `_usage_counts()` 再拆回三个独立桶；普通输入取原 input、缓存命中取原 cache_read，缓存写另计，没有重复扣除或漏算。新估算、冻结价格快照更新、历史重算共用此转换。

限制：沿用现有实现，以每行 `last_seen` 判断高低峰；累计记录无法还原每次调用的时段，中国法定节假日例外未自动识别。中转站实际扣费可能不同，本次没有猜测或写死折扣。

## 测试与前端

- 先写回归测试，确认别名和输入口径导致断言失败，再实施修复；另确认前端提示测试先失败后通过。
- 新增 7 个后端测试与 1 个前端测试全部通过：两种别名、覆盖率与非零费用、三个独立桶、高低峰、`custom` 覆盖和供应商隔离、冻结快照及重新计价、源库及 sidecar 不变、费用卡片与说明。
- 全量命令 `python -m unittest discover -s tests`：原 241 项，修复后 249 项；仍为 **6 failures + 2 errors，skipped=1**。已逐项比较失败测试完整 ID 和类型，八项与基线完全相同。
- Chromium 连接真实 HTTP 后端，验证 Hermes → Codex → 全部筛选，费用卡片非“未计价”，覆盖率 100%，浏览器异常 0；已检查截图。
- UI 与 README 明示 DeepSeek 官方参考价、中转站折扣/倍率可能造成差异及“价格规则”覆盖入口。已有历史费用快照不自动改写，需在 Hermes 筛选下先预览再应用历史重算。
- 独立只读代码审查未发现本次新增的实质问题，`git diff --check` 通过。

全程 Python 仅使用模块入口或 `.py` 文件，未向裸 Python 传管道/stdin。

## 源库只读证据

真实 DB、WAL、SHM 在最后一次完整验收前后的 **SHA-256、大小、mtime_ns 与存在状态完全一致**。测试也断言临时源库及 sidecar 零改动；原有 WAL 安全读取测试保持通过。

首轮真实验收遇到 WAL/SHM 在窗口内变化（DB 本体未变），严格断言如实失败；保留该轮证据，未放宽断言。随后使用相同断言重跑，在稳定窗口验证通过。读取路径只使用普通只读文件句柄复制 DB/WAL，在临时目录以 SQLite `mode=ro` 查询。

证据与复跑脚本：

- 精确失败基线比对：`work/pricing-test-comparison.json`（仅本地）
- 全量测试日志：`work/pricing-final-tests.txt`（仅本地）
- 修复前失败测试：`work/pricing-red-tests.txt`（仅本地）
- 真实库验算及源文件前后哈希：`work/pricing-live-evidence.json`（仅本地）
- 首轮活跃写入窗口证据：`work/pricing-live-active-writer.json`（仅本地）
- 逐行手算审计：`work/pricing-live-row-audit.json`（仅本地）
- 浏览器验收输出：`work/pricing-browser-output.txt`（仅本地）
- 费用卡片截图：`work/pricing-overview.png`（仅本地）
- 参考价提示截图：`work/pricing-reference-notice.png`（仅本地）
- 明细截图：`work/pricing-requests.png`（仅本地）
- 全量基线比对脚本：`work/verify_pricing_tests.py`（仅本地）
- 真实库及浏览器验收脚本：`work/verify_pricing_live.py`（仅本地）
