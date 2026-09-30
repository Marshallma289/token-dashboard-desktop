[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'release'),
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
& $Python (Join-Path $PSScriptRoot 'build_desktop.py') --output-dir $OutputDirectory
if ($LASTEXITCODE -ne 0) { throw "Desktop build failed (exit code $LASTEXITCODE)." }
