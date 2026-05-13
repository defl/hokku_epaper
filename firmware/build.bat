@echo off
setlocal EnableDelayedExpansion
set MSYSTEM=
set IDF_PATH=C:\esp\v5.5.3\esp-idf
set IDF_PYTHON_ENV_PATH=C:\Espressif\tools\python\v5.5.3\venv
set IDF_TOOLS_PATH=C:\Espressif
set PATH=C:\Espressif\tools\xtensa-esp-elf\esp-14.2.0_20251107\xtensa-esp-elf\bin;C:\Espressif\tools\ninja\1.12.1;C:\Espressif\tools\cmake\3.30.5\bin;C:\Espressif\tools\python\v5.5.3\venv\Scripts;%PATH%
set PYTHON=C:\Espressif\tools\python\v5.5.3\venv\Scripts\python.exe
set ESPTOOL=%PYTHON% %IDF_PATH%\components\esptool_py\esptool\esptool.py
cd /d C:\Users\defl\workspace\hokku_epaper\firmware

REM If the first argument is "build" with no other flags, force a reconfigure
REM so the CMake timestamp is always today's date, then run the build and
REM merge the output into a single firmware/release/hokku-firmware_<ver>.bin.
if /i "%~1"=="build" if "%~2"=="" (
    %PYTHON% %IDF_PATH%\tools\idf.py reconfigure build
    if errorlevel 1 exit /b %errorlevel%

    REM Read version from the app binary (bytes 0x30..0x4F, null-terminated)
    powershell -NoProfile -Command "$f=[IO.File]::OpenRead('build\hokku_epaper.bin');$f.Seek(0x30,0)|Out-Null;$b=New-Object byte[] 32;$f.Read($b,0,32)|Out-Null;$f.Close();[System.Text.Encoding]::ASCII.GetString($b).Split([char]0)[0]" > "%TEMP%\hokku_ver.txt"
    set /p VERSION=<"%TEMP%\hokku_ver.txt"
    del "%TEMP%\hokku_ver.txt" 2>nul
    if "!VERSION!"=="" (
        echo ERROR: could not read version from build/hokku_epaper.bin
        exit /b 1
    )
    echo Version: !VERSION!

    REM Remove old merged files, write new one
    del /q release\hokku-firmware_*.bin 2>nul
    %ESPTOOL% --chip esp32s3 merge_bin ^
        --output release\hokku-firmware_!VERSION!.bin ^
        0x0     build\bootloader\bootloader.bin ^
        0x8000  build\partition_table\partition-table.bin ^
        0x10000 build\hokku_epaper.bin
    if errorlevel 1 exit /b %errorlevel%
    echo Merged firmware: release\hokku-firmware_!VERSION!.bin
    exit /b 0
)

REM All other idf.py commands pass through unchanged
%PYTHON% %IDF_PATH%\tools\idf.py %*
