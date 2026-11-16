# Runs INSIDE Windows Sandbox (started by run-sandbox.ps1). Never run it on a real PC.
# Install -> upgrade while running -> uninstall, results to the mapped out folder.
$ErrorActionPreference = 'Continue'
$out = 'C:\out'; $log = "$out\results.txt"
function Check($name, $ok) { "$(if ($ok) {'PASS'} else {'FAIL'})  $name" | Add-Content $log }
if ($env:USERNAME -ne 'WDAGUtilityAccount') { 'Not in Windows Sandbox, refusing.' | Set-Content $log; exit 1 }
$setup = Get-ChildItem C:\dist\BigfootSetup-*.exe | Select-Object -First 1
$app = "$env:ProgramFiles\Bigfoot"
$key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{3484C9CA-B551-4A47-A264-7723F3745472}_is1'
$lnk = "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Bigfoot.lnk"
$args_ = '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'

$p = Start-Process $setup.FullName ($args_ + "/LOG=$out\install.log") -Wait -PassThru
Check "install exit 0 (got $($p.ExitCode))" ($p.ExitCode -eq 0)
Check 'Bigfoot.exe installed' (Test-Path "$app\Bigfoot.exe")
Check 'Start Menu shortcut' (Test-Path $lnk)
Check 'Add/Remove Programs entry' (Test-Path $key)

$run = Start-Process "$app\Bigfoot.exe" -PassThru; Start-Sleep 8
Check 'Bigfoot.exe launches and stays up' (-not $run.HasExited)
$p = Start-Process $setup.FullName ($args_ + "/LOG=$out\upgrade.log") -Wait -PassThru
Check "upgrade while running exit 0 (got $($p.ExitCode))" ($p.ExitCode -eq 0)
Check 'running Bigfoot was closed' ($run.HasExited)
Check 'Bigfoot.exe present after upgrade' (Test-Path "$app\Bigfoot.exe")

$p = Start-Process "$app\unins000.exe" $args_ -Wait -PassThru
$t = 0; while ((Test-Path $app) -and $t -lt 60) { Start-Sleep 1; $t++ }
Check "uninstall exit 0 (got $($p.ExitCode))" ($p.ExitCode -eq 0)
Check 'program folder removed' (-not (Test-Path $app))
Check 'Start Menu shortcut removed' (-not (Test-Path $lnk))
Check 'Add/Remove Programs entry removed' (-not (Test-Path $key))
'DONE' | Add-Content $log
