@echo off
rem Double-click to apply this update package. Asks for administrator rights.
setlocal
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
set "UPDATE_EXIT=%errorlevel%"
pause
exit /b %UPDATE_EXIT%
