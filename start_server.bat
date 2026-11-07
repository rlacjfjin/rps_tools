@echo off
rem Double-click to start the rps_tools web service.
rem First run creates .venv. Every start checks the runtime requirements.
rem LAN access: start_server.bat --host 0.0.0.0
setlocal
pushd "%~dp0" || exit /b 1
if not exist ".venv\Scripts\python.exe" (
    echo [rps_tools] First run: creating .venv ...
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || goto :python_error
    python -m venv .venv || goto :error
)
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || goto :python_error
rem Already-satisfied packages are kept; this does not force an upgrade.
".venv\Scripts\python.exe" -m pip --disable-pip-version-check install -r requirements.txt || goto :error
".venv\Scripts\python.exe" -m rps_tools serve %*
set "RPS_TOOLS_EXIT=%errorlevel%"
popd
exit /b %RPS_TOOLS_EXIT%

:python_error
echo [rps_tools] Python 3.10+ is required. Recreate .venv with a supported Python if necessary.
:error
echo [rps_tools] Setup failed. See the error above; the server was not started.
pause
popd
exit /b 1
