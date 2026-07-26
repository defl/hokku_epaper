@echo off
cd /d "%~dp0"

if "%1"=="" (
    echo Usage: run.bat ^<config.json path^>
    echo.
    echo Example: run.bat config.json
    echo   ^(copy hokku\webserver\config\config.json.example to config.json first^)
    echo.
    exit /b 1
)

..\\.venv\Scripts\python -m hokku.webserver "%1"
