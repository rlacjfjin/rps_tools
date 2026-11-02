@echo off
rem Double-click to start the rps_tools web service.
rem First run creates .venv and installs requirements.
rem LAN access: start_server.bat --host 0.0.0.0
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [rps_tools] First run: creating .venv and installing requirements ...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)
".venv\Scripts\python.exe" -m rps_tools serve %*
goto :eof

:error
echo [rps_tools] Failed to start. Python 3.10+ is required.
pause
