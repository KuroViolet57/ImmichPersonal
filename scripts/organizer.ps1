<#
.SYNOPSIS
    Runs immich-organizer from the local virtual environment.

.DESCRIPTION
    A thin wrapper so you do not have to activate .venv by hand. Every argument
    is forwarded to the CLI unchanged.

.EXAMPLE
    .\scripts\organizer.ps1 doctor
    .\scripts\organizer.ps1 search --query "person in a mountain" --limit 20
    .\scripts\organizer.ps1 plan -r rules.yaml --html plan.html --open
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Error "No virtual environment found. Run .\scripts\install.ps1 first."
    exit 2
}

& $venvPython -m immich_organizer @Arguments
exit $LASTEXITCODE
