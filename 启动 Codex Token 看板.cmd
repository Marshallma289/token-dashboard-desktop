@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0CodexTokenDesktop.exe" (
  echo CodexTokenDesktop.exe is missing. Please extract the complete ZIP package first.
  pause
  exit /b 1
)

start "" "%~dp0CodexTokenDesktop.exe"
exit /b 0
