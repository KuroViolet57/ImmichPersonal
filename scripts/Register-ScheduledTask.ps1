<#
.SYNOPSIS
    Registers a Windows scheduled task that files newly uploaded photos.

.DESCRIPTION
    Runs `immich-organizer apply --yes` on a schedule. Because rules skip
    anything already in the target album, a recurring run only picks up photos
    added since last time.

    Review your rules with `plan` before automating them -- this runs unattended
    with no confirmation prompt.

.PARAMETER RulesFile
    Path to the rules file. Defaults to rules.yaml in the repository root.

.PARAMETER Schedule
    Daily or Weekly (default: Weekly).

.PARAMETER Time
    Time of day to run, as HH:mm (default: 03:00).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\Register-ScheduledTask.ps1 -Schedule Daily -Time 02:30
#>
[CmdletBinding()]
param(
    [string]$RulesFile = "",
    [ValidateSet("Daily", "Weekly")][string]$Schedule = "Weekly",
    [string]$Time = "03:00",
    [string]$TaskName = "Immich Organizer"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not $RulesFile) { $RulesFile = Join-Path $repoRoot "rules.yaml" }

if (-not (Test-Path $venvPython)) { throw "Run .\scripts\install.ps1 first." }
if (-not (Test-Path $RulesFile))  { throw "Rules file not found: $RulesFile" }

# Fail early rather than discovering a broken rules file at 3am.
& $venvPython -m immich_organizer validate -r $RulesFile
if ($LASTEXITCODE -ne 0) { throw "The rules file did not validate; task not registered." }

$logDir = Join-Path $env:LOCALAPPDATA "immich-organizer"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "scheduled-run.log"

$command = "& '$venvPython' -m immich_organizer apply -r '$RulesFile' --yes *>> '$logFile'"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"$command`""

$trigger = if ($Schedule -eq "Daily") {
    New-ScheduledTaskTrigger -Daily -At $Time
} else {
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $Time
}

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Files new Immich photos into albums by rule." -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' ($Schedule at $Time)." -ForegroundColor Green
Write-Host "Rules:  $RulesFile"
Write-Host "Log:    $logFile"
Write-Host ""
Write-Host "Remove it later with:  Unregister-ScheduledTask -TaskName '$TaskName'"
