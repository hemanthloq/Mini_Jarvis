@echo off
rem ── JARVIS launcher ─────────────────────────────────────────
rem Starts the Python backend, then the Electron HUD overlay.
cd /d "%~dp0"

rem --interactive means a human ran this (show notepad / pause on error).
rem Autostart passes nothing, so it never blocks on a prompt.
set "INTERACTIVE=1"
if /I "%~1"=="/auto" set "INTERACTIVE="

if not exist ".env" (
    echo [JARVIS] No .env found — copying .env.example. Fill in your API keys!
    copy .env.example .env >nul
    if defined INTERACTIVE notepad .env
)

if not exist ".venv\Scripts\python.exe" (
    echo [JARVIS] No venv found. Run setup.bat first.
    if defined INTERACTIVE pause
    exit /b 1
)

echo [JARVIS] stopping any previous instance...
rem Match the ACTUAL command line ("...python.exe backend\main.py"). The old
rem pattern looked for double backslashes that never occur, so it never killed
rem anything and every launch stacked another backend — two backends fighting
rem over the microphone is what broke barge-in / "stop".
rem Match forward slashes too ('backend/main.py'), and a bare 'main.py' run from
rem the backend directory — both slipped through the backslash-only pattern.
rem backend/main.py ALSO refuses to start if the port is already held, so this
rem is now belt-and-braces rather than the only guard.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '(backend[\\/])?main\.py' -or ($_.Name -eq 'electron.exe' -and $_.CommandLine -match 'frontend') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
powershell -NoProfile -Command "Start-Sleep -Milliseconds 800" >nul 2>&1

echo [JARVIS] starting backend...
start "JARVIS backend" /min cmd /c ".venv\Scripts\python.exe backend\main.py"

rem wait for the backend health endpoint before opening the HUD
powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) { try { Invoke-WebRequest -Uri http://127.0.0.1:8765/health -UseBasicParsing -TimeoutSec 1 | Out-Null; exit 0 } catch { Start-Sleep -Milliseconds 500 } }; exit 1"
if errorlevel 1 echo [JARVIS] warning: backend not responding yet, opening HUD anyway...

echo [JARVIS] starting HUD...
set ELECTRON_RUN_AS_NODE=
start "JARVIS HUD" /min cmd /c "cd frontend && npm start"

echo [JARVIS] online. Say "hey jarvis".
echo [JARVIS] backend logs: data\jarvis.log  (backend runs in its own minimized window)
echo [JARVIS] debug a command without speaking:
echo     curl "http://127.0.0.1:8765/simulate?text=open+downloads"
