@echo off
setlocal enabledelayedexpansion

REM --- Elevation check ---
REM The Pi OS SD-card imaging phase needs admin (raw disk writes).
REM The ESP32 phase does not. Ask the user; only elevate if needed.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo This installer has two phases:
    echo   1. Flash a Raspberry Pi OS SD card  ^(requires Administrator^)
    echo   2. Configure / flash the ESP32 frame ^(does not^)
    echo.
    set /p "DOPI=Will you be flashing an SD card in this run? [y/N]: "
    if /i "!DOPI!"=="y" (
        echo.
        echo Relaunching with Administrator privileges ^(accept the UAC prompt^)...
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0' -Verb RunAs"
        exit /b 0
    )
    echo Continuing without elevation ^(ESP32-only mode^).
    echo.
)

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
pip install pyserial esptool
if %errorlevel% neq 0 (
    echo Failed to install dependencies.
    exit /b 1
)

REM Run the setup tool
echo.
python tools\hokku_setup.py
