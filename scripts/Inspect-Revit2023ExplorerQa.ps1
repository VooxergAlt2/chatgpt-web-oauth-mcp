#Requires -Version 5.1
<#
.SYNOPSIS
  Read-only discovery of genuine Explorer GUI windows for native
  Revit 2023 QA. Never launches Revit or Explorer.
.DESCRIPTION
  For each window, runs the SHA-pinned Invoke-VerifiedExplorerRevit2023.ps1
  in ValidateOnly mode with a freshly discovered HWND. Returns compact JSON.
  Preflight PASS is not Autodesk licensing or Astra startup PASS.
#>
[CmdletBinding()]
param(
    [string] $VerifiedLauncherPath = "$env:LOCALAPPDATA\Astra-Revit-Bridge\QA\Invoke-VerifiedExplorerRevit2023.ps1"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$expectedHash = '62010e74bbf8fbd247d0a672a5d42bbf034430a8bdd6494e2a161180c7d5fed9'
if (-not (Test-Path -LiteralPath $VerifiedLauncherPath -PathType Leaf)) {
    throw 'VERIFIED_EXPLORER_HELPER_MISSING'
}
$actualHash = (Get-FileHash -LiteralPath $VerifiedLauncherPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -cne $expectedHash) {
    throw 'VERIFIED_EXPLORER_HELPER_HASH_MISMATCH'
}

$revit = @(Get-CimInstance Win32_Process -Filter "Name='Revit.exe'")
$shell = New-Object -ComObject Shell.Application
$views = @($shell.Windows() | Where-Object {
    try {
        [string]$_.FullName -match '(?i)\\explorer.exe$' -and
        [string]$_.LocationURL -eq 'file:///C:/Users/Public/Desktop'
    } catch { $false }
})
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$checkedHandles = @{}
$candidates = @()
$failures = @()
foreach ($view in $views) {
    [long] $handle = $view.HWND
    if ($handle -le 0 -or $checkedHandles.ContainsKey($handle)) { continue }
    $checkedHandles[$handle] = $true
    try {
        $lines = @(& $ps -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $VerifiedLauncherPath -Mode ValidateOnly -ExplorerWindowHandle $handle 2>&1)
        $exit = $LASTEXITCODE
    } catch {
        # An invalid Explorer window can report native stderr. With
        # ErrorActionPreference=Stop PowerShell promotes that into a
        # terminating error. This is an expected per-window rejection,
        # not a reason to abandon other independently verified windows.
        $lines = @($_)
        $exit = 1
    }
    $response = ($lines | Out-String)
    $marker = [regex]::Match(
        $response, 'ASTRA_EXPLORER_UIA_PREFLIGHT_PASS ownerPid=(\d+) mode=ValidateOnly'
    )
    if ($exit -eq 0 -and $marker.Success -and
        $response.Contains('VALIDATE_ONLY_NO_REVIT_LAUNCHED')) {
        $candidates += [PSCustomObject]@{
            hwnd = $handle
            ownerPid = [int] $marker.Groups[1].Value
            verified = $true
        }
    } else {
        $failures += [PSCustomObject]@{
            hwnd = $handle
            status = 'VALIDATE_ONLY_FAILED'
            exitCode = $exit
        }
    }
}
$ready = $candidates.Count -ge 1
$report = [PSCustomObject]@{
    status = if ($ready) { 'QA_EXPLORER_UIA_PREFLIGHT_READY' }
        else { 'NATIVE_NOT_RUN: GUI_EXPLORER_LAUNCH_UNAVAILABLE' }
    helperSha256 = $actualHash
    launchPerformed = $false
    existingRevitCount = $revit.Count
    checkedWindows = $checkedHandles.Count
    validCandidates = @($candidates)
    failedCandidates = @($failures)
    manualApprovalRequired = $true
    licenseAcceptanceRequired = $true
    nextAction = if ($ready) {
        'After explicit approval and confirmed Autodesk licensing, select a freshly verified HWND and invoke the original helper with -Mode LaunchOnce exactly once. Verify PID, Explorer parent, license and Astra startup.'
    } else {
        'Restore an actual interactive Explorer GUI session. Never substitute direct Revit.exe or retry a licensing stall.'
    }
}
$report | ConvertTo-Json -Depth 6 -Compress
