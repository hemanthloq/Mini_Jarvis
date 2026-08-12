# Creates a "JARVIS" shortcut on the Desktop (and Start Menu) that launches the
# assistant via launch_jarvis.vbs, using jarvis.ico. The shortcut targets
# wscript.exe (a real .exe) so Windows allows pinning it to the taskbar.
# Run:  powershell -ExecutionPolicy Bypass -File scripts\create_desktop_shortcut.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$vbs  = Join-Path $root 'launch_jarvis.vbs'
$ico  = Join-Path $root 'jarvis.ico'

if (-not (Test-Path $vbs)) { throw "launch_jarvis.vbs not found at $vbs" }
if (-not (Test-Path $ico)) { Write-Host "WARNING: jarvis.ico missing - run scripts\make_icon.ps1 first." }

$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$ws = New-Object -ComObject WScript.Shell

function New-JarvisShortcut([string]$path) {
    $lnk = $ws.CreateShortcut($path)
    $lnk.TargetPath = $wscript
    $lnk.Arguments = '"{0}"' -f $vbs
    $lnk.WorkingDirectory = $root
    if (Test-Path $ico) { $lnk.IconLocation = '{0},0' -f $ico }
    $lnk.Description = 'Launch JARVIS voice assistant'
    $lnk.WindowStyle = 7       # start minimized
    $lnk.Save()
    Write-Host "created $path"
}

# GetFolderPath('Desktop') resolves the REAL desktop even when OneDrive has
# redirected it (the same redirection JARVIS handles in backend/config.py).
$desktop = [Environment]::GetFolderPath('Desktop')
New-JarvisShortcut (Join-Path $desktop 'JARVIS.lnk')

# Also put it in the Start Menu so it is searchable / pinnable to Start.
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'JARVIS.lnk'
New-JarvisShortcut $startMenu

Write-Host ""
Write-Host "Done. Double-click JARVIS on your Desktop to launch."
Write-Host "To pin to the taskbar: right-click the Desktop icon, Show more options, Pin to taskbar."
