<#
.SYNOPSIS
    One-command install of the Immich Smart Album plugin, from Windows.

.DESCRIPTION
    Immich runs under Docker inside WSL2, so the work has to happen there. This
    script is a launcher: it checks WSL and Docker are reachable, fetches this
    repository inside WSL, and runs the installer, which places the plugin,
    enables external plugins, mounts the folder, restarts Immich and verifies
    the plugin loaded.

    Every file it touches is backed up first, and a docker-compose.yml that
    fails validation after editing is restored automatically.

    Nothing here needs your Immich API key. You create that afterwards, in the
    Immich web UI, when you build the workflow.

.PARAMETER ImmichDir
    Folder inside WSL holding docker-compose.yml, e.g. '~/immich-app'. Omit it
    and the installer asks Docker where the running stack was started from,
    then falls back to the usual locations.

.PARAMETER Distro
    WSL distribution to use. Defaults to your default distro.

.PARAMETER NoRestart
    Make every change but leave the stack alone; you restart when ready.

.PARAMETER DryRun
    Show what would run, change nothing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\Install-ImmichPlugin.ps1

.EXAMPLE
    .\scripts\Install-ImmichPlugin.ps1 -ImmichDir '~/immich-app' -NoRestart
#>
[CmdletBinding()]
param(
    [string]$ImmichDir = "",
    [string]$Distro = "",
    [switch]$NoRestart,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$Repo    = "https://github.com/KuroViolet57/ImmichPersonal.git"
$Branch  = "claude/immich-album-organization-2sj7f8"
$Checkout = "`$HOME/.immich-organizer"     # expanded inside WSL, not here

function Info($m) { Write-Host $m }
function Good($m) { Write-Host $m -ForegroundColor Green }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Head($m) { Write-Host "`n== $m" -ForegroundColor Cyan }

function Get-WslArgs {
    param([string]$Script)
    $a = @()
    if ($Distro) { $a += @("-d", $Distro) }
    return $a + @("-e", "bash", "-lc", $Script)
}

# Run a command inside WSL and return its output as a single string.
# Deliberately does NOT `return $LASTEXITCODE`: in PowerShell everything a
# function writes to output becomes part of its return value, so returning the
# code as well would append it to the captured text. $LASTEXITCODE is global
# and readable by the caller straight after this returns.
function Invoke-WslCapture {
    param([string]$Script)
    $wslArgs = Get-WslArgs $Script
    $output = & wsl @wslArgs 2>&1
    return ($output | Out-String).Trim()
}

Write-Host "Immich Smart Album - plugin installer" -ForegroundColor Cyan
Write-Host ("-" * 46)

# ------------------------------------------------------------------ preflight

Head "Checking WSL"
if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    throw "wsl.exe not found. This script is for Immich running under Docker in WSL2."
}
$uname = Invoke-WslCapture 'uname -sr'
if ($LASTEXITCODE -ne 0) {
    throw "Could not run a command inside WSL. Try 'wsl -l -v' to see your distributions, and pass -Distro <name>."
}
Info "  $uname"

Head "Checking Docker inside WSL"
$dockerVersion = Invoke-WslCapture 'docker compose version 2>/dev/null || echo MISSING'
if ("$dockerVersion" -match "MISSING") {
    throw @"
Docker is not reachable from inside WSL.

If you use Docker Desktop, enable WSL integration for this distribution:
  Docker Desktop -> Settings -> Resources -> WSL Integration
Otherwise start the daemon inside WSL (e.g. 'sudo service docker start').
"@
}
Info "  $dockerVersion"

Head "Checking Immich is running"
$immich = Invoke-WslCapture "docker ps --filter name=immich --format '{{.Names}}' | tr '\n' ' '"
if (-not "$immich".Trim()) {
    Warn "  No running container matching 'immich'. Continuing, but the installer"
    Warn "  may not find your deployment - pass -ImmichDir if it cannot."
} else {
    Info "  $immich"
}

# ------------------------------------------------------------------ fetch repo

Head "Fetching the installer inside WSL"
$fetch = @"
set -e
if command -v git >/dev/null 2>&1; then :; else
  echo "git is not installed in this WSL distribution. Install it with: sudo apt update && sudo apt install -y git" >&2
  exit 1
fi
if [ -d "$Checkout/.git" ]; then
  cd "$Checkout" && git fetch -q origin "$Branch" && git checkout -q "$Branch" && git reset -q --hard "origin/$Branch"
  echo "updated $Checkout"
else
  git clone -q -b "$Branch" "$Repo" "$Checkout"
  echo "cloned into $Checkout"
fi
"@
$out = Invoke-WslCapture $fetch
if ($LASTEXITCODE -ne 0) { throw "Could not fetch the repository inside WSL:`n$out" }
Info "  $out"

# --------------------------------------------------------------------- install

$installArgs = @("--all", "--yes")
if ($NoRestart) { $installArgs += "--no-restart" }
$dirArg = ""
if ($ImmichDir) {
    # '~' is not expanded inside bash quotes; $HOME is.
    $safeDir = $ImmichDir -replace '^~', '$HOME'
    $dirArg = " `"$safeDir`""
}
$command = "cd `"$Checkout`" && bash scripts/install-plugin.sh $($installArgs -join ' ')$dirArg"

if ($DryRun) {
    Head "Dry run - the command that would run inside WSL"
    Info "  $command"
    Write-Host "`nNothing was changed." -ForegroundColor Yellow
    exit 0
}

Head "Running the installer"
Info "  (every file is backed up first; an invalid compose edit is reverted)"
Write-Host ""
$wslArgs = Get-WslArgs $command
& wsl @wslArgs
$code = $LASTEXITCODE

Write-Host ""
if ($code -ne 0) {
    Warn "The installer exited with code $code. Nothing above the failure point was left half-applied"
    Warn "unless it says otherwise - read its output, and see docs/INSTALL-PLUGIN-WSL2.md to roll back."
    exit $code
}

Good "Plugin installed."
Write-Host ""
Write-Host "Next, in the Immich web UI:" -ForegroundColor Cyan
Write-Host "  1. Account Settings -> API Keys -> New API Key."
Write-Host "     Tick ONLY 'asset.read'. Use the account that owns the photos."
Write-Host "  2. Workflows -> New (or the 'Smart album' template)."
Write-Host "     Trigger: Asset tagged   <- a just-uploaded photo is not indexed yet"
Write-Host "  3. Step 1 'Filter by smart search': a description, match depth 200, the key."
Write-Host "     Step 2 'Add to Album(s)': the target album."
Write-Host ""
Write-Host "Then tag a photo and check Workflows -> logs."
