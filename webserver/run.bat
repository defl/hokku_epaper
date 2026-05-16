@echo off
cd /d "%~dp0"

if "%1"=="" (
    echo Usage: run.bat ^<config.json path^>
    echo.
    echo Example: run.bat config.json
    echo.
    exit /b 1
)

..\\.venv\Scripts\python -m hokku_server "%1"
