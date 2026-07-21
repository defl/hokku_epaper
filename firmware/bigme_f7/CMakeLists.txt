# Bigme F7 (XR872) firmware — CMake build wrapper.
#
# Unlike the ESP32 firmware (ESP-IDF, CMake-native), the XR872 firmware builds with
# the XRADIO "Skylark" SDK's own GNU-make system inside a Docker toolchain image.
# This CMakeLists gives a uniform `cmake --build` entry that drives that build via
# build.sh and collects the flashable AWIH image into
# firmware/release/hokku-bigme_f7-<version>.img.
#
# Usage:
#   cmake -B build
#   cmake --build build            # runs the Docker build (needs xr872_sdk + image)
#
# Env passthrough (set before configuring, or export): XR872_SDK, BUILDER_IMAGE.
cmake_minimum_required(VERSION 3.16)
project(hokku_bigme_f7 NONE)

add_custom_target(firmware ALL
    COMMAND bash "${CMAKE_CURRENT_SOURCE_DIR}/build.sh"
    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
    COMMENT "Building Bigme F7 firmware via the XR872 SDK (Docker) -> firmware/release/"
    USES_TERMINAL
    VERBATIM)
