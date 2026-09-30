[CmdletBinding()]
param(
    [switch]$NoOpen,
    [int]$Port = 8765,
    [switch]$CheckOnly,
    [switch]$Web,
    [switch]$Portable
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Add-PythonCandidate {
    param(
        [System.Collections.Generic.List[object]]$Candidates,
        [string]$Command,
        [string[]]$Arguments,
        [string]$Label
    )

    if (-not [string]::IsNullOrWhiteSpace($Command)) {
        $Candidates.Add([pscustomobject]@{
            Command = $Command
            Arguments = @($Arguments)
            Label = $Label
        })
    }
}

function Find-CompatiblePython {
    $candidates = [System.Collections.Generic.List[object]]::new()

    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_DASHBOARD_PYTHON)) {
        Add-PythonCandidate $candidates $env:CODEX_DASHBOARD_PYTHON @() 'CODEX_DASHBOARD_PYTHON'
    }

    $pyLauncherPath = $null
    $pyLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        $pyLauncherPath = $pyLauncher.Source
    }
    else {
        foreach ($knownLauncher in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher\py.exe'),
            (Join-Path $env:SystemRoot 'py.exe')
        )) {
            if (Test-Path -LiteralPath $knownLauncher) {
                $pyLauncherPath = $knownLauncher
                break
            }
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($pyLauncherPath)) {
        foreach ($version in @('-3.14', '-3.13', '-3.12', '-3.11', '-3.10', '-3')) {
            Add-PythonCandidate $candidates $pyLauncherPath @($version) "Python launcher $version"
        }
    }

    foreach ($name in @('python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command -and $command.Source -notmatch '\\WindowsApps\\') {
            Add-PythonCandidate $candidates $command.Source @() $name
        }
    }

    $localPythonPattern = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe'
    foreach ($localPython in Get-ChildItem -Path $localPythonPattern -File -ErrorAction SilentlyContinue | Sort-Object FullName -Descending) {
        Add-PythonCandidate $candidates $localPython.FullName @() 'User Python'
    }

    $runtimeRoot = Join-Path $env:USERPROFILE '.cache\codex-runtimes'
    if (Test-Path -LiteralPath $runtimeRoot) {
        $runtimePattern = Join-Path $runtimeRoot '*\dependencies\python\python.exe'
        foreach ($runtime in Get-ChildItem -Path $runtimePattern -File -ErrorAction SilentlyContinue) {
            Add-PythonCandidate $candidates $runtime.FullName @() 'Codex bundled Python'
        }
    }

    $probe = 'import sys; print(sys.executable); raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
    foreach ($candidate in $candidates) {
        try {
            $resolved = & $candidate.Command @($candidate.Arguments) -c $probe 2>$null
            if ($LASTEXITCODE -eq 0) {
                return [pscustomobject]@{
                    Command = $candidate.Command
                    Arguments = @($candidate.Arguments)
                    Label = $candidate.Label
                    Resolved = ($resolved | Select-Object -Last 1)
                }
            }
        }
        catch {
            continue
        }
    }

    return $null
}

try {
    Set-Location -LiteralPath $PSScriptRoot
    Write-Host 'Starting Codex Token Dashboard...' -ForegroundColor Cyan

    if (-not $CheckOnly -and -not $Web -and $Portable) {
        $desktopExe = Join-Path $PSScriptRoot 'dist\CodexTokenDesktop\CodexTokenDesktop.exe'
        if (Test-Path -LiteralPath $desktopExe) {
            Start-Process -FilePath $desktopExe
            exit 0
        }
        throw 'The portable application was not found at dist\CodexTokenDesktop\CodexTokenDesktop.exe. Start without -Portable to use current source.'
    }

    if (-not $CheckOnly -and $Web) {
        $health = $null
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 1
        }
        catch {
            # No compatible local dashboard was found; continue with a normal start.
        }

        if ($null -ne $health) {
            if ($health.service -ne 'codex-token-dashboard') {
                throw "Port $Port is already used by another service."
            }
            $url = "http://127.0.0.1:$Port"
            Write-Host "Dashboard is already running. Opening $url" -ForegroundColor Green
            if (-not $NoOpen) {
                Start-Process $url
            }
            exit 0
        }
    }

    $python = Find-CompatiblePython
    if ($null -eq $python) {
        throw 'Python 3.10 or newer was not found. Install Python or set CODEX_DASHBOARD_PYTHON to python.exe.'
    }

    Write-Host "Using Python ($($python.Label)): $($python.Resolved)" -ForegroundColor DarkGray
    $backend = Join-Path $PSScriptRoot 'backend.py'

    if ($CheckOnly) {
        & $python.Command @($python.Arguments) $backend doctor
    }
    elseif (-not $Web) {
        $desktop = Join-Path $PSScriptRoot 'desktop.py'
        $pythonWindowed = Join-Path (Split-Path $python.Resolved) 'pythonw.exe'
        if (Test-Path -LiteralPath $pythonWindowed) {
            Start-Process -FilePath $pythonWindowed -ArgumentList ('"' + $desktop + '"')
            exit 0
        }
        & $python.Command @($python.Arguments) $desktop
    }
    else {
        $serverArguments = @($backend, 'serve', '--port', [string]$Port)
        if ($NoOpen) {
            $serverArguments += '--no-open'
        }
        Write-Host "Dashboard URL: http://127.0.0.1:$Port" -ForegroundColor Green
        Write-Host 'Close this window to stop the local service.' -ForegroundColor DarkGray
        & $python.Command @($python.Arguments) @serverArguments
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Dashboard process exited unexpectedly (code $LASTEXITCODE)."
    }
    exit 0
}
catch {
    Write-Host ''
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
