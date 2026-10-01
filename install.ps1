# Windows x64, current-user installation. Compatible with Windows PowerShell 5.1.
# Download this script from the repository and invoke with & to pass parameters.
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\CodexTokenDesktop'),
    [switch]$NoLaunch,
    [switch]$NoShortcut
)
$ErrorActionPreference = 'Stop'
$repo = 'Marshallma289/token-dashboard-desktop'
$bootstrap = "https://github.com/$repo/releases/latest/download/update.json"
$assetBase = "https://github.com/$repo/releases/download/"
$script:releaseBase = $null
$maxArchive = 250MB
$maxExpanded = 512MB
$stage = $null
$stageCreated = $false
$backup = $null
$installed = $false
$installLock = $null
$installLockPath = $null

function Read-Json([string]$Path) {
    $value = [IO.File]::ReadAllText($Path) | ConvertFrom-Json
    if ($null -eq $value -or $value -isnot [pscustomobject]) { throw 'Invalid JSON object.' }
    return $value
}
function Get-Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Hash-Stream($Stream) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Canonical-Json($Value) {
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [pscustomobject]) {
        $parts = foreach ($property in ($Value.PSObject.Properties | Sort-Object Name)) {
            ($property.Name | ConvertTo-Json -Compress) + ':' + (Canonical-Json $property.Value)
        }
        return '{' + ($parts -join ',') + '}'
    }
    if ($Value -is [array]) {
        $parts = foreach ($item in $Value) { Canonical-Json $item }
        return '[' + ($parts -join ',') + ']'
    }
    return ($Value | ConvertTo-Json -Compress)
}
function Test-Within([string]$Child, [string]$Root) {
    return $Child.Equals($Root, [StringComparison]::OrdinalIgnoreCase) -or $Child.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)
}
function Assert-NoLinks([string]$Path) {
    $cursor = $Path
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Installation paths cannot contain links or junctions.' }
        }
        $next = [IO.Path]::GetDirectoryName($cursor)
        if ($next -eq $cursor) { break }
        $cursor = $next
    }
}
function Assert-Target([string]$Target) {
    if ($Target -notmatch '^[A-Za-z]:\\' -or $Target -match '[<>"|?*]' -or $Target.Substring(2).Contains(':')) { throw 'Use a local absolute installation path.' }
    $root = [IO.Path]::GetPathRoot($Target)
    if ($Target.TrimEnd('\') -eq $root.TrimEnd('\')) { throw 'A drive root is not an installation directory.' }
    foreach ($part in $Target.Substring(3).Split('\')) {
        if (!$part -or $part.EndsWith(' ') -or $part.EndsWith('.') -or $part -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])($|\.)') { throw 'Unsafe installation directory name.' }
    }
    Assert-NoLinks $Target
    $protected = @($env:WINDIR, $env:USERPROFILE, $env:LOCALAPPDATA, $env:APPDATA, $env:ProgramData, $env:ProgramFiles, ${env:ProgramFiles(x86)}, [Environment]::GetFolderPath('DesktopDirectory'), [Environment]::GetFolderPath('MyDocuments'), [Environment]::GetFolderPath('MyPictures'), [Environment]::GetFolderPath('MyMusic'), [Environment]::GetFolderPath('MyVideos'), [Environment]::GetFolderPath('Programs'))
    foreach ($path in $protected) {
        if ($path -and $Target.Equals([IO.Path]::GetFullPath($path).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) { throw 'A system or user profile root cannot be the installation directory.' }
    }
    foreach ($path in @($env:WINDIR, (Join-Path $env:LOCALAPPDATA 'CodexTokenDashboard'), (Join-Path $env:USERPROFILE '.codex'), $env:CODEX_HOME)) {
        if ($path -and (Test-Within $Target ([IO.Path]::GetFullPath($path).TrimEnd('\')))) { throw 'The installation directory overlaps protected system or user data.' }
    }
    $dataPath = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'CodexTokenDashboard')).TrimEnd('\')
    if (Test-Within $dataPath $Target) { throw 'The installation directory contains the application data directory.' }
}
function Assert-NotRunning {
    $running = @(Get-Process -Name 'CodexTokenDesktop' -ErrorAction SilentlyContinue)
    foreach ($process in $running) {
        try { $processPath = $process.Path }
        catch { throw 'Cannot identify a running Codex Token Dashboard process. Close it before installing.' }
        if (!$processPath) { throw 'Cannot identify a running Codex Token Dashboard process. Close it before installing.' }
        if (Test-Within ([IO.Path]::GetFullPath($processPath)) $target) { throw 'Close Codex Token Dashboard in this installation directory before installing or upgrading.' }
    }
}
function Assert-UpdateIdle([string]$LockPath) {
    if (!(Test-Path -LiteralPath $LockPath)) { return }
    Assert-NoLinks $LockPath
    if (!(Test-Path -LiteralPath $LockPath -PathType Leaf) -or (Get-Item -LiteralPath $LockPath).Length -gt 64KB) { throw 'Unrecognized application update lock. Inspect it before retrying.' }
    try { $lock = Read-Json $LockPath }
    catch { throw 'Unreadable application update lock. Inspect it before retrying.' }
    if ($lock.pid -notmatch '^\d+$' -or [long]$lock.pid -le 0 -or [long]$lock.pid -gt [int]::MaxValue) { throw 'Invalid application update lock PID. Inspect it before retrying.' }
    if (Get-Process -Id ([int]$lock.pid) -ErrorAction SilentlyContinue) { throw 'An application updater is still running. Wait for it to finish.' }
    # This exact lock belongs to this validated local installation parent.
    Assert-NoLinks $LockPath
    Remove-Item -LiteralPath $LockPath -Force
}
function Get-Download([string]$Url, [string]$Destination, [long]$Limit) {
    if ($Url -cne $bootstrap -and (!$script:releaseBase -or !$Url.StartsWith($script:releaseBase, [StringComparison]::Ordinal))) { throw 'Download source is not the configured GitHub release.' }
    $current = [Uri]$Url
    for ($redirect = 0; $redirect -le 10; $redirect++) {
        if ($current.Scheme -ne 'https' -or $current.UserInfo -or !$current.IsDefaultPort -or !($current.Host -eq 'github.com' -or $current.Host.EndsWith('.githubusercontent.com'))) { throw 'Unsafe download redirect.' }
        if ($current.Host -eq 'github.com') {
            $githubUrl = $current.AbsoluteUri
            if ($githubUrl -cne $bootstrap) {
                if ($Url -ceq $bootstrap) {
                    $tagPattern = '^' + [Regex]::Escape($assetBase) + '([^/?#]+)/update\.json$'
                    $tagMatch = [Regex]::Match($githubUrl, $tagPattern)
                    if (!$tagMatch.Success) { throw 'Latest release redirected outside the configured release asset.' }
                    $resolvedBase = $assetBase + $tagMatch.Groups[1].Value + '/'
                    if ($script:releaseBase -and $script:releaseBase -cne $resolvedBase) { throw 'Latest release changed during the download.' }
                    $script:releaseBase = $resolvedBase
                } elseif ($githubUrl -cne $Url) { throw 'Release asset redirected to a different GitHub asset.' }
            } elseif ($Url -cne $bootstrap) { throw 'A fixed release asset redirected to latest.' }
        } elseif ($Url -ceq $bootstrap -and !$script:releaseBase) { throw 'Latest release tag was not resolved before downloading.' }
        $request = [Net.HttpWebRequest]::Create($current)
        $request.AllowAutoRedirect = $false
        $request.UserAgent = 'CodexTokenDashboard-Installer'
        $request.Accept = 'application/octet-stream'
        $request.Timeout = 30000
        $request.ReadWriteTimeout = 30000
        $response = $request.GetResponse()
        try {
            if ([int]$response.StatusCode -ge 300 -and [int]$response.StatusCode -lt 400) {
                if (!$response.Headers['Location']) { throw 'Invalid download redirect.' }
                $current = [Uri]::new($current, $response.Headers['Location'])
                continue
            }
            if ($response.ContentLength -gt $Limit) { throw 'Download exceeds size limit.' }
            $inputStream = $response.GetResponseStream()
            $outputStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew)
            try {
                $buffer = New-Object byte[] 131072
                [long]$total = 0
                while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $total += $count
                    if ($total -gt $Limit) { throw 'Download exceeds size limit.' }
                    $outputStream.Write($buffer, 0, $count)
                }
            } finally { $outputStream.Dispose(); $inputStream.Dispose() }
            return
        } finally { $response.Close() }
    }
    throw 'Too many download redirects.'
}
function Assert-RelativePath([string]$Path) {
    if (!$Path -or $Path.StartsWith('/') -or $Path.Contains('\') -or $Path.Contains(':') -or $Path -match '[\x00-\x1f<>"|?*]') { throw 'Archive contains an unsafe path.' }
    foreach ($part in $Path.Split('/')) {
        if (!$part -or $part -eq '.' -or $part -eq '..' -or $part.EndsWith(' ') -or $part.EndsWith('.') -or $part -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])($|\.)') { throw 'Archive contains an unsafe Windows filename.' }
    }
}
function Expand-ValidatedArchive([string]$Archive, $Manifest, [string]$Destination) {
    if ($Manifest.files -isnot [array] -or $Manifest.files.Count -gt 20000) { throw 'Invalid file manifest.' }
    $declared = New-Object 'Collections.Generic.Dictionary[string,object]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($record in $Manifest.files) {
        Assert-RelativePath $record.path
        if ($record.type -eq 'symlink' -or $record.sha256 -cnotmatch '^[0-9a-f]{64}$' -or $record.path -eq 'build-manifest.json' -or $declared.ContainsKey($record.path)) { throw 'Invalid or duplicate manifest record.' }
        $declared.Add($record.path, $record)
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        if ($zip.Entries.Count -gt 40000) { throw 'Archive contains too many entries.' }
        $nodes = New-Object 'Collections.Generic.Dictionary[string,bool]' ([StringComparer]::OrdinalIgnoreCase)
        $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        [long]$expanded = 0
        $internalManifest = $false
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            $isDir = $name.EndsWith('/')
            $canonical = $name
            if ($isDir) { $canonical = $name.Substring(0, $name.Length - 1) }
            Assert-RelativePath $canonical
            if (!$name.StartsWith('CodexTokenDesktop/', [StringComparison]::Ordinal)) { throw 'Archive has an unexpected root directory.' }
            if ($nodes.ContainsKey($canonical)) { throw 'Archive contains duplicate paths.' }
            $nodes.Add($canonical, $isDir)
            $unixType = (($entry.ExternalAttributes -shr 16) -band 0xF000)
            if ($unixType -eq 0xA000 -or ($unixType -ne 0 -and $unixType -ne 0x8000 -and $unixType -ne 0x4000) -or ($entry.ExternalAttributes -band 0x400) -ne 0) { throw 'Archive contains a link or special file.' }
            $expanded += $entry.Length
            if ($expanded -gt $maxExpanded) { throw 'Expanded archive exceeds size limit.' }
            if ($isDir) { if ($entry.Length -ne 0) { throw 'Invalid directory entry.' }; continue }
            $relative = $name.Substring('CodexTokenDesktop/'.Length)
            $stream = $entry.Open()
            try {
                if ($relative -ceq 'build-manifest.json') {
                    if ($entry.Length -gt 2MB) { throw 'Invalid internal manifest size.' }
                    $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
                    try { $inside = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
                    if ((Canonical-Json $inside) -cne (Canonical-Json $Manifest)) { throw 'Internal manifest does not match release manifest.' }
                    $internalManifest = $true
                } else {
                    if (!$declared.ContainsKey($relative) -or $declared[$relative].path -cne $relative -or (Hash-Stream $stream) -cne $declared[$relative].sha256) { throw 'Archive file hash or manifest declaration mismatch.' }
                    [void]$seen.Add($relative)
                }
            } finally { $stream.Dispose() }
        }
        if (!$internalManifest -or $seen.Count -ne $declared.Count) { throw 'Archive is missing required files.' }
        foreach ($name in $nodes.Keys) {
            $parentName = $name
            while ($parentName.Contains('/')) {
                $parentName = $parentName.Substring(0, $parentName.LastIndexOf('/'))
                if ($nodes.ContainsKey($parentName) -and !$nodes[$parentName]) { throw 'Archive contains file/directory conflicts.' }
            }
        }
        # All entries and file contents have been checked before any extraction.
        [void][IO.Directory]::CreateDirectory($Destination)
        foreach ($entry in $zip.Entries) {
            $output = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName.Replace('/', '\')))
            if (!(Test-Within $output $Destination)) { throw 'Extraction path escaped staging.' }
            if ($entry.FullName.EndsWith('/')) { [void][IO.Directory]::CreateDirectory($output); continue }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($output))
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $output, $false)
        }
    } finally { $zip.Dispose() }
}
function Remove-OwnedStage {
    if (!$stageCreated -or !$stage -or !(Test-Path -LiteralPath $stage)) { return }
    $full = [IO.Path]::GetFullPath($stage)
    if ([IO.Path]::GetDirectoryName($full) -cne $parent -or [IO.Path]::GetFileName($full) -cnotmatch '^\.codex-token-install-[0-9a-f]{32}$') { throw 'Refusing to clean an unrecognized staging directory.' }
    Assert-NoLinks $full
    foreach ($item in Get-ChildItem -LiteralPath $full -Force -Recurse) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Refusing to clean staging containing links.' }
    }
    Remove-Item -LiteralPath $full -Recurse -Force
}

try {
    if (![Environment]::Is64BitOperatingSystem -or ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64')) { throw 'This installer requires Windows x64.' }
    if (!$env:LOCALAPPDATA -or !$env:USERPROFILE) { throw 'Current-user profile paths are unavailable.' }
    $rawTarget = $InstallDir.Replace('/', '\')
    if ($rawTarget -notmatch '^[A-Za-z]:\\' -or $rawTarget -match '(^|\\)\.\.?($|\\)') { throw 'Use an absolute local installation path without dot segments.' }
    $target = [IO.Path]::GetFullPath($rawTarget).TrimEnd('\')
    Assert-Target $target
    Assert-NotRunning
    $parent = [IO.Path]::GetDirectoryName($target)
    if (Test-Path -LiteralPath $target) {
        if (!(Test-Path -LiteralPath $target -PathType Container)) { throw 'Installation target is not a directory.' }
        $existing = @(Get-ChildItem -LiteralPath $target -Force)
        if ($existing.Count) {
            $oldInfoPath = Join-Path $target '_internal\build-info.json'
            $oldManifestPath = Join-Path $target 'build-manifest.json'
            if (!(Test-Path -LiteralPath (Join-Path $target 'CodexTokenDesktop.exe') -PathType Leaf) -or !(Test-Path -LiteralPath $oldInfoPath -PathType Leaf) -or !(Test-Path -LiteralPath $oldManifestPath -PathType Leaf)) { throw 'The target contains unrecognized files. Choose an empty installation directory.' }
            Assert-NoLinks $oldInfoPath
            Assert-NoLinks $oldManifestPath
            $oldInfo = Read-Json $oldInfoPath
            $oldManifest = Read-Json $oldManifestPath
            if ($oldManifest.platform -ne 'win32' -or $oldManifest.architecture -ne 'x64' -or $oldInfo.version -ne $oldManifest.version -or $oldInfo.source_commit -ne $oldManifest.source_commit -or $oldInfo.version -notmatch '^\d+\.\d+\.\d+$' -or $oldInfo.source_commit -cnotmatch '^[0-9a-f]{40}$') { throw 'Existing installation identity is invalid.' }
        }
    }
    [void][IO.Directory]::CreateDirectory($parent)
    Assert-NoLinks $parent
    $installLockPath = Join-Path $parent ('.' + [IO.Path]::GetFileName($target) + '.install.lock')
    try { $installLock = [IO.File]::Open($installLockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None) }
    catch { throw 'Another installer may be active. Check the installation lock before retrying.' }
    $lockPath = Join-Path $parent ('.' + [IO.Path]::GetFileName($target) + '.update.lock')
    Assert-UpdateIdle $lockPath
    $id = [Guid]::NewGuid().ToString('N')
    $stage = Join-Path $parent ('.codex-token-install-' + $id)
    if (Test-Path -LiteralPath $stage) { throw 'Staging directory already exists.' }
    [void][IO.Directory]::CreateDirectory($stage)
    $stageCreated = $true
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Write-Host 'Finding the latest GitHub release...'
    $updatePath = Join-Path $stage 'update.json'
    Get-Download $bootstrap $updatePath 2MB
    if (!$script:releaseBase) { throw 'Could not resolve a fixed GitHub release tag.' }
    $update = Read-Json $updatePath
    if ($update.schema -ne 1 -or $update.version -cnotmatch '^\d+\.\d+\.\d+$' -or $update.source_commit -cnotmatch '^[0-9a-f]{40}$' -or $update.source_digest -cnotmatch '^[0-9a-f]{64}$' -or $update.build_number -notmatch '^\d+$') { throw 'Invalid release update metadata.' }
    $package = $update.packages.'windows-x64'
    if ($null -eq $package -or $package.sha256 -cnotmatch '^[0-9a-f]{64}$' -or $package.manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or $package.size -notmatch '^\d+$' -or [long]$package.size -le 0 -or [long]$package.size -gt $maxArchive -or $package.archive -isnot [string] -or $package.manifest -isnot [string] -or $package.archive -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$' -or $package.manifest -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') { throw 'Invalid Windows package metadata.' }
    Write-Host ('Downloading version ' + $update.version + '...')
    $archivePath = Join-Path $stage 'package.zip'
    Get-Download ($script:releaseBase + [Uri]::EscapeDataString($package.archive)) $archivePath $maxArchive
    if ((Get-Item -LiteralPath $archivePath).Length -ne [long]$package.size -or (Get-Hash $archivePath) -cne $package.sha256) { throw 'Package size or SHA256 mismatch.' }
    $manifestPath = Join-Path $stage 'manifest.json'
    Get-Download ($script:releaseBase + [Uri]::EscapeDataString($package.manifest)) $manifestPath 2MB
    if ((Get-Hash $manifestPath) -cne $package.manifest_sha256) { throw 'Manifest SHA256 mismatch.' }
    $manifest = Read-Json $manifestPath
    if ($manifest.platform -ne 'win32' -or $manifest.architecture -ne 'x64' -or $manifest.version -cne $update.version -or $manifest.source_commit -cne $update.source_commit -or $manifest.source_digest -cne $update.source_digest) { throw 'Manifest does not match the release.' }
    Write-Host 'Verifying every archive file...'
    $candidateRoot = Join-Path $stage 'candidate'
    Expand-ValidatedArchive $archivePath $manifest $candidateRoot
    $candidate = Join-Path $candidateRoot 'CodexTokenDesktop'
    $info = Read-Json (Join-Path $candidate '_internal\build-info.json')
    if ($info.version -cne $update.version -or $info.source_commit -cne $update.source_commit -or $info.source_digest -cne $update.source_digest -or $info.build_number -ne $update.build_number -or !(Test-Path -LiteralPath (Join-Path $candidate 'CodexTokenDesktop.exe') -PathType Leaf)) { throw 'Candidate application identity mismatch.' }
    $portable = Join-Path $target 'providers.json'
    if (Test-Path -LiteralPath $portable) {
        Assert-NoLinks $portable
        if (!(Test-Path -LiteralPath $portable -PathType Leaf)) { throw 'Portable provider configuration is not a file.' }
        Copy-Item -LiteralPath $portable -Destination (Join-Path $candidate 'providers.json') -Force
    }
    Assert-Target $target
    Assert-NotRunning
    Assert-UpdateIdle $lockPath
    $backup = Join-Path $parent ([IO.Path]::GetFileName($target) + '.oldbackup-' + $id)
    $movedOld = $false
    try {
        if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $backup; $movedOld = $true }
        Move-Item -LiteralPath $candidate -Destination $target
        $installed = $true
    } catch {
        if ($movedOld -and !(Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $backup)) { Move-Item -LiteralPath $backup -Destination $target }
        throw
    }
    $exe = Join-Path $target 'CodexTokenDesktop.exe'
    if (!$NoShortcut) {
        try {
            $shell = New-Object -ComObject WScript.Shell
            foreach ($folder in @([Environment]::GetFolderPath('DesktopDirectory'), [Environment]::GetFolderPath('Programs'))) {
                if (!$folder) { throw 'Current-user shortcut folder is unavailable.' }
                [void][IO.Directory]::CreateDirectory($folder)
                $shortcut = $shell.CreateShortcut((Join-Path $folder 'Codex Token Dashboard.lnk'))
                $shortcut.TargetPath = $exe
                $shortcut.WorkingDirectory = $target
                $shortcut.IconLocation = $exe + ',0'
                $shortcut.Save()
            }
        } catch { Write-Warning ('Installed successfully; could not create shortcuts: ' + $_.Exception.Message) }
    }
    Write-Host ('Installed: ' + $target)
    if (Test-Path -LiteralPath $backup) { Write-Host ('Previous installation retained: ' + $backup) }
    if (!$NoLaunch) {
        try { Start-Process -FilePath $exe -WorkingDirectory $target }
        catch { Write-Warning ('Installed successfully; could not launch: ' + $_.Exception.Message) }
    }
} catch {
    Write-Error ('Installation failed: ' + $_.Exception.Message)
} finally {
    try { Remove-OwnedStage } catch { Write-Warning ('Staging retained for safety: ' + $_.Exception.Message) }
    if ($null -ne $installLock) {
        $installLock.Dispose()
        try { Assert-NoLinks $installLockPath; Remove-Item -LiteralPath $installLockPath -Force }
        catch { Write-Warning ('Installation lock retained: ' + $_.Exception.Message) }
    }
}
