@echo off
rem Register JARVIS to start at log on via Task Scheduler. This is far more
rem reliable than a Startup-folder shortcut: it waits for the user session,
rem uses the correct working directory, and applies a 30s delay so the
rem environment (network, audio, venv) is ready. Scoped to the current user,
rem so it needs no Administrator rights.
cd /d "%~dp0"

echo [JARVIS] Registering scheduled task "JARVIS Assistant" (at log on, 30s delay)...

rem CRITICAL: -AllowStartIfOnBatteries is required. Without it (the Task Scheduler
rem default), a laptop that boots on battery SILENTLY refuses to start the task —
rem which is exactly why autostart "never worked" before. We set it explicitly and
rem then VERIFY it stuck.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$dir = '%~dp0'.TrimEnd('\');" ^
  "$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c \"' + $dir + '\start.bat\" /auto') -WorkingDirectory $dir;" ^
  "$trigger = New-ScheduledTaskTrigger -AtLogOn -User ($env:USERDOMAIN + '\' + $env:USERNAME);" ^
  "$trigger.Delay = 'PT30S';" ^
  "$principal = New-ScheduledTaskPrincipal -UserId ($env:USERDOMAIN + '\' + $env:USERNAME) -LogonType Interactive;" ^
  "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0);" ^
  "try {" ^
  "  Register-ScheduledTask -TaskName 'JARVIS Assistant' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null;" ^
  "  $s = (Get-ScheduledTask -TaskName 'JARVIS Assistant').Settings;" ^
  "  Write-Host ('[JARVIS] Registered. Starts on battery: ' + (-not $s.DisallowStartIfOnBatteries) + ' (must be True).');" ^
  "  if ($s.DisallowStartIfOnBatteries) { Write-Host '[JARVIS] WARNING: battery-start still blocked — autostart may not fire on battery.' }" ^
  "  else { Write-Host '[JARVIS] Done. JARVIS will start ~30s after you log in.' }" ^
  "} catch { Write-Host ('[JARVIS] FAILED to register: ' + $_.Exception.Message); Write-Host '[JARVIS] If this mentions access denied, right-click this .bat and Run as administrator.' }"

rem Remove any old Startup-folder shortcut from the previous approach.
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JARVIS.lnk" 2>nul

echo.
echo [JARVIS] Verify the task's registered state yourself:
echo     schtasks /Query /TN "JARVIS Assistant"
echo [JARVIS] Test the action now without rebooting:
echo     schtasks /Run /TN "JARVIS Assistant"
echo [JARVIS] (Then reboot once to confirm it fires automatically at log on.)
pause
