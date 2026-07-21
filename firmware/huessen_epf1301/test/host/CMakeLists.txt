cmake_minimum_required(VERSION 3.16)
project(hokku_firmware_tests C)
set(CMAKE_C_STANDARD 11)

# Generate version.h from firmware/VERSION so host tests see the same defines as firmware builds
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../VERSION" _FW_VER_RAW)
string(STRIP "${_FW_VER_RAW}" FIRMWARE_VERSION)
string(REPLACE "." ";" _FW_VER_LIST "${FIRMWARE_VERSION}")
list(GET _FW_VER_LIST 0 FW_PROTOCOL)
list(GET _FW_VER_LIST 1 FW_CONFIG)
list(GET _FW_VER_LIST 2 FW_N)
string(TIMESTAMP BUILD_TIMESTAMP "%Y%m%d%H%M%SZ" UTC)
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/../../main/version.h.in"
    "${CMAKE_BINARY_DIR}/version.h"
)

if (NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Catch missing stubs at compile time rather than as silent linker failures.
# Avoid -Wall/-Wextra: the included firmware source has intentional unused
# variables and format-string quirks that would pollute CI output.
if (CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-Werror=implicit-function-declaration)
endif()

# Shared ESP32 modules now live in firmware/common/esp32/ (compiled into both
# the huessen and seeed firmwares). Relative to this test dir that is
# ../../../common/esp32.
set(HOKKU_COMMON ../../../common/esp32)
set(HOKKU_COMMON_ALL ../../../common/all)
# Shared ESP-IDF mock kit + MSVC POSIX shims (relocated from this dir so the
# seeed host tests reuse them). ${HOKKU_TEST} on the include path resolves the
# firmware sources' `#include "mocks/..."` and `driver/...` lines.
set(HOKKU_TEST ../../../common/esp32/test)

# ── test_text_render ──────────────────────────────────────────────────────
# Pure test: no ESP-IDF dependencies.
add_executable(test_text_render
    test_text_render.c
    ${HOKKU_COMMON}/text_render.c
)
target_include_directories(test_text_render PRIVATE
    ${HOKKU_COMMON}     # for text_render.h
)

# ── test_logic ────────────────────────────────────────────────────────────
# Compiles test_logic.c, which #include-s the firmware source with all static
# storage classes stripped. mocks/ shadows the real ESP-IDF headers.
# win_compat/ provides POSIX stubs missing from MSVC (sys/time.h, etc.).
add_executable(test_logic
    test_logic.c
)
target_include_directories(test_logic PRIVATE
    ${HOKKU_TEST}/mocks # mock ESP-IDF headers (bare "driver/gpio.h" etc.)
    ${HOKKU_TEST}       # resolves the "mocks/..."-prefixed includes
    ${HOKKU_COMMON}     # config.h / text_render.h / state.h / scheduler.h / log.h
    ${HOKKU_COMMON_ALL} # frame_state.h / firmware_url.h / json_util.h
    ../../main          # for any board-local headers
    ${CMAKE_BINARY_DIR} # for generated version.h
)
if (WIN32)
    target_include_directories(test_logic PRIVATE ${HOKKU_TEST}/win_compat)
endif()

# ── test_config ───────────────────────────────────────────────────────────
# Tests config_load() and config_is_valid() in isolation; only needs the NVS mock.
add_executable(test_config
    test_config.c
)
target_include_directories(test_config PRIVATE
    ${HOKKU_TEST}/mocks
    ${HOKKU_TEST}
    ${HOKKU_COMMON}
    ../../main
    ${CMAKE_BINARY_DIR} # for generated version.h
)
if (WIN32)
    target_include_directories(test_config PRIVATE ${HOKKU_TEST}/win_compat)
endif()

# ── CTest integration ────────────────────────────────────────────────────
enable_testing()
add_test(NAME text_render COMMAND test_text_render)
add_test(NAME logic       COMMAND test_logic)
add_test(NAME config      COMMAND test_config)
