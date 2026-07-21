@echo off
rem Remove the JARVIS autostart scheduled task (and any legacy startup shortcut).
schtasks /Delete /TN "JARVIS Assistant" /F 2>nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JARVIS.lnk" 2>nul
echo [JARVIS] Autostart removed.
pause
