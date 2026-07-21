cmake_minimum_required(VERSION 3.16)
project(hokku_seeedstudio_e1004_tests C)
set(CMAKE_C_STANDARD 11)

# Generate version.h from this firmware's VERSION so the host tests see the same
# FW_* defines (incl. FW_CONFIG_VERSION, used by the shared config.h).
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

if (CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-Werror=implicit-function-declaration)
endif()

set(HOKKU_COMMON     ../../../common/esp32)
set(HOKKU_COMMON_ALL ../../../common/all)
# Shared ESP-IDF mock kit + MSVC POSIX shims, reused from common/esp32/test.
set(HOKKU_TEST       ../../../common/esp32/test)

# ── test_logic ────────────────────────────────────────────────────────────
# Compiles test_logic.c, which #include-s the shared modules + this firmware's
# main.c (static-neutralised) as one TU against the shared ESP-IDF mocks. This
# both unit-tests the board-specific frame-state gatherer and proves seeed's
# whole integration links against common/{all,esp32}.
add_executable(test_logic test_logic.c)
target_include_directories(test_logic PRIVATE
    ${HOKKU_TEST}/mocks # bare "driver/gpio.h" etc. from the firmware sources
    ${HOKKU_TEST}       # resolves the "mocks/..."-prefixed includes
    ${HOKKU_COMMON}
    ${HOKKU_COMMON_ALL}
    ../../main
    ${CMAKE_BINARY_DIR}
)
if (WIN32)
    target_include_directories(test_logic PRIVATE ${HOKKU_TEST}/win_compat)
endif()

enable_testing()
add_test(NAME logic COMMAND test_logic)
