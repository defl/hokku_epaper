@echo off
setlocal

:: Resolve repo root (one level above this script's directory)
for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
set "BUILDS_DIR=%REPO_ROOT%\build"

echo Building hokku-server Debian package via Docker...

docker run --rm ^
    --volume "%REPO_ROOT%:/workspace" ^
    --workdir /workspace/webserver ^
    debian:bookworm ^
    bash -c "set -e && apt-get update -qq && apt-get install -y --no-install-recommends build-essential debhelper dh-python python3 python3-setuptools pybuild-plugin-pyproject && dpkg-buildpackage -us -uc -b"

if errorlevel 1 (
    echo Error: Docker build failed.
    exit /b 1
)

if not exist "%BUILDS_DIR%" mkdir "%BUILDS_DIR%"

:: dpkg-buildpackage drops artifacts one level above webserver/ (= repo root)
pushd "%REPO_ROOT%"
for %%f in (hokku-server_*.deb hokku-server_*.buildinfo hokku-server_*.changes) do (
    if exist "%%f" move "%%f" "%BUILDS_DIR%\" >nul
)
popd

dir /b "%BUILDS_DIR%\hokku-server_*.deb" >nul 2>&1
if errorlevel 1 (
    echo Error: dpkg-buildpackage produced no artifacts.
    exit /b 1
)

echo Done. Artifacts in %BUILDS_DIR%:
dir /b "%BUILDS_DIR%\hokku-server_*.deb"
