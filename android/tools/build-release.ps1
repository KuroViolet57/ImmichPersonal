# Builds a signed release APK on Windows and copies it, with its changes.txt,
# to "K:\My Drive\Image Panel App\v<version>\".
#   powershell -ExecutionPolicy Bypass -File android\tools\build-release.ps1
param(
    [string]$Out = "K:\My Drive\Image Panel App",
    [string]$Signing = "$env:USERPROFILE\.imagepanel\signing.properties"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot           # ...\android
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
$env:IMAGEPANEL_SIGNING = $Signing
Push-Location $root
try {
    & .\gradlew.bat --no-daemon --console=plain assembleRelease
    if ($LASTEXITCODE -ne 0) { throw "Gradle failed ($LASTEXITCODE)" }
    $version = (Select-String -Path app\build.gradle.kts -Pattern 'versionName = "([^"]+)"').Matches[0].Groups[1].Value
    $apk = Get-ChildItem app\build\outputs\apk\release\*.apk | Select-Object -First 1
    $dest = Join-Path $Out "v$version"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item $apk.FullName (Join-Path $dest "ImagePanel-$version.apk") -Force
    $changes = Join-Path $root "changes\$version.txt"
    if (Test-Path $changes) { Copy-Item $changes (Join-Path $dest "changes.txt") -Force }
    Write-Output "Delivered $dest\ImagePanel-$version.apk"
} finally {
    Pop-Location
}
