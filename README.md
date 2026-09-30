# Codex Token Dashboard

## Windows / Apple 芯片 Mac 共用源码

两端共用 `backend.py`、`desktop.py`、`pricing.py` 和 `web/`，功能、统计口径和页面设计保持一致。
只维护本目录这一份源码；例如新增模型计价时，只修改一次 `pricing.py`。

- Windows x64：使用 WebView2，用户数据仍保存在 `%LOCALAPPDATA%\CodexTokenDashboard`。
- Apple 芯片 Mac（macOS 14 或更新版本）：使用系统 WKWebView，用户数据保存在
  `~/Library/Application Support/CodexTokenDashboard`。不构建 Intel Mac 版本。
- Windows 便携目录的 `providers.json` 优先于用户数据目录；Mac 的 `providers.json` 放在用户数据目录，更新 `.app` 不会覆盖它。
- 数据库、模型计价、日志扫描、筛选、主题及 CSV 导出使用同一套代码。两端的系统字体、窗口按钮和保存对话框采用各系统的原生样式。

### 修改一次，生成两个版本

共用公开仓库：[Marshallma289/token-dashboard-desktop](https://github.com/Marshallma289/token-dashboard-desktop)。
在本目录修改并验证源码后，提交到仓库：

```sh
git add .
git commit -m "Update dashboard"
git push
```

`.github/workflows/build-desktop.yml` 在每次提交、拉取请求或手动运行时，同时构建
Windows x64 和 macOS arm64。两个构建使用同一个提交，产物的 `build-manifest.json`
记录 `source_digest` 和源码提交；同一个提交的源码摘要应一致，便于确认价格表及界面同步。
构建结果在 GitHub 仓库的 **Actions → Build desktop → Artifacts** 下载；提交到 `main` 后，两端
全部构建成功才发布完整的 [Release](https://github.com/Marshallma289/token-dashboard-desktop/releases/latest)。

已安装的程序包含打包时的代码。1.3.0 起，桌面窗口右上角提供“检查更新”；发现新发布后点击
“立即更新”，软件下载对应系统的更新包、校验文件、退出并重启。统计数据、主题设置和供应商
配置保留，原程序目录留作回退备份。检查更新无需 GitHub 登录，启动时会后台检查一次；不会静默安装。
同一版本号的后续源码提交也能通过构建编号检测，不必为了增加模型计价修改多个版本文件。

1.2.0 及更早版本没有更新入口，需手动安装一次 1.3.0 或更新版本，之后即可在软件内更新。
下载失败、校验不符或目录不可写时，不关闭当前软件；新版无法启动确认时尝试恢复并打开旧版。
Mac 应用应放在可写的固定目录，例如用户的 `~/Applications`，不能直接在只读 DMG 或应用临时转移目录更新。
源码模式保留原有开发运行方式，不自动覆盖源码；应用更新只替换程序，不上传本地统计数据库和日志。

### 1.3.0 更新

- 新增桌面页面检查更新、下载进度和一键重启更新。
- 使用 GitHub Releases 发布 Windows x64 与 macOS arm64 的完整更新，校验 SHA-256、文件清单和源码提交。
- 更新失败可恢复旧程序；Windows 便携目录的 `providers.json` 及两端用户数据目录继续保留。
- 后端显示版本、打包版本和更新版本统一读取 `VERSION`。

本地构建先把项目复制到干净的临时目录，不包含个人配置、统计数据库或旧构建产物；
验证和打包均在副本里完成。Windows 与 Mac 必须分别在各自系统构建。

Windows：

```powershell
python -m pip install -r requirements-build.txt
python build_desktop.py --output-dir release
```

Apple 芯片 Mac：

```sh
python3 -m pip install -r requirements-build.txt
sh build-macos.sh
```

源码开发时，Windows 运行 `start-dashboard.cmd`，Mac 运行 `sh start-desktop.command`；
两端也可直接运行 `python desktop.py`（Mac 使用 `python3`）。开发启动器加载当前源码。
浏览器模式继续使用原有 `start-dashboard.sh` 或 `python backend.py serve`。

Mac 构建产物为 `CodexTokenDesktop.app`。当前配置提供本机临时签名，未接入 Apple Developer
证书和公证。更广泛分发前可再接入签名流程；不要通过关闭系统安全保护来运行。

平台依据：[pywebview 渲染器](https://pywebview.flowrl.com/guide/web_engine.html)、
[PyInstaller 目标平台说明](https://pyinstaller.org/en/stable/)、
[GitHub 构建机说明](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)。

## 1.2.0 更新

- 新增暖白 / 墨绿总览布局、版本标识与更清晰的指标层级。
- 枚举失败和文件读取不完整时保留缓存，显示部分更新状态并自动重试。
- 重复记录选择完整快照，不再逐字段取最大值；同一请求优先保留较完整、总量较大的记录，无法可靠判断向下修正时不自动缩减。
- 汇总按数据版本缓存，单次查询只读取一次明细；仍按变化文件全量解析，不宣称字节级增量。
- 接通扫描健康状态轮询和桌面主题恢复；CSV 文本增加公式防护。
- 修正源码启动器的 dist 路径；新增统计完整性回归测试。

## 1.1.0 更新

- 修正旧日志线程归属、累计快照计量和跨文件重复记录重扫的一致性。
- 最近 1 / 7 / 30 / 90 天以当前展示时区的今天为截止日期。
- 历史记录使用当前供应商显示名；扫描状态与页面连接状态分别展示。
- 统一浅色与深色页面，明细支持分页，CSV 保留当前筛选的全部行。
- 桌面主题保存于 `%LOCALAPPDATA%\CodexTokenDashboard\preferences.json`。

### 构建便携版

安装 `requirements-build.txt` 中的构建依赖，然后运行 `build-portable.ps1`。
脚本先验证 Python 测试和前端语法，再在独立构建目录打包，生成带版本号的 ZIP
及 `build-manifest.json` 文件校验清单。源码包不应包含 build、dist、缓存数据库或个人配置。

## Windows 桌面客户端

现在默认启动独立桌面窗口，不打开外部浏览器。客户端使用 Windows WebView2 渲染精修后的本地界面，并在随机回环端口启动仅供该窗口使用的内部服务；不会对局域网开放。

桌面版与精修网页使用同一套现代卡片界面，支持浅色/深色主题、模型用量卡片、工作空间横条图、日活趋势、24 小时热力图、请求大小分布、悬浮精确提示和原生 CSV 保存窗口。

- 免安装版：完整解压 `CodexTokenDesktop-Windows.zip`，双击文件夹内的 `CodexTokenDesktop.exe`。请保留旁边的 `_internal` 文件夹；无需安装 Python。
- 源码目录：双击 `start-dashboard.cmd`，通过本机 Python 打开当前的 `desktop.py`。需要明确启动 `dist` 中的旧便携包时，可给启动器加 `-Portable` 参数。
- 供应商、工作空间和模型筛选全局联动且可直接相互切换，图表支持悬浮提示，CSV 导出当前筛选明细。
- 桌面数据库保存在 `%LOCALAPPDATA%\CodexTokenDashboard\usage.sqlite3`，首次启动会从现有 Codex 日志重建统计，不修改日志。关闭窗口即可退出。
- Windows 11 通常已包含 WebView2；免安装包不需要 Python。仅从源码运行桌面版时，先执行 `py -m pip install -r requirements-desktop.txt`。
- 以下浏览器版说明作为可选模式保留；需要时运行 `python backend.py serve`。

一个本地、实时、无遥测的 Codex Token 用量看板。它读取 Codex 自己写入的
rollout JSONL 日志，将官方 OpenAI API 与不同 `model_provider` 的第三方供应商
分开统计，并在浏览器中实时展示。

## 已实现

- 模型筛选与模型 Token 用量卡片，展示占比、请求数、输入及输出；
- 供应商、工作空间和模型选项在筛选后仍完整保留，可直接切换；
- 日活图与小时热力图支持鼠标悬浮、键盘聚焦查看精确请求数和 Token；
- 一键重置筛选，优化桌面/窄屏布局与明暗主题；
- 工作空间活跃分布；
- 日活趋势与 24 小时分布；
- 单次请求 Token 大小直方图，以及 P50 / P90 / P99；
- 按“日期 × 供应商 × 模型”的日使用分布；
- 每日模型 Input / Output / Cache read / Cache write / Reasoning / Total / 请求数明细；
- 按官方公开价格估算的美元费用、计价覆盖率和未计价模型提示；
- 1 / 7 / 30 / 90 天和全部历史筛选；
- 供应商、工作空间筛选；
- SQLite 增量缓存、2 秒轮询、SSE 实时刷新；
- 明暗主题与响应式界面；
- JSON / CSV 导出；
- 官方 API 和同名模型的第三方供应商独立归因。

## 快速开始

要求 Python 3.10 或更高版本，不需要安装第三方 Python 包。Windows 启动器会依次
查找系统 Python、`py` 启动器和 Codex 自带的 Python，因此通常无需额外配置。

### Windows

双击 `start-dashboard.cmd`。启动窗口会显示所用 Python 和看板地址；如果启动
失败，窗口会停留并显示原因，不会再一闪而过。

也可以在 PowerShell 中运行：

```powershell
python backend.py serve
```

如果电脑使用 `py` 启动器：

```powershell
py -3 backend.py serve
```

### macOS / Linux

```bash
python3 backend.py serve
```

服务默认监听 `127.0.0.1:8765` 并打开浏览器。首次启动会扫描：

```text
~/.codex/sessions/**/*.jsonl
~/.codex/archived_sessions/**/*.jsonl
```

停止服务请在终端按 `Ctrl+C`。

如果双击时提示找不到 Python 3.10，可安装新版 Python，或设置环境变量
`CODEX_DASHBOARD_PYTHON` 为可用的 `python.exe` 完整路径。仅检查启动环境可运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-dashboard.ps1 -CheckOnly
```

## 供应商显示名

Codex 日志中的 `session_meta.model_provider` 或
`thread_settings_applied.model_provider_id` 是供应商归因的事实来源。`openai`
会自动显示为“OpenAI 官方”。要给其他供应商设置更友好的名称：

1. 将 `providers.json.example` 复制为 `providers.json`；
2. 将键改为 Codex `config.toml` 中的 provider ID；
3. 将值改为希望在看板显示的名称。

示例：

```json
{
  "providers": {
    "cc-switch": "CC Switch",
    "moonshot": "Moonshot",
    "deepseek": "DeepSeek"
  }
}
```

切换供应商后，新请求会按日志记录的 provider 独立统计；即使模型名相同，也
不会与官方用量合并。旧日志缺少 provider 元数据时会显示 `unknown` 或
`custom`，软件不会猜测供应商。

## 常用命令

```bash
# 检查日志目录和数据库
python backend.py doctor

# 扫描一次后退出
python backend.py scan

# 输出最近 30 天汇总 JSON
python backend.py summary --days 30

# 启动看板，不自动打开浏览器
python backend.py serve --no-open

# 指定时区、端口和扫描频率
python backend.py serve --timezone Asia/Shanghai --port 9000 --interval 1

# 导出 JSON 或 CSV
python backend.py export --days 30 --format json --output usage.json
python backend.py export --days 30 --format csv --output usage.csv
```

如果 `CODEX_HOME` 不在用户目录，可重复传入 `--root`：

```bash
python backend.py serve --root /path/to/sessions --root /path/to/archived_sessions
```

默认数据库为项目目录下的 `codex-token-dashboard.sqlite3`。可以用 `--db`
修改位置。

## 统计口径

- 新版日志只累计 `token_usage_record.payload.usage`。这是一次 API response 的
  增量用量；不会错误累加 `turn_token_usage` 或 `thread_token_usage` 的累计值。
- 按 `(thread_id, response_id)` 去重。同一 response 被复制到其他 rollout
  文件或重扫时只计一次，不同线程允许拥有相同 response ID。
- 同一文件同时存在新版 `token_usage_record` 和旧版 `token_count` 时，以新版
  记录为准，避免重复。
- Input 已包含缓存命中 Token；Cache、Cache write、Reasoning 是拆分指标，
  Reasoning 不会再次加到 Total。
- 费用估算按每条请求单独匹配模型与价格档位，再聚合到日期、模型和筛选结果。
  OpenAI 的 Input、Cached input、Cache write 按官方定义互斥计价，并以
  272,000 input tokens 作为长短上下文界线；DeepSeek 按请求时间区分
  01:00–04:00、06:00–10:00 UTC 工作日高峰价和低谷价；Qwen 使用
  Alibaba Cloud Model Studio 国际站价格。未配置官方独立价格的模型显示
  “未计价”，其 Token 单独计入覆盖率，不按其他模型价格猜测。
- 价格按日志中的模型名匹配，不按供应商 ID 限制；因此 `custom`、`cc-switch`
  等同名模型的费用仍按公开参考价估算，供应商折扣或合同价不会自动纳入。
- 部分中转会把供应商标识写进模型名（例如 `deepseek/deepseek-flash`）。这类
  名称按同一模型的公开参考价计费，高峰/低谷时段仍按每条请求自身的时间判断；
  统计时也并入同一模型行（`deepseek/deepseek-flash` 计入 `deepseek-flash`），
  合并前的原始名称保留在每行的 `source_models` 字段中。供应商仍分开统计。
- DeepSeek 官方说明中国法定节假日在高峰时段内也按低谷价计费，但本地日志
  不含可靠的节假日标记；当前实现仅按 UTC 工作日时段判断，节假日例外需人工
  核对。
- 时间戳存为 UTC，日期和小时按 `--timezone`（默认系统本地时区）归组。
- 日志移入 `archived_sessions` 时会安全重建索引，避免归档移动导致丢计。

这是一份本机日志统计，不等同于 OpenAI 或第三方供应商的账单。订阅额度、
服务端重试、供应商侧折扣、区域价格、高峰日期例外与未写入本机日志的请求
可能造成差异。当前费用基于以下公开参考价，并在界面和 CSV 中明确标注：

- OpenAI API Pricing：<https://developers.openai.com/api/docs/pricing>
- DeepSeek API Pricing：<https://api-docs.deepseek.com/quick_start/pricing/>
- Alibaba Cloud Model Studio Pricing：
  <https://www.alibabacloud.com/help/en/model-studio/model-pricing>
- Xiaomi MiMo Pricing：<https://xiaomimimo.com>

`codex-auto-review` 的调用本身没有公开的独立模型单价，因此保持“未计价”；
它触发的后端模型调用若出现在本机日志中，会按对应模型单独计量。

桌面数据库启动时会执行轻量完整性检查。若 SQLite 文件已损坏，程序会先把
原文件及 WAL/SHM 副本重命名为带 UTC 时间戳的 `.corrupt-*.bak`，再重建缓存
并从 Codex 日志重新扫描；不会静默覆盖损坏文件。

## 本地 API

- `GET /`：看板页面；
- `GET /api/health`：扫描状态和记录数；
- `GET /api/dashboard?days=30&provider=openai&workspace=...`：聚合数据；
- `GET /api/events`：SSE 实时更新流。

`days=0` 表示全部历史。服务默认只绑定回环地址；不要在不可信网络上使用
`--host 0.0.0.0`。

## 测试

```bash
python -m unittest discover -s tests -v
node --check web/app.js
```

测试覆盖现代与旧版日志、多供应商/模型切换、重复 response、增量重扫、归档
移动、北京时间跨 UTC 日期边界、五类聚合、静态资源、HTTP API 和 SSE。

## 隐私

数据库只保存 timestamp、workspace、thread/response 标识、provider、model 和
Token 计数。不会保存提示词、回复正文、工具参数、工具输出、加密 reasoning、
环境变量或 API Key；不会读取 `auth.json`；网页不使用 CDN、分析脚本或遥测。

## 项目来源与许可

界面和指标设计参考了 `fuyi-git/token-dashboard`，但该仓库在审查时没有许可证，
因此本项目没有复制其代码。数据适配和实时架构也参考了 MIT 许可的
`xiufengsun/TokenTracker` 与 `nateherkai/token-dashboard` 的公开设计思路。
具体说明见 `THIRD_PARTY_NOTICES.md`。

本项目采用 MIT License。
