# Runs INSIDE Windows Sandbox (started by run-tests.ps1). Never run it on a real PC.
# 1. installer tests  2. black-box tests on the installed app  3. pytest tests\app  4. uninstall + portable zip
param([string]$Pytest = '', [string]$K = '', [switch]$NoInstall, [switch]$Quick)
$ErrorActionPreference = 'Continue'
$out = 'C:\out'; $sum = "$out\summary.txt"
if ($env:USERNAME -ne 'WDAGUtilityAccount') { 'FAIL  not in Windows Sandbox, refusing' | Set-Content $sum; 'x' | Set-Content "$out\DONE"; exit 1 }
$py = 'C:\bigfoot\.venv-test\Scripts\python.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'
function Check($name, $ok) { "$(if ($ok) {'PASS'} else {'FAIL'})  $name" | Add-Content $sum }
function Log($m) { "$(Get-Date -Format HH:mm:ss)  $m" | Add-Content "$out\progress.log" }

$setup = Get-ChildItem C:\bigfoot\dist\BigfootSetup-*.exe | Sort-Object LastWriteTime | Select-Object -Last 1
$oldSetup = 'C:\old\BigfootSetup-1.1.exe'
$app = "$env:ProgramFiles\Bigfoot"
$key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{3484C9CA-B551-4A47-A264-7723F3745472}_is1'
$lnk = "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Bigfoot.lnk"
$desk = "$env:PUBLIC\Desktop\Bigfoot.lnk"
$silent = '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
$t0 = Get-Date

if (-not $NoInstall) {
    Log 'A2: interactive install from the wizard, desktop shortcut ticked'
    & $py C:\bigfoot\tests\sandbox\blackbox.py wizard $setup.FullName
    Check 'A2 wizard install finished (exit 0)' ($LASTEXITCODE -eq 0)
    Check 'A2 desktop shortcut created' (Test-Path $desk)
    Check 'A1 Start Menu shortcut' (Test-Path $lnk)
    Check 'A1 Add/Remove Programs entry' (Test-Path $key)
    Check 'A1 version 1.2 registered' ((Get-ItemProperty $key).DisplayVersion -eq '1.2')

    Log 'A4: uninstall, then A3: install the older version and upgrade over it'
    Start-Process "$app\unins000.exe" $silent -Wait
    $t = 0; while ((Test-Path $app) -and $t -lt 60) { Start-Sleep 1; $t++ }
    Check 'A4 uninstall removes the program folder' (-not (Test-Path $app))
    Check 'A4 uninstall removes the desktop shortcut' (-not (Test-Path $desk))
    Check 'A4 uninstall keeps the user settings' (Test-Path 'HKCU:\Software\Bigfoot\Bigfoot')
    $p = Start-Process $oldSetup ($silent + "/LOG=$out\old-install.log") -Wait -PassThru
    Check "A3 older version installs (exit $($p.ExitCode))" ($p.ExitCode -eq 0 -and (Get-ItemProperty $key).DisplayVersion -eq '1.1')
    $p = Start-Process $setup.FullName ($silent + "/LOG=$out\upgrade-old.log") -Wait -PassThru
    Check "A3 upgrade from 1.1 (exit $($p.ExitCode))" ($p.ExitCode -eq 0 -and (Get-ItemProperty $key).DisplayVersion -eq '1.2')
    & $py C:\bigfoot\tests\sandbox\blackbox.py title "$app\Bigfoot.exe" 'Bigfoot 1.2'
    Check 'A3 the upgraded app is 1.2 (window title)' ($LASTEXITCODE -eq 0)
    Check 'A3 only one uninstaller entry' ((Get-ChildItem $app -Filter 'unins*.exe').Count -eq 1)

    Log 'A3: upgrade while Bigfoot runs'
    $run = Start-Process "$app\Bigfoot.exe" -PassThru; Start-Sleep 6
    $p = Start-Process $setup.FullName ($silent + "/LOG=$out\upgrade.log") -Wait -PassThru
    Check "A3 upgrade while running (exit $($p.ExitCode))" ($p.ExitCode -eq 0)
    Check 'A3 setup closed the running Bigfoot' ($run.HasExited)

    Log 'black-box tests on the installed app'
    & $py C:\bigfoot\tests\sandbox\blackbox.py all "$app\Bigfoot.exe"
    Get-Content "$out\blackbox-results.txt" -ErrorAction SilentlyContinue | Add-Content $sum
}

