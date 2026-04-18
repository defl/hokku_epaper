@echo off
set MSYSTEM=
set IDF_PATH=C:\esp\v5.5.3\esp-idf
set IDF_PYTHON_ENV_PATH=C:\Espressif\tools\python\v5.5.3\venv
set IDF_TOOLS_PATH=C:\Espressif
set PATH=C:\Espressif\tools\xtensa-esp-elf\esp-14.2.0_20251107\xtensa-esp-elf\bin;C:\Espressif\tools\ninja\1.12.1;C:\Espressif\tools\cmake\3.30.5\bin;C:\Espressif\tools\python\v5.5.3\venv\Scripts;%PATH%
cd /d C:\Users\defl\workspace\hokku_epaper\.claude\worktrees\amazing-burnell-b76a2a\firmware
C:\Espressif\tools\python\v5.5.3\venv\Scripts\python.exe %IDF_PATH%\tools\idf.py %*
