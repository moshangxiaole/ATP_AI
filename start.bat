@echo off
rem ============================================================================
rem  ATP Console - one-click launcher (Windows)
rem
rem  Double-click to start. Everything smart (reuse running instance, open
rem  browser, model health check, auto port fallback) lives in Python, so this
rem  script only has to find an interpreter and hand over. Keep it ASCII-only:
rem  a UTF-8 .bat prints mojibake before "chcp 65001" takes effect.
rem
rem  Usage:
rem    start.bat              start (or focus) the console
rem    start.bat --port 9000  custom port
rem    start.bat --stop       stop the background console
rem    start.bat --status     show whether it is running
rem ============================================================================
chcp 65001 >nul 2>nul
setlocal EnableDelayedExpansion
title ATP Console
cd /d "%~dp0"

rem  Only hold the window when something failed. Successful runs (including
rem  --stop / --status from a script or CI) must exit immediately,
rem  otherwise a pause would hang the caller forever.
set "HOLD="

rem ---------- 1. system interpreter (used to bootstrap the venv if needed)
set "SYS="
where py >nul 2>nul && set "SYS=py -3"
if not defined SYS (
  where python >nul 2>nul && set "SYS=python"
)
if not defined SYS goto :no_python

rem ---------- 2. project venv, created on first run so we never dirty the OS
set "PY=venv\Scripts\python.exe"
if not exist "!PY!" (
  echo [*] First run - creating virtual environment ...
  %SYS% -m venv venv
  if errorlevel 1 (
    echo [!] Could not create venv, falling back to the system interpreter.
    set "PY=%SYS%"
  )
)

rem ---------- 3. version gate
%PY% -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python

rem ---------- 4. dependencies
%PY% -c "import pydantic,yaml,PIL" >nul 2>nul
if errorlevel 1 (
  echo [*] Installing dependencies ...
  %PY% -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 goto :deps_failed
)

rem ---------- 5. hand over
%PY% -m atp.cli web %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [!] Console exited with code %RC%.
  set "HOLD=1"
)
if defined HOLD (
  echo.
  pause
)
endlocal & exit /b %RC%

:no_python
echo.
echo [!] Python 3.10+ not found on this machine.
echo     Install it from https://www.python.org/downloads/
echo     and tick "Add python.exe to PATH" during setup.
echo.
pause
endlocal & exit /b 1

:old_python
echo.
echo [!] Python 3.10+ is required, found an older one.
echo     Upgrade Python, then delete the "venv" folder and run this again.
echo.
pause
endlocal & exit /b 1

:deps_failed
echo.
echo [!] Dependency install failed. Try manually:
echo     %PY% -m pip install -r requirements.txt
echo.
pause
endlocal & exit /b 1
