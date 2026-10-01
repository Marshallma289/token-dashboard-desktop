"""External update helpers. Both accept only the absolute path of job.json.

The application must acknowledge its own PID only after its window and HTTP
server are ready. Helpers keep the previous installation until startup succeeds;
the new application then removes the rollback and download staging area.
"""

WINDOWS_HELPER = r'''param([Parameter(Mandatory=$true)][string]$JobPath)
$ErrorActionPreference = 'Stop'
$stage = $null
$newProcess = $null
$oldMoved = $false
$newMoved = $false
$pathsValidated = $false
$parentExited = $false
$job = $null

function Safe-Path([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) { throw '路径必须为绝对路径' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($Path -cne $full -or $full -eq [IO.Path]::GetPathRoot($full).TrimEnd('\')) { throw "不安全的路径: $Path" }
    if ($full.StartsWith('\\')) { throw '不支持网络安装目录' }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "路径不能经过链接: $cursor" }
        }
        $next = [IO.Path]::GetDirectoryName($cursor)
        if ($next -eq $cursor) { break }
        $cursor = $next
    }
    return $full
}
function Write-Result([string]$State, [string]$Message) {
    if (-not $stage) { return }
    $data = @{state=$State; message=$Message}
    if ($State -eq 'success') { $data.backup = [string]$job.backup }
    $temp = Join-Path $stage 'result.tmp'
    $data | ConvertTo-Json -Compress | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination (Join-Path $stage 'result.json') -Force
}
try {
    $JobPath = Safe-Path $JobPath
    $job = Get-Content -LiteralPath $JobPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($job.id -isnot [string] -or $job.id -cnotmatch '^[0-9a-f]{32}$' -or $job.platform -cne 'win32') { throw '更新任务标识或平台无效' }
    if (($job.parent_pid -isnot [int] -and $job.parent_pid -isnot [long]) -or $job.parent_pid -le 0 -or $job.parent_pid -eq $PID) { throw '父进程标识无效' }
    $target = Safe-Path ([string]$job.target)
    $candidate = Safe-Path ([string]$job.candidate)
    $backup = Safe-Path ([string]$job.backup)
    $parent = [IO.Path]::GetDirectoryName($target)
    $systemRoot = [IO.Path]::GetFullPath($env:WINDIR).TrimEnd('\')
    if ($target -ieq $systemRoot -or $target.StartsWith($systemRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '不能更新系统目录中的目标' }
    $expectedStage = Safe-Path (Join-Path $parent ('.codex-token-update-' + $job.id))
    if ($JobPath -cne (Join-Path $expectedStage 'job.json') -or $candidate -cne (Join-Path $expectedStage 'candidate\CodexTokenDesktop') -or $backup -cne ($target + '.rollback-' + $job.id)) { throw '更新路径不符合任务约束' }
    $stage = $expectedStage
    $originalExe = Safe-Path (Join-Path $target 'CodexTokenDesktop.exe')
    if (-not (Test-Path -LiteralPath $target -PathType Container) -or -not (Test-Path -LiteralPath $originalExe -PathType Leaf)) { throw '旧安装目录或程序文件无效' }
    $pathsValidated = $true
    if (-not (Test-Path -LiteralPath $target -PathType Container) -or -not (Test-Path -LiteralPath $candidate -PathType Container) -or (Test-Path -LiteralPath $backup)) { throw '目标、候选或备份目录状态无效' }
    foreach ($directory in @($target, $candidate)) {
        $exe = Safe-Path (Join-Path $directory 'CodexTokenDesktop.exe')
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "缺少程序文件: $exe" }
    }
    $ack = Join-Path $stage 'ack.json'
    $failed = Join-Path $stage 'failed'
    if ((Test-Path -LiteralPath $ack) -or (Test-Path -LiteralPath $failed) -or (Test-Path -LiteralPath (Join-Path $stage 'result.json')) -or (Test-Path -LiteralPath (Join-Path $stage 'result.tmp'))) { throw '更新任务已使用或存在陈旧确认文件' }
    $oldProcess = Get-Process -Id $job.parent_pid -ErrorAction SilentlyContinue
    if ($oldProcess -and -not $oldProcess.WaitForExit(60000)) { throw '等待旧程序退出超时；安装目录未变更' }
    $parentExited = $true
    if (Test-Path -LiteralPath $backup) { throw '备份目录已出现，停止更新' }
    Move-Item -LiteralPath $target -Destination $backup
    $oldMoved = $true
    if (Test-Path -LiteralPath $target) { throw '目标目录已出现，停止安装' }
    Move-Item -LiteralPath $candidate -Destination $target
    $newMoved = $true
    # Start-Process joins ArgumentList: quote the data argument explicitly.
    if ($JobPath.Contains('"')) { throw '任务路径包含无效引号' }
    $newProcess = Start-Process -FilePath (Join-Path $target 'CodexTokenDesktop.exe') -WorkingDirectory $target -ArgumentList @('--update-job', ('"' + $JobPath + '"')) -WindowStyle Hidden -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    $confirmed = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        $newProcess.Refresh()
        if ($newProcess.HasExited) { throw '新程序在启动确认前退出' }
        if (Test-Path -LiteralPath $ack -PathType Leaf) {
            Safe-Path $ack | Out-Null
            try { $a = Get-Content -LiteralPath $ack -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $a = $null }
            if ($a -and $a.id -ceq $job.id -and ($a.pid -is [int] -or $a.pid -is [long]) -and $a.pid -eq $newProcess.Id) { $confirmed = $true; break }
        }
        Start-Sleep -Milliseconds 250
    }
    $newProcess.Refresh()
    if (-not $confirmed -or $newProcess.HasExited) { throw '新程序启动确认超时或已经退出' }
    Write-Result 'success' '更新完成'
} catch {
    $diagnostic = $_.Exception.Message
    # Persist the failure before any restored window can read its update status.
    try { Write-Result 'error' $diagnostic } catch { [Console]::Error.WriteLine('无法写入更新结果: ' + $_.Exception.Message) }
    if ($oldMoved) {
        try {
            if ($newProcess) {
                $newProcess.Refresh()
                if (-not $newProcess.HasExited) { $newProcess.Kill(); if (-not $newProcess.WaitForExit(10000)) { throw '新程序终止超时' } }
            }
            if ($newMoved) {
                if (Test-Path -LiteralPath $failed) { throw '失败版本保存目录已存在' }
                Move-Item -LiteralPath $target -Destination $failed
            }
            if (Test-Path -LiteralPath $target) { throw '目标目录被其他进程创建，无法恢复' }
            Move-Item -LiteralPath $backup -Destination $target
            Start-Process -FilePath (Join-Path $target 'CodexTokenDesktop.exe') -WindowStyle Hidden | Out-Null
        } catch { $diagnostic += '; 回滚或重启失败: ' + $_.Exception.Message }
    } elseif ($pathsValidated) {
        try {
            $remainingParent = Get-Process -Id $job.parent_pid -ErrorAction SilentlyContinue
            if (-not $remainingParent) {
                $parentExited = $true
                $originalExe = Safe-Path (Join-Path $target 'CodexTokenDesktop.exe')
                if (-not (Test-Path -LiteralPath $originalExe -PathType Leaf)) { throw '旧程序文件已经不存在' }
                Start-Process -FilePath $originalExe -WindowStyle Hidden | Out-Null
            }
        } catch { $diagnostic += '; 旧程序重启失败: ' + $_.Exception.Message }
    }
    try { Write-Result 'error' $diagnostic } catch { [Console]::Error.WriteLine('无法写入更新结果: ' + $_.Exception.Message) }
    [Console]::Error.WriteLine($diagnostic)
    exit 1
}
'''

