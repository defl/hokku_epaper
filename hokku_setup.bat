@echo off
setlocal

REM Check if python3 is available
where python3 >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON=python3
    goto :found
)

REM Fall back to python (Windows often uses 'python' for Python 3)
where python >nul 2>&1
if %errorlevel% equ 0 (
    python --version 2>&1 | findstr /r "Python 3\." >nul
    if %errorlevel% equ 0 (
        set PYTHON=python
        goto :found
    )
)

echo Python 3 is not installed or not in PATH.
echo Please install Python 3 from https://www.python.org/downloads/
echo Make sure to check "Add Python to PATH" during installation.
exit /b 1

:found
echo Using %PYTHON%

REM Create virtual environment
if not exist .venv (
    echo Creating virtual environment...
    %PYTHON% -m venv .venv
    if %errorlevel% neq 0 (
        echo Failed to create virtual environment.
        exit /b 1
    )
)

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Install dependencies
echo Installing dependencies...
pip install flask pillow numpy pillow-heif pillow-avif-plugin
if %errorlevel% neq 0 (
    echo Failed to install dependencies.
    exit /b 1
)

REM Create image directories if they don't exist
if not exist images\upload mkdir images\upload
if not exist images\cache mkdir images\cache

REM Run the webserver
echo Starting Hokku image server...
python webserver\webserver.py
