@echo off
setlocal

REM ============================================================
REM  hokku-setup launcher
REM
REM  Always elevates to Administrator (Pi OS imaging needs raw
REM  disk access). To avoid the common UAC trap where the elevated
REM  cmd shell can't find the user-scope Python install, we set up
REM  the venv first in the non-elevated pass and then launch the
REM  venv's python.exe directly (by absolute path) with RunAs.
REM ============================================================

net session >nul 2>&1
if %errorlevel% equ 0 (
    goto :already_elevated
)

REM --- non-elevated first pass: resolve python, build venv, install deps ---

where python3 >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON=python3
    goto :py_found
)

where python >nul 2>&1
if %errorlevel% equ 0 (
    python --version 2>&1 | findstr /r "Python 3\." >nul
    if %errorlevel% equ 0 (
        set PYTHON=python
        goto :py_found
    )
)

echo Python 3 is not installed or not in PATH.
echo Please install Python 3 from https://www.python.org/downloads/
echo Make sure to check "Add Python to PATH" during installation.
pause
exit /b 1

:py_found
echo Using %PYTHON%

if not exist .venv (
    echo Creating virtual environment...
    %PYTHON% -m venv .venv
    if %errorlevel% neq 0 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

echo Installing dependencies...
pip install pyserial esptool >nul
if %errorlevel% neq 0 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

REM --- relaunch elevated, targeting the venv's python.exe by absolute path ---
echo.
echo Requesting Administrator privileges (accept the UAC prompt)...
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList 'tools\hokku_setup.py','--pause-on-exit' -WorkingDirectory '%~dp0' -Verb RunAs"
exit /b 0

:already_elevated
REM --- already admin: use the venv python if set up, else python from PATH ---
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" tools\hokku_setup.py --pause-on-exit
) else (
    echo Warning: .venv not found; running with system python. Dependencies may be missing.
    python tools\hokku_setup.py --pause-on-exit
)
