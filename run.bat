@echo off
REM LANWatch launcher. A healthy .venv launches straight away; a missing or
REM broken one is repaired through setup.bat /quiet (no pause inside setup).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [LANWatch] First run: setting up dependencies - this may take a minute...
    call setup.bat /quiet
    if errorlevel 1 goto :setup_failed
)

".venv\Scripts\python.exe" -c "import websockets, zeroconf, customtkinter, python_mpv_jsonipc" >nul 2>&1
if errorlevel 1 (
    echo [LANWatch] Dependencies look broken, repairing...
    call setup.bat /quiet
    if errorlevel 1 goto :setup_failed
    ".venv\Scripts\python.exe" -c "import websockets, zeroconf, customtkinter, python_mpv_jsonipc" >nul 2>&1
    if errorlevel 1 goto :setup_failed
)

if not exist "lanwatch\main.py" (
    echo [LANWatch] lanwatch\main.py not found. Is the repo complete?
    pause
    exit /b 1
)

echo [LANWatch] Starting...
".venv\Scripts\python.exe" lanwatch\main.py
if errorlevel 1 goto :app_error
goto :eof

:setup_failed
echo.
echo Setup failed. Run setup.bat manually for details.
pause
exit /b 1

:app_error
echo.
echo LANWatch exited with an error. See messages above.
pause
exit /b 1
