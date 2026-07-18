cmake_minimum_required(VERSION 3.16)
project(hokku_bigme_f7_tests C)
set(CMAKE_C_STANDARD 11)

if (NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Catch missing stubs at compile time rather than as silent linker failures.
if (CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-Werror=implicit-function-declaration)
endif()

# ── test_logic ────────────────────────────────────────────────────────────
# Compiles test_logic.c, which #include-s hokku_config.c, led.c, and main.c
# with all static storage classes stripped, all as ONE translation unit (not
# led.c as a separate compiled object — that would give it its own private
# copy of the mocks/driver/chip/hal_gpio.h _mock_gpio[] array via C's normal
# per-TU `static` linkage, leaving main.c's led_usb_present() calls unable to
# see what a test set). mocks/ shadows the real XR872 SDK headers (paths
# mirror the real SDK tree so main.c's #include lines resolve unchanged).
add_executable(test_logic
    test_logic.c
)
target_include_directories(test_logic PRIVATE
    mocks               # mock XR872 SDK headers (searched first)
    ../..                # for epd.h / led.h / hokku_config.h (real, local)
    ../../../common/all  # SoC-agnostic shared: firmware_url.h / frame_state.h
)

# ── test_config ───────────────────────────────────────────────────────────
# Tests hokku_config_load/get/save in isolation; only needs the fdcm mock.
add_executable(test_config
    test_config.c
)
target_include_directories(test_config PRIVATE
    mocks
    ../..
)

# ── CTest integration ────────────────────────────────────────────────────
enable_testing()
add_test(NAME logic  COMMAND test_logic)
add_test(NAME config COMMAND test_config)
