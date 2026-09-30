@echo off
setlocal
cd /d "%~dp0"
title Codex Token Dashboard

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dashboard.ps1"
set "dashboard_exit=%errorlevel%"

if not "%dashboard_exit%"=="0" (
  echo.
  echo Codex Token Dashboard failed to start. See the message above.
  echo This window will stay open so the error is not lost.
  pause
)

endlocal & exit /b %dashboard_exit%
