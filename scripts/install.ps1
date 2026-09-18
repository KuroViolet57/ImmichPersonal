<#
.SYNOPSIS
    Sets up immich-organizer in a local virtual environment on Windows.

.DESCRIPTION
    Creates .venv next to the repository, installs the package into it, and
    verifies the CLI runs. Re-running is safe; it upgrades in place.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#>
[CmdletBinding()]
param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repoRoot ".venv"

function Find-Python {
    # Returns @{ Exe = <path>; Args = <string[]> }.
    if ($PythonExe) { return @{ Exe = $PythonExe; Args = @() } }

    # The py launcher is the reliable way to get a real Python rather than the
    # Microsoft Store stub that ships disabled on a fresh Windows install.
    $candidates = @(
        @{ Exe = "py";      Args = @("-3") },
        @{ Exe = "python";  Args = @() },
        @{ Exe = "python3"; Args = @() }
    )
    foreach ($candidate in $candidates) {
        try {
            $probe = @($candidate.Args) + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
            $version = & $candidate.Exe @probe 2>$null
            if ($LASTEXITCODE -eq 0 -and $version) {
                $major, $minor = "$version".Trim().Split(".")
                if ([int]$major -ge 3 -and [int]$minor -ge 9) { return $candidate }
            }
        } catch { continue }
    }
    throw "No Python 3.9+ found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run this script."
}

Write-Host "Immich Organizer - Windows setup" -ForegroundColor Cyan
Write-Host ("-" * 42)

$python = Find-Python
Write-Host ("Using Python: {0} {1}" -f $python.Exe, ($python.Args -join " "))

if (-not (Test-Path $venvPath)) {
    Write-Host "Creating virtual environment at $venvPath"
    $venvArgs = @($python.Args) + @("-m", "venv", $venvPath)
    & $python.Exe @venvArgs
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
} else {
    Write-Host "Reusing existing virtual environment."
}

$venvPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) { throw "Virtual environment looks broken: $venvPython is missing. Delete .venv and re-run." }

Write-Host "Installing immich-organizer..."
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet --upgrade "$($repoRoot)[yaml]"
if ($LASTEXITCODE -ne 0) { throw "Installation failed." }

$version = & $venvPython -m immich_organizer --version
Write-Host ""
Write-Host "Installed: $version" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. .\scripts\organizer.ps1 setup       # store your server URL and API key"
Write-Host "  2. .\scripts\organizer.ps1 doctor      # confirm smart search is working"
Write-Host "  3. Copy rules.example.yaml to rules.yaml and edit it"
Write-Host "  4. .\scripts\organizer.ps1 plan -r rules.yaml --html plan.html --open"
Write-Host ""
Write-Host "To reach it from your phone:"
Write-Host "  .\scripts\organizer.ps1 serve --host 0.0.0.0 -r rules.yaml"