Log "pytest tests\app $Pytest"
New-Item -ItemType Directory -Force C:\bftest | Out-Null
$args_ = @('-m', 'pytest', 'C:\bigfoot\tests\app', '-p', 'no:cacheprovider', '-v', '-rs', '--durations=25',
           "--junitxml=$out\pytest.xml", '-o', 'junit_family=xunit1', '--basetemp=C:\bftmp',
           '-o', 'faulthandler_timeout=900')  # a hung test dumps every thread's stack into the log
if ($Pytest) { $args_ += $Pytest -split ' ' }
if ($K) { $args_ += @('-k', ($K -replace '~', ' ')) }  # '~' stands for a space (the .wsb command line can't quote)
if ($Quick) { $args_ += @('-m', 'not big', '-k', 'not whole_c_drive and not monkey') }
& $py @args_ 2>&1 | Out-File -Encoding utf8 "$out\pytest.log"
$code = $LASTEXITCODE
Check "pytest tests\app (exit $code; see pytest.log)" ($code -eq 0)
Select-String -Path "$out\pytest.log" -Pattern '^(FAILED|ERROR) ' | ForEach-Object { "FAIL  $($_.Line)" } | Add-Content $sum
(Select-String -Path "$out\pytest.log" -Pattern '^=+ .*(passed|failed).* =+$' | Select-Object -Last 1).Line | Add-Content $sum

if (-not $NoInstall) {
    Log 'A4: uninstall refused while running, then a clean uninstall'
    $run = Start-Process "$app\Bigfoot.exe" -PassThru; Start-Sleep 6
    $p = Start-Process "$app\unins000.exe" $silent -Wait -PassThru; Start-Sleep 3
    Check "A4 uninstall refused while running (exit $($p.ExitCode))" ($p.ExitCode -ne 0 -and (Test-Path "$app\Bigfoot.exe"))
    Stop-Process -Id $run.Id -Force -ErrorAction SilentlyContinue; Start-Sleep 2
    $p = Start-Process "$app\unins000.exe" $silent -Wait -PassThru
    $t = 0; while ((Test-Path $app) -and $t -lt 60) { Start-Sleep 1; $t++ }
    Check "A4 uninstall (exit $($p.ExitCode))" ($p.ExitCode -eq 0)
    Check 'A4 program folder removed' (-not (Test-Path $app))
    Check 'A4 Start Menu shortcut removed' (-not (Test-Path $lnk))
    Check 'A4 Add/Remove Programs entry removed' (-not (Test-Path $key))

    Log 'A5: portable zip'
    $zip = Get-ChildItem C:\bigfoot\dist\Bigfoot-*-portable-win64.zip | Select-Object -First 1
    Expand-Archive $zip.FullName C:\portable -Force
    $exe = Get-ChildItem C:\portable -Recurse -Filter Bigfoot.exe | Select-Object -First 1
    & $py C:\bigfoot\tests\sandbox\blackbox.py portable $exe.FullName
    Check 'A5 portable zip runs and scans' ($LASTEXITCODE -eq 0)
    Check 'A5 portable run wrote nothing to Program Files' (-not (Test-Path $app))
}
"elapsed $([int]((Get-Date) - $t0).TotalMinutes) min" | Add-Content $sum
Log 'done'
'done' | Set-Content "$out\DONE"
shutdown /s /t 5  # close the Sandbox when finished
