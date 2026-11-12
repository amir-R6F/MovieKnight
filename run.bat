@echo off
REM LANWatch launcher: creates the venv on first run, then starts the app.
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

if not exist ".venv\Scripts\python.exe" (
    echo [LANWatch] Creating virtual environment ^(first run only^)...
    %PY% -m venv .venv
    if errorlevel 1 goto :error
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r lanwatch\requirements.txt
    if errorlevel 1 goto :error
) else (
    ".venv\Scripts\python.exe" -c "import websockets, customtkinter, python_mpv_jsonipc" >nul 2>&1
    if errorlevel 1 (
        echo [LANWatch] Installing dependencies...
        ".venv\Scripts\python.exe" -m pip install -r lanwatch\requirements.txt
        if errorlevel 1 goto :error
    )
)

echo [LANWatch] Starting...
".venv\Scripts\python.exe" lanwatch\main.py
if errorlevel 1 goto :error
goto :eof

:error
echo.
echo [LANWatch] Something went wrong. The message above explains what.
pause
exit /b 1
