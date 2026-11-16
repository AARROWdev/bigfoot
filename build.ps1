# Builds Bigfoot for Windows:
#   build\dist\Bigfoot\Bigfoot.exe          the app (PyInstaller one-folder build)
#   dist\BigfootSetup-<ver>.exe             the installer (Inno Setup 6)
#   dist\Bigfoot-<ver>-portable-win64.zip   the portable version
# Needs: .venv with requirements + pyinstaller, and Inno Setup 6 (winget install JRSoftware.InnoSetup).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$m = Select-String -Path bigfoot.py -Pattern '^VERSION = "([^"]+)"'
if (-not $m) { throw 'VERSION not found in bigfoot.py' }
$ver = $m.Matches[0].Groups[1].Value

.\.venv\Scripts\python -m PyInstaller --noconfirm --windowed --name Bigfoot --icon "$PSScriptRoot\bigfoot.ico" `
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtMultimedia --exclude-module psutil `
    --distpath build\dist --workpath build\work --specpath build bigfoot.py
if ($LASTEXITCODE) { throw "PyInstaller failed ($LASTEXITCODE)" }
Copy-Item README.md build\dist\Bigfoot

$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw 'Inno Setup 6 not found (winget install JRSoftware.InnoSetup)' }
New-Item -ItemType Directory -Force dist | Out-Null
& $iscc /Q "/DAppVersion=$ver" installer\bigfoot.iss
if ($LASTEXITCODE) { throw "Inno Setup failed ($LASTEXITCODE)" }

$zip = "dist\Bigfoot-$ver-portable-win64.zip"
Compress-Archive -Path build\dist\Bigfoot -DestinationPath $zip -Force

Write-Host "Built build\dist\Bigfoot\Bigfoot.exe, dist\BigfootSetup-$ver.exe, $zip"
