@echo off
rem  Stop the background console. Thin wrapper so double-clicking works and
rem  the window stays long enough to read the result (start.bat never pauses
rem  on success, so the pause lives here).
chcp 65001 >nul 2>nul
cd /d "%~dp0"
call "%~dp0start.bat" --stop
echo.
pause
