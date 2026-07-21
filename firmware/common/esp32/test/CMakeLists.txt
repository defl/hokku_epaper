cmake_minimum_required(VERSION 3.16)
project(hokku_common_esp32_tests C)
set(CMAKE_C_STANDARD 11)

if (NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

if (CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-Werror=implicit-function-declaration)
endif()

# Tests the shared ESP-IDF modules (config/state/scheduler/log + compile-link of
# wifi/net/ota) against the mock kit in this dir. A test-only version.h (this
# dir) satisfies config.h's include.
add_executable(test_logic test_logic.c)
target_include_directories(test_logic PRIVATE
    mocks        # bare "driver/gpio.h" etc.
    .            # "mocks/..."-prefixed includes + the test-only version.h
    ..           # the common/esp32 headers (config.h, state.h, ...)
    ../../all    # common/all headers
)
if (WIN32)
    target_include_directories(test_logic PRIVATE win_compat)
endif()

enable_testing()
add_test(NAME logic COMMAND test_logic)
