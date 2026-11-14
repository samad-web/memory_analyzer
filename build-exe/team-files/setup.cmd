@echo off
REM Sirahlyzer one-time setup. Safe to re-run. No admin needed.
REM  1. Wires Claude Code hooks so your agents show up in the island.
REM  2. Registers the 15-minute SirahOS push task.
setlocal
set "EXE=%~dp0SirahlyzerBridge.exe"
set "SETTINGS=%USERPROFILE%\.claude\settings.json"

echo [1/2] Connecting Claude Code to Sirahlyzer...
"%EXE%" hooks-install
findstr /C:"SirahlyzerBridge.exe" "%SETTINGS%" >nul 2>&1
if errorlevel 1 (
  echo     FAILED: hooks not found in %SETTINGS%
  echo     Is Claude Code installed for this Windows user?
  goto :fail
)
echo     OK - hooks added to %SETTINGS%
echo     Restart any open Claude Code sessions so they pick up the hooks.

echo.
echo [2/2] Registering the SirahOS auto-push task (every 15 min)...
schtasks /Create /TN SirahOSBridge /TR "\"%EXE%\" push" /SC MINUTE /MO 15 /F >nul
if errorlevel 1 (
  echo     FAILED to register the task. You can still use "Push now" in the dashboard.
  goto :fail
)
echo     OK - task SirahOSBridge registered

echo.
echo Setup complete. Next: open the dashboard (http://127.0.0.1:8765),
echo SirahOS tab, and sign in.
pause
exit /b 0

:fail
pause
exit /b 1