MACOS_HELPER = r'''#!/bin/sh
# All path data is quoted; never evaluate job values as shell code.
stage=''
new_pid=''
old_moved=0
new_moved=0
paths_validated=0
parent_exited=0
diagnostic=''
json_get() { /usr/bin/plutil -extract "$2" raw -o - "$1" 2>/dev/null; }
parent_alive() { /bin/ps -p "$parent_pid" -o pid= >/dev/null 2>&1; }
new_alive() {
    [ -n "$new_pid" ] || return 1
    process_info=$(/bin/ps -p "$new_pid" -o ppid= -o stat= 2>/dev/null) || return 1
    # ps emits only an integer and status flags; this split contains no paths.
    set -- $process_info
    [ "$#" -eq 2 ] && [ "$1" = "$$" ] || return 1
    case "$2" in Z*) return 1;; esac
    /bin/kill -0 "$new_pid" 2>/dev/null
}
write_result() {
    [ -n "$stage" ] || return 1
    tmp="$stage/result.tmp"
    /usr/bin/plutil -create xml1 "$tmp" &&
    /usr/bin/plutil -insert state -string "$1" "$tmp" &&
    /usr/bin/plutil -insert message -string "$2" "$tmp" || return 1
    if [ "$1" = success ]; then
        /usr/bin/plutil -insert backup -string "$backup" "$tmp" || return 1
    fi
    /usr/bin/plutil -convert json "$tmp" && /bin/mv -f "$tmp" "$stage/result.json"
}
fail() { diagnostic="$1"; return 1; }
physical_dir() { (CDPATH='' cd -P "$1" 2>/dev/null && /bin/pwd -P); }
valid_absolute() {
    case "$1" in /*) ;; *) return 1;; esac
    case "$1" in /|*/|*'/../'*|*'/./'*|*'//'*) return 1;; esac
    [ ! -L "$1" ]
}
run_update() {
    [ "$#" -eq 1 ] || { fail '需要唯一的 job.json 参数'; return 1; }
    job="$1"
    valid_absolute "$job" && [ -f "$job" ] || { fail '任务路径无效'; return 1; }
    id=$(json_get "$job" id) || { fail '任务 JSON 无效'; return 1; }
    case "$id" in *[!0-9a-f]*|'') fail '任务标识无效'; return 1;; esac
    [ "${#id}" -eq 32 ] || { fail '任务标识长度无效'; return 1; }
    platform=$(json_get "$job" platform)
    [ "$platform" = darwin ] || { fail '任务平台无效'; return 1; }
    target=$(json_get "$job" target) && candidate=$(json_get "$job" candidate) && backup=$(json_get "$job" backup) && parent_pid=$(json_get "$job" parent_pid) || { fail '缺少任务字段'; return 1; }
    case "$parent_pid" in ''|*[!0-9]*) fail '父进程标识无效'; return 1;; esac
    [ "$parent_pid" -gt 0 ] 2>/dev/null && [ "$parent_pid" != "$$" ] || { fail '父进程标识无效'; return 1; }
    valid_absolute "$target" && valid_absolute "$candidate" && valid_absolute "$backup" || { fail '安装路径无效'; return 1; }
    case "$target" in *.app) ;; *) fail '目标必须为 .app'; return 1;; esac
    parent=${target%/*}
    [ -n "$parent" ] && [ "$parent" != / ] && [ "$(physical_dir "$parent")" = "$parent" ] || { fail '安装父目录不安全或包含链接'; return 1; }
    case "$parent" in /System|/System/*|/usr|/usr/*|/bin|/bin/*|/sbin|/sbin/*|/dev|/dev/*|/private|/private/*) fail '不能更新系统目录中的目标'; return 1;; esac
    expected_stage="$parent/.codex-token-update-$id"
    [ "$job" = "$expected_stage/job.json" ] && [ "$candidate" = "$expected_stage/candidate/CodexTokenDesktop.app" ] && [ "$backup" = "$target.rollback-$id" ] || { fail '更新路径不符合任务约束'; return 1; }
    [ "$(physical_dir "$expected_stage")" = "$expected_stage" ] && [ "$(physical_dir "$expected_stage/candidate")" = "$expected_stage/candidate" ] || { fail '暂存目录包含链接或不存在'; return 1; }
    stage="$expected_stage"
    [ -d "$target" ] && [ "$(physical_dir "$target/Contents/MacOS")" = "$target/Contents/MacOS" ] && [ ! -L "$target/Contents/MacOS/CodexTokenDesktop" ] && [ -f "$target/Contents/MacOS/CodexTokenDesktop" ] && [ -x "$target/Contents/MacOS/CodexTokenDesktop" ] || { fail '旧安装目录或程序文件无效'; return 1; }
    paths_validated=1
    [ -d "$target" ] && [ -d "$candidate" ] && [ ! -e "$backup" ] && [ ! -L "$backup" ] || { fail '安装、候选或备份目录状态无效'; return 1; }
    for dir in "$target" "$candidate"; do
        [ "$(physical_dir "$dir/Contents/MacOS")" = "$dir/Contents/MacOS" ] && [ ! -L "$dir/Contents/MacOS/CodexTokenDesktop" ] && [ -f "$dir/Contents/MacOS/CodexTokenDesktop" ] && [ -x "$dir/Contents/MacOS/CodexTokenDesktop" ] || { fail '缺少可执行程序或程序路径包含链接'; return 1; }
    done
    ack="$stage/ack.json"
    failed="$stage/failed"
    for unused in "$ack" "$failed" "$stage/result.json" "$stage/result.tmp"; do
        [ ! -e "$unused" ] && [ ! -L "$unused" ] || { fail '更新任务已使用'; return 1; }
    done
    count=0
    while parent_alive; do
        [ "$count" -lt 60 ] || { fail '等待旧程序退出超时；安装目录未变更'; return 1; }
        /bin/sleep 1
        count=$((count + 1))
    done
    parent_exited=1
    [ ! -e "$backup" ] && [ ! -L "$backup" ] && /bin/mv "$target" "$backup" || { fail '无法移动旧安装目录'; return 1; }
    old_moved=1
    [ ! -e "$target" ] && [ ! -L "$target" ] && /bin/mv "$candidate" "$target" || { fail '无法安装候选目录'; return 1; }
    new_moved=1
    (cd "$target" && exec "$target/Contents/MacOS/CodexTokenDesktop" --update-job "$job") >"$stage/new-process.log" 2>&1 &
    new_pid=$!
    count=0
    while [ "$count" -lt 60 ]; do
        new_alive || { fail '新程序在启动确认前退出'; return 1; }
        if [ -f "$ack" ] && [ ! -L "$ack" ]; then
            ack_id=$(json_get "$ack" id)
            ack_pid=$(json_get "$ack" pid)
            if [ "$ack_id" = "$id" ] && [ "$ack_pid" = "$new_pid" ]; then
                new_alive || { fail '新程序已经退出'; return 1; }
                write_result success '更新完成' || { fail '无法写入更新成功结果'; return 1; }
                return 0
            fi
        fi
        /bin/sleep 1
        count=$((count + 1))
    done
    fail '新程序启动确认超时'
}
if run_update "$@"; then exit 0; fi
# Report first so a restarted original application sees the failure immediately.
write_result error "$diagnostic" || /usr/bin/printf '%s\n' '无法写入更新结果' >&2
if [ "$old_moved" -eq 1 ]; then
    stopped=1
    if new_alive; then
        /bin/kill -TERM "$new_pid" 2>/dev/null
        count=0
        while new_alive && [ "$count" -lt 10 ]; do /bin/sleep 1; count=$((count + 1)); done
        if new_alive; then /bin/kill -KILL "$new_pid" 2>/dev/null; fi
        wait "$new_pid" 2>/dev/null
        if new_alive; then stopped=0; fi
    fi
    restored=0
    if [ "$stopped" -eq 1 ]; then
        if [ "$new_moved" -eq 0 ] || { [ ! -e "$failed" ] && [ ! -L "$failed" ] && /bin/mv "$target" "$failed"; }; then
            if [ ! -e "$target" ] && [ ! -L "$target" ] && /bin/mv "$backup" "$target"; then restored=1; fi
        fi
    fi
    if [ "$restored" -eq 1 ]; then
        "$target/Contents/MacOS/CodexTokenDesktop" >"$stage/rollback-process.log" 2>&1 &
        old_restart_pid=$!
        /bin/sleep 1
        /bin/kill -0 "$old_restart_pid" 2>/dev/null || diagnostic="$diagnostic; 旧程序重启失败"
    else
        diagnostic="$diagnostic; 回滚失败，保留目录以便恢复"
    fi
elif [ "$paths_validated" -eq 1 ] && ! parent_alive; then
    parent_exited=1
    if [ "$(physical_dir "$target/Contents/MacOS")" = "$target/Contents/MacOS" ] && [ ! -L "$target/Contents/MacOS/CodexTokenDesktop" ] && [ -f "$target/Contents/MacOS/CodexTokenDesktop" ] && [ -x "$target/Contents/MacOS/CodexTokenDesktop" ]; then
        "$target/Contents/MacOS/CodexTokenDesktop" >"$stage/rollback-process.log" 2>&1 &
        old_restart_pid=$!
        /bin/sleep 1
        /bin/kill -0 "$old_restart_pid" 2>/dev/null || diagnostic="$diagnostic; 旧程序重启失败"
    else
        diagnostic="$diagnostic; 旧程序路径已经变化，无法重启"
    fi
fi
write_result error "$diagnostic" || /usr/bin/printf '%s\n' '无法写入更新结果' >&2
/usr/bin/printf '%s\n' "$diagnostic" >&2
exit 1
'''
