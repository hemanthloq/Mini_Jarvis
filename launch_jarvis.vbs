' Silent launcher for JARVIS — starts the backend + HUD with no console window.
' Runs start.bat with /auto so it never blocks on an invisible prompt.
Set sh = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.CurrentDirectory = scriptDir
sh.Run "cmd /c """ & scriptDir & "start.bat"" /auto", 0, False
