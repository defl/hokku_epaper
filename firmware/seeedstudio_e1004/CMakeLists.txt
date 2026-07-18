cmake_minimum_required(VERSION 3.16)

# Firmware version — plain semver (this screen has no NVS config schema and
# no OTA yet, so it doesn't use huessen_epf1301's PROTOCOL.CONFIG.N scheme;
# see firmware/seeedstudio_e1004/README.md).
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/VERSION" _FW_VER_RAW)
string(STRIP "${_FW_VER_RAW}" FIRMWARE_VERSION)

set(PROJECT_VER "${FIRMWARE_VERSION}")

# Build timestamp kept as debug metadata embedded in the binary app descriptor
string(TIMESTAMP BUILD_TIMESTAMP "%Y%m%d%H%M%SZ" UTC)

# Generate version.h for use by firmware source files
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/main/version.h.in"
    "${CMAKE_BINARY_DIR}/version.h"
)

set(IDF_TARGET "esp32s3")
set(EXTRA_COMPONENT_DIRS "")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(hokku_seeedstudio_e1004)
