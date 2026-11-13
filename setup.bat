@echo off
REM ============================================================
REM  LANWatch setup
REM  Creates .venv and installs the app + dev dependencies with
REM  network-resilient pip options and mirror fallback.
REM  Safe to run many times. Windows 10/11, no admin rights needed.
REM
REM  Usage:  setup.bat          normal: banner, progress, pause
REM          setup.bat /quiet   used by run.bat: warnings/errors
REM                             only, no pause, same exit codes
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PYTHON_URL=https://www.python.org/downloads/windows/"
set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "REQ_APP=lanwatch\requirements.txt"
set "REQ_DEV=requirements-dev.txt"

REM Optional first argument "/quiet" (case-insensitive): silence the banner and
REM the success chatter and skip the final pause. run.bat uses it for automatic
REM installs and repairs. Warnings and errors are printed in both modes.
set "QUIET="
if /i "%~1"=="/quiet" set "QUIET=1"
set "SAY=echo"
if defined QUIET set "SAY=rem"

%SAY% ============================================================
%SAY%  LANWatch - setup
%SAY% ============================================================
%SAY%  Everything is installed into a local "%VENV_DIR%" folder.
%SAY%  No admin rights, no PATH changes, safe to run again.
%SAY% ============================================================
%SAY%.

REM ---------- 1) Python must exist and be 3.11 or newer ----------
REM Prefer the "py" launcher as "py -3"; fall back to a bare "python".
REM The interpreter name lives in PY and its arguments in PY_ARGS so that every
REM call below can be quoted ("%PY%") and still run: a value like "py -3" must
REM not be put inside the quotes, cmd would look for a file named "py -3".
set "PY=python"
set "PY_ARGS="
where py >nul 2>&1
if errorlevel 1 goto :python_ready
py -3 --version >nul 2>&1
if errorlevel 1 goto :python_ready
set "PY=py"
set "PY_ARGS=-3"

:python_ready
"%PY%" %PY_ARGS% --version >nul 2>&1
if errorlevel 1 goto :no_python

"%PY%" %PY_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto :old_python

%SAY% [OK] Python is available:
if not defined QUIET "%PY%" %PY_ARGS% --version

REM ---------- 2) Virtual environment ----------
if exist "%VENV_PY%" goto :venv_ready

%SAY% [setup] Creating the virtual environment in "%VENV_DIR%" ...
"%PY%" %PY_ARGS% -m venv "%VENV_DIR%"
if errorlevel 1 goto :venv_failed

:venv_ready
if not exist "%VENV_DIR%\Scripts\activate.bat" goto :venv_broken
call "%VENV_DIR%\Scripts\activate.bat"
%SAY% [OK] Virtual environment ready: "%VENV_DIR%"
%SAY%.

REM ---------- 3) pip ----------
%SAY% [setup] Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip --default-timeout=120 --retries 5 --trusted-host pypi.org --trusted-host files.pythonhosted.org
if errorlevel 1 (
    echo [WARN] Could not upgrade pip. Continuing with the pip inside "%VENV_DIR%".
)
%SAY%.

REM ---------- 4) Dependencies: pypi.org first, then mirrors ----------
%SAY% [setup] Installing dependencies from pypi.org ...
"%VENV_PY%" -m pip install --default-timeout=120 --retries 5 --trusted-host pypi.org --trusted-host files.pythonhosted.org -r "%REQ_APP%" -r "%REQ_DEV%"
if not errorlevel 1 goto :deps_ok

echo.
echo [WARN] Installing from pypi.org failed (see the pip messages above).
echo [WARN] Trying fallback mirrors. This can take a while on a slow network.
echo.

call :try_mirror "https://pypi.tuna.tsinghua.edu.cn/simple" "pypi.tuna.tsinghua.edu.cn"
if not errorlevel 1 goto :deps_ok
call :try_mirror "https://mirror.runflare.com/pypi/simple" "mirror.runflare.com"
if not errorlevel 1 goto :deps_ok
call :try_mirror "https://mirrors.aliyun.com/pypi/simple" "mirrors.aliyun.com"
if not errorlevel 1 goto :deps_ok

