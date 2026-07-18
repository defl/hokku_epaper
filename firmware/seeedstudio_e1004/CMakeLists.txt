cmake_minimum_required(VERSION 3.16)

# Read firmware version from VERSION file (format: PROTOCOL.CONFIG.N) — the same
# scheme huessen_epf1301 uses, so the shared config.h (CONFIG_VERSION =
# FW_CONFIG_VERSION) works unchanged.
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/VERSION" _FW_VER_RAW)
string(STRIP "${_FW_VER_RAW}" FIRMWARE_VERSION)
string(REPLACE "." ";" _FW_VER_LIST "${FIRMWARE_VERSION}")
list(GET _FW_VER_LIST 0 FW_PROTOCOL)
list(GET _FW_VER_LIST 1 FW_CONFIG)
list(GET _FW_VER_LIST 2 FW_N)

set(PROJECT_VER "${FIRMWARE_VERSION}")

# Build timestamp embedded in the app descriptor (X-Firmware-Build).
string(TIMESTAMP BUILD_TIMESTAMP "%Y%m%d%H%M%SZ" UTC)

configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/main/version.h.in"
    "${CMAKE_BINARY_DIR}/version.h"
)

set(IDF_TARGET "esp32s3")
set(EXTRA_COMPONENT_DIRS "")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(hokku_seeedstudio_e1004)
