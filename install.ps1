# Installs the built app to C:\Program Files\Bigfoot with Start Menu + Desktop shortcuts.
# Needs admin for Program Files: asks via UAC. Run again after a rebuild to update.
param([string]$Source = "$PSScriptRoot\build\dist\Bigfoot")
$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    $p = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Source', "`"$Source`"")
    exit $p.ExitCode
}
$dest = Join-Path $env:ProgramFiles 'Bigfoot'
Get-Process Bigfoot -ErrorAction SilentlyContinue | Stop-Process -Force
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item $Source $dest -Recurse
$exe = Join-Path $dest 'Bigfoot.exe'
$sh = New-Object -ComObject WScript.Shell
foreach ($lnk in @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Bigfoot.lnk",
                   "$([Environment]::GetFolderPath('Desktop'))\Bigfoot.lnk")) {
    $s = $sh.CreateShortcut($lnk); $s.TargetPath = $exe; $s.WorkingDirectory = $dest
    $s.Description = 'Find what is eating your disk'; $s.Save()
}
Write-Host "Installed to $dest"
