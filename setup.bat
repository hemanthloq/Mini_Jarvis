@echo off
rem ── JARVIS one-time setup ───────────────────────────────────
cd /d "%~dp0"

echo [1/5] creating Python venv...
if not exist ".venv" python -m venv .venv

echo [2/5] installing Python dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet

echo [3/5] installing Electron (frontend)...
pushd frontend
call npm install --no-fund --no-audit
popd

echo [4/5] downloading wake-word models + building file index...
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'backend'); from audio import wake; wake.ensure_models(); from system import indexer; indexer.build_index()"

echo       training the custom 'jarvis' wake word (a few minutes, one time)...
.venv\Scripts\python.exe scripts\train_wake_jarvis.py

if not exist ".env" (
    copy .env.example .env >nul
    echo [5/5] .env created — opening it so you can paste your API keys.
    notepad .env
) else (
    echo [5/5] .env already exists — skipping.
)

echo.
echo Setup complete. Optional but recommended (needs ELEVENLABS_API_KEY in .env):
echo     .venv\Scripts\python.exe scripts\cache_phrases.py
echo Then run start.bat
pause
