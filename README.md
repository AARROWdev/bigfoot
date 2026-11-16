# Bigfoot

**Find what's eating your disk.** A fast disk space analyzer for Windows.

## Download
Windows 10/11, 64-bit. From the [latest release](https://github.com/AARROWdev/bigfoot/releases/latest):
- **BigfootSetup-<version>.exe** (recommended): installs to Program Files with a Start Menu entry. Run it again to upgrade; it closes Bigfoot if it's open.
- **Bigfoot-<version>-portable-win64.zip**: unzip anywhere and run `Bigfoot.exe`. Nothing is installed.

The app isn't code-signed yet, so Windows may show "Windows protected your PC": click **More info**, then **Run anyway**.

## Uninstall
Settings > Apps > Bigfoot > Uninstall (portable: delete the folder). Your settings stay in the registry (`HKCU\Software\Bigfoot`); delete that key to remove them too.

## What it does
- **Fast scan** of any drive or folder (multi-threaded, live progress, tree fills in as it scans)
- **Biggest-first tree:** size, % of drive bar, files, folders, newest modified date. Click any column to sort.
- **Map:** treemap of the selected folder, colored by file type. Click to find it in the tree, double-click a folder to zoom in, right-click or Up to zoom out.
- **Types:** space by extension and category (video, audio, images, archives, disk images, programs, documents, code)
- **Top 100 files:** the biggest files under the selected folder
- **Actions:** open, show in Explorer, copy path, scan only this folder, send to Recycle Bin (totals update on the spot)
- Drive picker with used/free bar

## Keys
| Key | Action |
|---|---|
| F5 | Rescan |
| Ctrl+L | Type a path |
| Delete | Send selection to Recycle Bin |
| Ctrl+C | Copy path |
| Backspace (on map) | Zoom out |

Folders Windows won't let you read are counted as "access denied" in the status bar; run as administrator to include them.

## Build
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
winget install JRSoftware.InnoSetup   # installer compiler
.\build.ps1   # -> build\dist\Bigfoot\Bigfoot.exe, dist\BigfootSetup-<ver>.exe, dist\Bigfoot-<ver>-portable-win64.zip
```
`Bigfoot.bat` runs it from source. `tests\sandbox\run-sandbox.ps1` tests the installer (install, upgrade while running, uninstall) in a throwaway Windows Sandbox.
Settings live in the registry (`HKCU\Software\Bigfoot`); `error.log` is in `%LOCALAPPDATA%\Bigfoot\`.

## Later
- NTFS master file table reader (whole-drive scans in seconds, needs admin)
- Hard-link de-duplication (WinSxS), allocated size on disk
- Mac version
- Clutter cleaner: scan → report → approve → quarantine
