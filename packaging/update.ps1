# Applies an update package to an installed rps_tools server.
#
# Only files whose content differs are replaced. The workspace, the service
# wrapper and its XML are never touched. Any failure restores the previous files.
#
# Keep this file ASCII: Windows PowerShell 5.1 misreads UTF-8 without a BOM.
[CmdletBinding()]
param(
    [string]$InstallDir = 'E:\rps_tools_server',
    [string]$ServiceName = 'rps_tools',
    [int]$Port = 8765,
    [int]$HealthSeconds = 60,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ManifestName = 'rps_tools_release.json'
$ExeName = 'rps_tools_server.exe'
$ProcessName = 'rps_tools_server'

function Info([string]$Message) { Write-Host "[update] $Message" }

function Fail([string]$Message) {
    Write-Host "[update] FAILED: $Message" -ForegroundColor Red
    exit 1
}

function Get-Sha256([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Read-Manifest([string]$Path) {
    $table = @{}
    $json = [IO.File]::ReadAllText($Path) | ConvertFrom-Json
    foreach ($entry in $json.files.PSObject.Properties) {
        $table[$entry.Name.Replace('/', '\')] = $entry.Value
    }
    $table
}

function Test-ManagedPath([string]$Rel) {
    if ([IO.Path]::IsPathRooted($Rel) -or $Rel -match '(^|\\)\.\.(\\|$)') { return $false }
    if ($Rel -like 'rps_tools\workspace\*') { return $false }
    $Rel -notin @('rps_tools_service.exe', 'rps_tools_service.xml', $ManifestName)
}

function Get-ActiveJobCount {
    try {
        $jobs = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/jobs?limit=1000" -TimeoutSec 5
        @($jobs | Where-Object { $_.status -in @('queued', 'running') }).Count
    } catch {
        0
    }
}

function Get-ServerProcesses {
    @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($InstallRoot + '\', [StringComparison]::OrdinalIgnoreCase)
    })
}

function Stop-ServerProcesses {
    foreach ($process in Get-ServerProcesses) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 60; $i++) {
        if ((Get-ServerProcesses).Count -eq 0) { return }
        Start-Sleep -Milliseconds 500
    }
    throw 'Server processes did not exit.'
}

function Wait-Healthy {
    for ($i = 0; $i -lt $HealthSeconds; $i++) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/meta" -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return $true }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    $false
}

# Returns the paths it could not restore. One stuck file must not stop the others.
function Restore-Backup {
    # Nothing touched means the backup may still belong to an earlier update.
    if ($Touched.Count -eq 0) { return }
    foreach ($rel in $Touched) {
        $saved = Join-Path $BackupFiles $rel
        $destination = Join-Path $InstallRoot $rel
        try {
            if (Test-Path -LiteralPath $saved -PathType Leaf) {
                if ((Test-Path -LiteralPath $destination -PathType Leaf) -and (Get-Sha256 $destination) -eq (Get-Sha256 $saved)) { continue }
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
                Copy-Item -LiteralPath $saved -Destination $destination -Force
            } elseif (Test-Path -LiteralPath $destination -PathType Leaf) {
                Remove-Item -LiteralPath $destination -Force
            }
        } catch {
            $rel
        }
    }
    try {
        $savedManifest = Join-Path $Backup $ManifestName
        if (Test-Path -LiteralPath $savedManifest -PathType Leaf) {
            Copy-Item -LiteralPath $savedManifest -Destination $InstalledManifest -Force
        } elseif (Test-Path -LiteralPath $InstalledManifest -PathType Leaf) {
            Remove-Item -LiteralPath $InstalledManifest -Force
        }
    } catch {
        $ManifestName
    }
}

function Start-ServerService {
    Start-Service -Name $ServiceName
    (Get-Service -Name $ServiceName).WaitForStatus('Running', '00:00:30')
}

function Undo-Update([string]$Reason) {
    $notRestored = @(Restore-Backup)
    $restartFailed = $false
    if ($ServiceWasRunning) {
        try { Start-ServerService } catch { $restartFailed = $true }
    }
    $message = $Reason
    if ($notRestored.Count -gt 0) {
        $message += " Could not restore: $($notRestored -join ', '). Copies are in $Backup."
    } else {
        $message += ' The previous version was restored.'
    }
    if ($restartFailed) { $message += " The service could not be restarted; start $ServiceName manually." }
    Fail $message
}

# ---------------------------------------------------------------- checks: nothing changes here
$PackageManifest = Join-Path $PSScriptRoot $ManifestName
$PackageFiles = Join-Path $PSScriptRoot 'files'
if (-not (Test-Path -LiteralPath $PackageManifest -PathType Leaf)) { Fail "Package manifest is missing: $PackageManifest" }
if (-not (Test-Path -LiteralPath (Join-Path $InstallDir $ExeName) -PathType Leaf)) { Fail "No installed server found in $InstallDir" }

$InstallRoot = (Resolve-Path -LiteralPath $InstallDir).Path.TrimEnd('\')
$InstalledManifest = Join-Path $InstallRoot $ManifestName
$Backup = $InstallRoot + '_update_backup'
$BackupFiles = Join-Path $Backup 'files'

$Target = Read-Manifest $PackageManifest
$Previous = @{}
if (Test-Path -LiteralPath $InstalledManifest -PathType Leaf) { $Previous = Read-Manifest $InstalledManifest }

$ToCopy = New-Object System.Collections.ArrayList
foreach ($rel in $Target.Keys) {
    if (-not (Test-ManagedPath $rel)) { Fail "Package lists a protected or unsafe path: $rel" }
    $destination = Join-Path $InstallRoot $rel
    if ((Test-Path -LiteralPath $destination -PathType Leaf) -and (Get-Sha256 $destination) -eq $Target[$rel]) { continue }
    $source = Join-Path $PackageFiles $rel
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        Fail "$rel differs on this server but is not in the package. The server is not at the expected version; build a package from the deployed manifest."
    }
    if ((Get-Sha256 $source) -ne $Target[$rel]) { Fail "Package file is corrupted: $rel" }
    [void]$ToCopy.Add($rel)
}

$ToDelete = New-Object System.Collections.ArrayList
foreach ($rel in $Previous.Keys) {
    if ($Target.ContainsKey($rel) -or -not (Test-ManagedPath $rel)) { continue }
    if (Test-Path -LiteralPath (Join-Path $InstallRoot $rel) -PathType Leaf) { [void]$ToDelete.Add($rel) }
}

if ($ToCopy.Count -eq 0 -and $ToDelete.Count -eq 0) {
    Copy-Item -LiteralPath $PackageManifest -Destination $InstalledManifest -Force
    Info 'Already up to date. Nothing was replaced.'
    exit 0
}

$active = Get-ActiveJobCount
if ($active -gt 0 -and -not $Force) {
    Fail "$active job(s) are queued or running. Wait for them to finish, or run again with -Force to interrupt them."
}

$Service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$ServiceWasRunning = ($null -ne $Service) -and ($Service.Status -eq 'Running')
if ($null -ne $Service) {
    $identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail 'Administrator rights are required to stop the service. Use update.bat.'
    }
}

# ---------------------------------------------------------------- apply
Info ("Replacing {0} file(s), removing {1} file(s)." -f $ToCopy.Count, $ToDelete.Count)
$Touched = New-Object System.Collections.ArrayList
try {
    if ($ServiceWasRunning) {
        Info 'Stopping service...'
        Stop-Service -Name $ServiceName -Force
        (Get-Service -Name $ServiceName).WaitForStatus('Stopped', '00:00:30')
    }
    $BareServer = (Get-ServerProcesses).Count -gt 0 -and -not $ServiceWasRunning
    Stop-ServerProcesses

    if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $BackupFiles | Out-Null
    if (Test-Path -LiteralPath $InstalledManifest -PathType Leaf) {
        Copy-Item -LiteralPath $InstalledManifest -Destination (Join-Path $Backup $ManifestName) -Force
    }

    foreach ($rel in @($ToCopy) + @($ToDelete)) {
        $current = Join-Path $InstallRoot $rel
        if (Test-Path -LiteralPath $current -PathType Leaf) {
            $saved = Join-Path $BackupFiles $rel
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $saved) | Out-Null
            Copy-Item -LiteralPath $current -Destination $saved -Force
        }
    }

    foreach ($rel in $ToCopy) {
        $destination = Join-Path $InstallRoot $rel
        [void]$Touched.Add($rel)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath (Join-Path $PackageFiles $rel) -Destination $destination -Force
        if ((Get-Sha256 $destination) -ne $Target[$rel]) { throw "Copy verification failed: $rel" }
        Info "  replaced $rel"
    }
    foreach ($rel in $ToDelete) {
        [void]$Touched.Add($rel)
        Remove-Item -LiteralPath (Join-Path $InstallRoot $rel) -Force
        Info "  removed  $rel"
    }
    Copy-Item -LiteralPath $PackageManifest -Destination $InstalledManifest -Force
} catch {
    $reason = $_.Exception.Message
    Info 'Update failed. Restoring previous files...'
    Undo-Update $reason
}

# ---------------------------------------------------------------- restart
if ($ServiceWasRunning) {
    Info 'Starting service...'
    $healthy = $false
    try {
        Start-ServerService
        $healthy = Wait-Healthy
    } catch {
        $healthy = $false
    }
    if (-not $healthy) {
        Info 'New version did not become healthy. Rolling back...'
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        try { Stop-ServerProcesses } catch { Info $_.Exception.Message }
        Undo-Update 'The new version did not start.'
    }
    Info "Update complete. Service is healthy on port $Port."
} elseif ($BareServer) {
    Info 'Update complete. The server was not running as a service; start it again yourself.'
} else {
    Info 'Update complete. The server was not running, so it was left stopped.'
}
Info "Previous files are in $Backup"
exit 0
