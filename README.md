# Codex Token Dashboard

本地 Codex Token 用量看板：读取本机 rollout JSONL 日志，实时展示用量、请求分布和按公开参考价估算的美元费用。支持 Windows x64、Apple Silicon Mac 桌面版，以及浏览器模式。

[下载桌面版](https://github.com/Marshallma289/token-dashboard-desktop/releases/latest) · [构建记录](https://github.com/Marshallma289/token-dashboard-desktop/actions/workflows/build-desktop.yml) · [问题反馈](https://github.com/Marshallma289/token-dashboard-desktop/issues)

> 看板统计的是本机日志，费用是参考估算，不等同于 OpenAI 或第三方供应商账单，也不代表订阅额度消耗。

## 终端一键安装（推荐）

复制一条命令到终端即可安装最新版。脚本自动下载、校验 SHA-256 和文件清单并完成安装，无需另装 Python、手动解压或管理员权限。只连接本仓库的 GitHub 发布，不上传本地日志或统计数据。

### Windows：PowerShell

```powershell
irm https://github.com/Marshallma289/token-dashboard-desktop/releases/latest/download/install.ps1 | iex
```

默认安装到 `%LOCALAPPDATA%\Programs\CodexTokenDesktop`，创建当前用户的桌面和开始菜单快捷方式，并启动软件。

指定 F 盘安装目录：

```powershell
& ([scriptblock]::Create((irm https://github.com/Marshallma289/token-dashboard-desktop/releases/latest/download/install.ps1))) -InstallDir 'F:\Token统计面板\CodexTokenDesktop'
```

脚本支持 `-NoLaunch`（安装后不启动）和 `-NoShortcut`（不创建快捷方式）。Windows 仍需 WebView2 Runtime；通常 Windows 10 / 11 已包含，缺少时按下文的 Microsoft 官方链接安装。

### Apple Silicon Mac：终端

```sh
curl -fsSL https://github.com/Marshallma289/token-dashboard-desktop/releases/latest/download/install.sh | /bin/bash
```

默认安装到 `~/Applications/CodexTokenDesktop.app` 并打开软件。自定义目录或安装后不启动：

```sh
curl -fsSL https://github.com/Marshallma289/token-dashboard-desktop/releases/latest/download/install.sh | /bin/bash -s -- --install-dir "$HOME/Applications/CodexTokenDesktop.app" --no-launch
```

只支持 Apple 芯片及 macOS 14 或更新版本。Mac 包仍采用临时签名、未经 Apple 公证；安装脚本不会关闭系统安全保护，首次打开仍遵循系统提示。

### 再次安装或升级

关闭正在运行的软件，再对同一安装目录运行命令即可安装最新版。脚本先校验新包，再替换已识别的程序并保留旧程序备份；不会覆盖无关目录。历史数据库、主题和供应商配置保留。已安装的桌面版也可继续使用页面右上角的“检查更新”。

## 手动下载安装

桌面发行包已包含 Python 运行环境，无需另行安装 Python。

| 平台 | 下载文件名 | 系统要求 |
| --- | --- | --- |
| Windows x64 | `CodexTokenDashboard-Windows-Portable-x64-*.zip` | 64 位 Windows 10 / 11，WebView2 Runtime |
| Apple Silicon Mac | `CodexTokenDashboard-macOS-arm64-*.zip` | macOS 14 或更新版本；不提供 Intel Mac 包 |

### Windows

1. 从 [最新 Release](https://github.com/Marshallma289/token-dashboard-desktop/releases/latest) 下载 Windows ZIP，完整解压。
2. 双击 `CodexTokenDesktop.exe`，保留旁边的 `_internal` 文件夹。
3. 如果系统缺少 WebView2，请安装 [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。

不要直接在压缩包内运行。Windows 11 和大多数仍在更新的 Windows 10 已包含 WebView2。

### Apple Silicon Mac

1. 下载 macOS arm64 ZIP 并解压。
2. 将 `CodexTokenDesktop.app` 放到固定、可写的目录，例如 `~/Applications`，再打开应用。

当前 Mac 包采用临时签名，未接入 Apple Developer 证书和公证。若系统阻止打开，请先确认下载来源，并按系统针对单个应用的提示处理；不要关闭系统安全保护。应用内更新需要可写目录，不能在只读 DMG 或应用临时转移目录中进行。

首次启动会扫描本机 Codex 日志；已有历史先显示，新数据在后台采集。默认每 2 秒检查日志变化，并通过 SSE 刷新页面。关闭桌面窗口即可退出。

## 主要功能

- **用量总览**：Token 总量、请求数、模型占比及 Input / Output / Cache read / Cache write / Reasoning 明细。
- **联动筛选**：供应商、工作空间、模型，最近 1 / 7 / 30 / 90 天或全部历史。
- **分布与趋势**：工作空间活跃度、日趋势、24 小时热力图、单次请求大小直方图和 P50 / P90 / P99。
- **明细与导出**：按日期、供应商、模型汇总，分页查看；页面导出当前筛选的全部明细为 CSV，命令行支持 JSON / CSV。
- **本地历史**：用量持久保存到 SQLite，已采集记录在删除原始日志后仍保留。
- **数据管理**：桌面版可自选历史保存目录，并预览、确认清理指定天数之前的数据。
- **桌面体验**：浅色 / 深色主题、原生 CSV 保存窗口、检查更新和下载进度。

Windows 与 Mac 共用 `backend.py`、`desktop.py`、`pricing.py` 和 `web/`，采用相同统计口径和界面；分别使用 WebView2 和系统 WKWebView。

## 数据、历史与隐私

### 日志来源

默认扫描：

```text
~/.codex/sessions/**/*.jsonl
~/.codex/archived_sessions/**/*.jsonl
```

如果设置了 `CODEX_HOME`，则读取其下的 `sessions` 和 `archived_sessions`。浏览器模式及命令行也可重复传入 `--root` 指定扫描目录。

### 数据保存位置

桌面版和浏览器版默认使用相同的独立数据目录：

| 系统 | 数据目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\CodexTokenDashboard` |
| macOS | `~/Library/Application Support/CodexTokenDashboard` |
| Linux | `~/CodexTokenDashboard`（设置 `LOCALAPPDATA` 时使用其下的同名目录） |

历史保存在 `usage.sqlite3`；桌面主题和自定义历史路径保存在默认数据目录的 `preferences.json`。更新应用不会覆盖这些文件。页面底部显示历史库大小，悬浮可查看路径。

桌面版右上角的 **数据管理** 支持输入完整目录或选择文件夹，再点 **保存并迁移**。应用会在线复制并校验现有历史，成功后使用新位置；旧数据库保留作备份。目标目录已有 `usage.sqlite3` 时会拒绝覆盖。请使用应用安装目录以外的位置；若选择外置磁盘，启动时需连接该磁盘。浏览器版和命令行仍以 `--db` 指定位置。

### 手动清理历史

在 **数据管理** 中选择“删除多少天前的数据”，可输入自定义天数。先 **预览清理**，查看截止日期和请求数，再确认执行。只删除严格早于截止日期的本地统计；截止当天及之后的数据保留。例如 10 月 1 日选择 30 天，删除 9 月 1 日之前的数据。不会自动按天清理，也不会删除 Codex 原始日志。

清理前会在当前数据目录的 `backups` 子目录生成校验过的 SQLite 备份，完成后显示其路径。清理截止日期会随数据库持久保存，旧日志重扫不会重新导入已清理日期。备份及迁移时保留的旧库仍占磁盘空间；如需恢复，先关闭应用，将当前 `usage.sqlite3` 及存在的同名 `-wal` / `-shm` 文件移到备份位置，再将清理前的备份复制为当前保存位置的 `usage.sqlite3`。

软件运行期间自动采集用量并事务写入历史。重扫、日志归档、跨文件复制，以及已删除日志恢复时会去重；数据库会回收无引用快照和失效索引，但容量仍随真实请求数增长。

**历史保留有边界**：软件关闭期间未采集、且随后被删除的日志无法补回；数据库本身被删除或损坏后，已不存在的源日志也无法重新扫描恢复。

首次迁移旧版数据库时会保留 `usage.sqlite3.pre-history.bak`。1.4.0 采用新的历史结构，回退旧版前须停止软件并还原迁移备份。数据库损坏时，会先将原文件及 WAL / SHM 副本改名为带 UTC 时间戳的 `.corrupt-*.bak`，再从可读日志重建。

### 隐私与网络

历史库保存时间、工作空间、供应商、模型、Token 数，以及用于去重的线程 / response 标识 SHA-256 摘要；扫描索引还记录源文件路径等元数据。不复制对话正文、原始日志或密钥，不保存工具参数、工具输出或加密 reasoning，也不读取 `auth.json`。

网页资源全部在本地，无 CDN、分析脚本或遥测。桌面内部服务仅监听随机回环端口，浏览器模式默认监听 `127.0.0.1:8765`。本地统计无需联网；桌面更新检查和下载会连接 GitHub，但不上传日志或统计数据库。

## 统计与费用口径

### Token 与请求数

- 新版日志累计 `token_usage_record.payload.usage` 的单次 API response 用量，不叠加 `turn_token_usage` 或 `thread_token_usage` 的累计值。
- 按 `(thread_id, response_id)` 去重；不同线程可以拥有相同 response ID。“请求数”指可计量的用量记录，并非用户发送消息数。
- 同一文件同时含新版 `token_usage_record` 和旧版 `token_count` 时，优先使用新版；旧日志按其可用快照解析。
- 重复记录选择完整快照，不逐列拼接最大值。Input 已包含缓存命中 Token；缓存读取、缓存写入是输入拆分指标，Reasoning 属于输出子集，不再重复加到 Total。Total 优先使用日志报告的总数，缺失时以 Input + Output 计算。
- 时间戳存为 UTC；日期和小时按展示时区归组。最近 N 天以该时区的今天为截止日期。

供应商以日志中的 `session_meta.model_provider` 或 `thread_settings_applied.model_provider_id` 为归因来源，同名模型的不同供应商仍分开统计。极老日志缺少稳定请求标识时只能保守去重，不能保证与账单完全一致。

### 费用估算

费用使用应用内 `pricing.py` 的参考价格表，**不会在线自动同步价格**。按每条请求的模型、输入、缓存、输出和时间匹配价格档位后再汇总。页面与 CSV 标明计价状态，未配置价格的模型保留 Token 统计并显示“未计价”。

| 价格来源 | 当前实现 |
| --- | --- |
| OpenAI | 配置了长上下文档位的模型，在 Input 超过 272,000 Token 时切换价格 |
| DeepSeek | UTC 工作日 01:00–04:00、06:00–10:00 使用高峰价，其余时间使用低谷价 |
| Qwen | 使用 Alibaba Cloud Model Studio 国际站参考价 |
| Xiaomi MiMo | 使用价格表中的参考价；未单列缓存写入价格时按普通输入估算 |

模型名按已配置别名归一化，例如 `deepseek/deepseek-flash` 并入 `deepseek-flash`；原始名称保留在 API 的 `source_models` 字段中。不会将任意带前缀的名称自动认作已知模型。

价格匹配不限制 provider ID，因此第三方供应商同名模型也按公开参考价估算。订阅额度、服务端重试、供应商折扣、合同价、区域价格和未写入本机日志的请求可能造成差异。DeepSeek 中国法定节假日的低谷价例外未自动识别，需人工核对。

`codex-auto-review` 没有配置独立公开单价，保持“未计价”；它触发的后端模型调用若出现在本机日志中，按对应模型单独计量。

参考来源：[OpenAI](https://developers.openai.com/api/docs/pricing)、[DeepSeek](https://api-docs.deepseek.com/quick_start/pricing/)、[Alibaba Cloud Model Studio](https://www.alibabacloud.com/help/en/model-studio/model-pricing)、[Xiaomi MiMo](https://xiaomimimo.com)。具体模型、价格和参考日期以 [pricing.py](pricing.py) 为准。

## 配置供应商显示名

复制 [providers.json.example](providers.json.example) 为 `providers.json`。键填写 Codex `config.toml` 中的 provider ID，值填写看板中的显示名称：

```json
{
  "providers": {
    "moonshot": "Moonshot",
    "deepseek": "DeepSeek",
    "siliconflow": "SiliconFlow"
  }
}
```

配置位置取决于运行方式：

| 运行方式 | 配置读取位置 |
| --- | --- |
| Windows 桌面版（含源码运行） | 优先程序 / 源码目录的 `providers.json`，其次用户数据目录 |
| macOS 桌面版 | 用户数据目录中的 `providers.json`；源码运行时若没有此文件，会回退源码目录 |
| 浏览器模式 / 命令行 | 默认源码目录；可用 `--providers` 指定文件 |

默认将 `openai` 显示为“OpenAI 官方”。缺少 provider 元数据时显示 `unknown` 或 `custom`，不猜测供应商。显示名配置仅改变标签，不修改日志归因或参考价格。

## 从源码运行

浏览器后端只需 Python 3.10+，不需要第三方 Python 包；桌面窗口额外需要 `requirements-desktop.txt` 中的 pywebview 依赖。

```sh
git clone https://github.com/Marshallma289/token-dashboard-desktop.git
cd token-dashboard-desktop
```

### 桌面模式

Windows：

```powershell
py -m pip install -r requirements-desktop.txt
.\start-dashboard.cmd
```

Mac：

```sh
python3 -m pip install -r requirements-desktop.txt
sh start-desktop.command
```

也可直接运行 `python desktop.py`（Mac 使用 `python3`）。Windows 的 `start-dashboard.cmd` 默认启动当前源码的桌面窗口；源码模式不支持应用内替换更新。

Windows 启动器会查找可用 Python，也可用 `CODEX_DASHBOARD_PYTHON` 指定可执行文件完整路径。检查启动环境：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-dashboard.ps1 -CheckOnly
```

### 浏览器模式与命令行

```sh
# Windows；使用 py 启动器时可将 python 换成 py -3
python backend.py serve

# macOS / Linux
python3 backend.py serve
```

默认打开浏览器并监听 `http://127.0.0.1:8765`，终端按 `Ctrl+C` 停止。以下示例中，macOS / Linux 使用 `python3`：

```sh
# 检查日志目录和数据库
python backend.py doctor

# 扫描一次后退出
python backend.py scan

# 输出最近 30 天汇总 JSON
python backend.py summary --days 30

# 指定展示时区、端口和轮询间隔（秒），不自动打开浏览器
python backend.py serve --timezone Asia/Shanghai --port 9000 --interval 1 --no-open

# 指定日志目录；--root 可重复使用
python backend.py serve --root /path/to/sessions --root /path/to/archived_sessions

# 指定供应商显示名配置
python backend.py serve --providers /path/to/providers.json

# 导出按日期、供应商、模型汇总的数据
python backend.py export --days 30 --format json --output usage.json
python backend.py export --days 30 --format csv --output usage.csv
```

`--days 0` 表示全部历史；`--db` 可指定数据库路径。若放到工作空间内，删除该目录也会删除对应历史。旧浏览器版项目目录中的数据库可通过 `--db /旧路径/codex-token-dashboard.sqlite3` 原地迁移，或在停用旧软件后复制到独立数据目录。

### 本地 API

| 路径 | 用途 |
| --- | --- |
| `GET /` | 看板页面 |
| `GET /api/health` | 扫描健康状态、记录数和历史库信息 |
| `GET /api/dashboard?days=30&provider=openai&workspace=...&model=...` | 按筛选条件返回聚合数据 |
| `GET /api/events` | SSE 实时更新流 |

服务默认只监听回环地址，不要在不可信网络上使用 `--host 0.0.0.0`。更多安全说明见 [SECURITY.md](SECURITY.md)。

## 应用更新

1.3.0 起的发行桌面版启动时会后台检查 GitHub Releases，也可点击窗口右上角“检查更新”。发现更新后由用户点击“立即更新”，下载并校验 SHA-256、文件清单和源码信息，再退出并重启；不会静默安装，无需 GitHub 登录。

- 更新保留统计数据库、主题设置及供应商配置，旧程序目录留作回退备份。
- 下载失败、校验不符或目录不可写时不关闭当前软件；新版启动未确认时尝试恢复旧程序。
- 同一版本号的新构建也能通过构建编号识别。
- 1.2.0 及更早版本需先手动安装 1.3.0 或更新版本，之后可在应用内更新。

## 开发与构建

### 项目结构

```text
backend.py              日志解析、持久历史、聚合统计、HTTP API 与 SSE
desktop.py              桌面窗口、主题偏好、数据管理与原生 CSV 保存
pricing.py              参考价格和模型别名
updater.py              更新检查、下载与校验
update_helpers.py       程序替换、重启与失败恢复
web/                    共用前端
tests/                  单元与回归测试
build_desktop.py        干净暂存、验证与跨平台打包入口
publish_update.py       Release 发布与更新清单
install.ps1 / install.sh 终端一键安装与再次安装升级
VERSION                 共用版本号
```

### 验证

在仓库根目录运行：

```sh
python -m unittest discover -s tests -v
node --check web/app.js
```

测试涵盖新旧日志、供应商 / 模型切换、重复 response、重扫、归档、源文件删除后的历史保留、迁移与空间回收、时区、聚合、HTTP API 和 SSE。

### 本地构建

安装构建依赖及 Node.js。Windows 与 Mac 必须分别在各自系统构建；Mac 需要原生 arm64 Python。本项目 CI 使用 Python 3.14 和 Node.js 22。

Windows x64：

```powershell
python -m pip install -r requirements-build.txt
python build_desktop.py --output-dir release
```

Apple Silicon Mac：

```sh
python3 -m pip install -r requirements-build.txt
sh build-macos.sh
```

构建脚本将源码复制到干净暂存目录，排除个人配置、数据库和旧构建产物，在副本中验证并打包。输出包含 ZIP 和 `*.build-manifest.json`，清单记录源码摘要、版本、平台和文件校验信息。

### GitHub Actions

[构建工作流](.github/workflows/build-desktop.yml) 在推送到 `main`、Pull Request 或手动运行时，同时构建 Windows x64 和 macOS arm64。产物可在 **Actions → Build desktop packages → 对应运行 → Artifacts** 下载。

两端构建使用同一个提交，`source_digest` 应一致。工作流在各系统实际执行终端安装与再次安装检查；两端都成功后，`main` 分支发布完整 Release、`update.json` 和两个终端安装脚本。因此直接修改 `main` 的 README 也会触发构建及发布。

## 版本摘要

| 版本 | 主要变化 |
| --- | --- |
| 1.4.1 | 深色主题统一配色与层级；自定义历史保存位置；预览和确认清理指定天数前的数据 |
| 1.4.0 | 独立持久历史；日志删除后保留已采集用量；迁移备份与空间回收 |
| 1.3.0 | 应用内检查、下载并重启更新；失败恢复；版本号统一读取 `VERSION` |
| 1.2.0 | 统计完整性和扫描恢复改进；完整快照去重；CSV 公式防护 |
| 1.1.0 | 旧日志计量、跨文件去重、日期范围修正；明细分页与主题保存 |

## 来源与许可

界面和指标设计参考 `fuyi-git/token-dashboard` 的公开思路；该项目在审查时没有许可证，本项目未复制其代码。数据适配和实时架构参考了 MIT 许可的 `xiufengsun/TokenTracker` 与 `nateherkai/token-dashboard`。

具体说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目采用 [MIT License](LICENSE)。
