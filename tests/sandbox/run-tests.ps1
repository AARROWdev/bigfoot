# Runs the whole Bigfoot test suite in a fresh Windows Sandbox:
#   installer tests (install, desktop shortcut, upgrade from an older version, upgrade while running, uninstall,
#   portable zip), black-box tests against the installed Bigfoot.exe, and every test in tests\app (pytest).
# Nothing runs on this PC except the build of an older setup used for the upgrade test.
# Results land in a temp folder (printed at the end). Needs: build.ps1 run first, and .venv-test
# (python -m venv .venv-test; .venv-test\Scripts\pip install PySide6-Essentials pytest pywinauto psutil).
param([string]$Pytest = '', [string]$K = '', [switch]$NoInstall, [switch]$Networking, [switch]$Quick, [switch]$NoWait, [int]$Minutes = 240)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$py = (Get-Content "$root\.venv-test\pyvenv.cfg" | Where-Object { $_ -like 'home*' }) -replace '^home\s*=\s*', ''
$ver = [regex]::Match((Get-Content "$root\bigfoot.py" -Raw), 'VERSION = "([^"]+)"').Groups[1].Value
if (-not $ver) { throw 'No VERSION in bigfoot.py' }
if (-not (Test-Path "$root\dist\BigfootSetup-$ver.exe")) { throw "Run build.ps1 first: dist\BigfootSetup-$ver.exe is missing (a build of another version doesn't count)" }

# An older setup (v1.2 code, same installer script) so the upgrade path is tested for real.
$old = "$root\build\old-setup"
if (-not (Test-Path "$old\BigfootSetup-1.2.exe")) {
    $src = "$root\build\old-src"
    if (Test-Path $src) { git -C $root worktree remove --force $src 2>$null; Remove-Item $src -Recurse -Force -ErrorAction SilentlyContinue }
    git -C $root worktree add --detach $src v1.2 | Out-Null
    try {
        Push-Location $src
        & "$root\.venv\Scripts\python" -m PyInstaller --noconfirm --windowed --name Bigfoot --icon "$src\bigfoot.ico" `
            --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick `
            --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtMultimedia --exclude-module psutil `
            --distpath "$src\build\dist" --workpath "$src\build\work" --specpath "$src\build" bigfoot.py 2>&1 | Out-Null
        Pop-Location
        New-Item -ItemType Directory -Force "$src\installer" | Out-Null
        Copy-Item "$root\installer\bigfoot.iss" "$src\installer\bigfoot.iss" -Force
        $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
        if (-not $iscc) { $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1 }
        & $iscc /Q /DAppVersion=1.2 "/O$old" "$src\installer\bigfoot.iss"
        if ($LASTEXITCODE) { throw "old setup build failed" }
    } finally {
        Set-Location $root
        git -C $root worktree remove --force $src
    }
}

$out = Join-Path $env:TEMP "bigfoot-tests-$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory $out | Out-Null
$wsb = @"
<Configuration>
  <Networking>$(if ($Networking) {'Default'} else {'Disable'})</Networking>
  <MemoryInMB>8192</MemoryInMB>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <MappedFolders>
    <MappedFolder><HostFolder>$root</HostFolder><SandboxFolder>C:\bigfoot</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$py</HostFolder><SandboxFolder>$py</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$old</HostFolder><SandboxFolder>C:\old</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$out</HostFolder><SandboxFolder>C:\out</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell -NoProfile -ExecutionPolicy Bypass -File C:\bigfoot\tests\sandbox\inside.ps1 -Pytest "$Pytest" -K "$K" $(if ($NoInstall) {'-NoInstall'}) $(if ($Quick) {'-Quick'})</Command></LogonCommand>
</Configuration>
"@
$wsb | Set-Content "$out\bigfoot.wsb" -Encoding UTF8
# only one Sandbox can run at a time: a leftover one would swallow this start
Get-Process WindowsSandbox* -ErrorAction SilentlyContinue | Stop-Process -Force
for ($i = 0; $i -lt 30 -and (Get-Process WindowsSandbox* -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
Start-Sleep 5
Start-Process "$out\bigfoot.wsb"
# Keep the Sandbox window minimized and never activate it: the PC's owner keeps using their mouse and keyboard.
# (SW_SHOWMINNOACTIVE = 7. The client can re-show itself while it connects, so watch it for 2 minutes.)
Add-Type -Namespace BfWin -Name U -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
[DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
'@
$until = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $until) {
    foreach ($p in Get-Process WindowsSandboxClient -ErrorAction SilentlyContinue) {
        if ($p.MainWindowHandle -ne 0 -and -not [BfWin.U]::IsIconic($p.MainWindowHandle)) {
            [void][BfWin.U]::ShowWindow($p.MainWindowHandle, 7)
        }
    }
    Start-Sleep -Milliseconds 300
}
Write-Host "Sandbox started; results in $out"
if ($NoWait) { $out; exit 0 }
$done = "$out\DONE"
for ($i = 0; $i -lt $Minutes * 12 -and -not (Test-Path $done); $i++) { Start-Sleep 5 }
Get-Process WindowsSandbox* -ErrorAction SilentlyContinue | Stop-Process -Force
if (-not (Test-Path $done)) { Write-Host "TIMEOUT after $Minutes minutes"; exit 2 }
Get-Content "$out\summary.txt"
if (Select-String -Path "$out\summary.txt" -Pattern '^FAIL' -Quiet) { exit 1 }
