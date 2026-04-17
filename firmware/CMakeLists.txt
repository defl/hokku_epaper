cmake_minimum_required(VERSION 3.16)

# Generate build timestamp as version: YYYYMMDDHHMMSSZ
# This is embedded in the ESP-IDF app description at a fixed offset (bytes 48-79)
# and can be read by the configurator to detect firmware versions.
string(TIMESTAMP BUILD_TIMESTAMP "%Y%m%d%H%M%SZ" UTC)
set(PROJECT_VER "${BUILD_TIMESTAMP}")

set(EXTRA_COMPONENT_DIRS "")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(hokku_epaper)
