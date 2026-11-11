# Builds the portable Windows version: build\dist\Bigfoot\Bigfoot.exe
# Needs: .venv with requirements + pyinstaller.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
.\.venv\Scripts\python -m PyInstaller --noconfirm --windowed --name Bigfoot --icon "$PSScriptRoot\bigfoot.ico" `
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtMultimedia --exclude-module psutil `
    --distpath build\dist --workpath build\work --specpath build bigfoot.py
Copy-Item README.md build\dist\Bigfoot -ErrorAction SilentlyContinue
Write-Host "Built build\dist\Bigfoot\Bigfoot.exe"