goto :deps_failed

:deps_ok
%SAY% [OK] Dependencies installed.
%SAY%.

REM ---------- 5) Verify the imports the app needs ----------
%SAY% [setup] Verifying the installation ...
set "MISSING="
for %%M in (websockets zeroconf customtkinter) do (
    "%VENV_PY%" -c "import %%M" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python module "%%M" could not be imported.
        set "MISSING=1"
    ) else (
        %SAY% [OK] %%M imports correctly.
    )
)

REM python-mpv-jsonipc installs the module "python_mpv_jsonipc" (that is the
REM name player.py imports). Some builds also expose "mpv", so accept either.
"%VENV_PY%" -c "import python_mpv_jsonipc" >nul 2>&1
if not errorlevel 1 goto :mpv_ok
"%VENV_PY%" -c "import mpv" >nul 2>&1
if not errorlevel 1 goto :mpv_ok
echo [ERROR] The mpv binding could not be imported (tried "python_mpv_jsonipc" and "mpv").
set "MISSING=1"
goto :mpv_done

:mpv_ok
%SAY% [OK] python-mpv-jsonipc imports correctly.

:mpv_done
if defined MISSING goto :verify_failed

%SAY%.
%SAY% Setup complete. Double-click run.bat to start LANWatch.
%SAY%.
if not defined QUIET pause
exit /b 0

REM ============================================================
REM  Subroutines and error paths (never reached by fall-through)
REM ============================================================

:try_mirror
REM %1 = index URL, %2 = mirror host name
%SAY% [mirror] Trying %~2 ...
"%VENV_PY%" -m pip install --default-timeout=120 --retries 5 --trusted-host pypi.org --trusted-host files.pythonhosted.org -i "%~1" --trusted-host "%~2" -r "%REQ_APP%" -r "%REQ_DEV%"
if not errorlevel 1 (
    %SAY% [OK] Installed from %~2.
    exit /b 0
)
echo [WARN] Mirror %~2 did not work; trying the next option.
exit /b 1

:no_python
echo.
echo [ERROR] Python 3.11 or newer was not found.
echo         Tried "py -3" and "python".
echo         Download it from: %PYTHON_URL%
echo         During installation tick "Add python.exe to PATH".
echo         If "py -3 --version" works but "python" does not, reinstall
echo         Python with the PATH option enabled.
goto :failed

:old_python
echo.
echo [ERROR] Your Python is older than 3.11.
"%PY%" %PY_ARGS% --version
echo         Install Python 3.11 or newer from: %PYTHON_URL%
echo         Then run setup.bat again.
goto :failed

:venv_failed
echo.
echo [ERROR] Could not create the virtual environment in "%VENV_DIR%".
echo         Check that you can write in this folder and that Python is
echo         installed correctly, then run setup.bat again.
goto :failed

:venv_broken
echo.
echo [ERROR] The folder "%VENV_DIR%" exists but its activation script is missing.
echo         Delete that folder and run setup.bat again.
goto :failed

:deps_failed
echo.
echo [ERROR] Installing dependencies failed on pypi.org and on every mirror.
echo         What to try next:
echo           - Check your internet connection and disable VPN for a moment.
echo           - Run setup.bat again; it is safe to repeat and resumes progress.
echo           - If pip keeps timing out, create %%APPDATA%%\pip\pip.ini as
echo             described in README.md ("If pip keeps timing out").
echo           - Last resort, install by hand to see the full pip error:
echo             ".venv\Scripts\python.exe" -m pip install -r lanwatch\requirements.txt
goto :failed

:verify_failed
echo.
echo [ERROR] Some modules are missing after installation.
echo         Run setup.bat again (safe to repeat). If the same module keeps
echo         failing, install the packages by hand to see the full error:
echo           ".venv\Scripts\python.exe" -m pip install websockets zeroconf customtkinter python-mpv-jsonipc
goto :failed

:failed
echo.
echo Setup did not finish. Fix the problem above and run setup.bat again.
echo.
if not defined QUIET pause
exit /b 1
