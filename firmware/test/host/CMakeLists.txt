cmake_minimum_required(VERSION 3.16)
project(hokku_firmware_tests C)
set(CMAKE_C_STANDARD 11)

if (NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Catch missing stubs at compile time rather than as silent linker failures.
# Avoid -Wall/-Wextra: the included firmware source has intentional unused
# variables and format-string quirks that would pollute CI output.
if (CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-Werror=implicit-function-declaration)
endif()

# ── test_text_render ──────────────────────────────────────────────────────
# Pure test: no ESP-IDF dependencies.
add_executable(test_text_render
    test_text_render.c
    ../../main/text_render.c
)
target_include_directories(test_text_render PRIVATE
    ../../main          # for text_render.h
)

# ── test_logic ────────────────────────────────────────────────────────────
# Compiles test_logic.c, which #include-s the firmware source with all static
# storage classes stripped. mocks/ shadows the real ESP-IDF headers.
# win_compat/ provides POSIX stubs missing from MSVC (sys/time.h, etc.).
add_executable(test_logic
    test_logic.c
)
target_include_directories(test_logic PRIVATE
    mocks               # mock ESP-IDF headers (searched first)
    ../../main          # for text_render.h included by main.c
)
if (WIN32)
    target_include_directories(test_logic PRIVATE win_compat)
endif()

# ── CTest integration ────────────────────────────────────────────────────
enable_testing()
add_test(NAME text_render COMMAND test_text_render)
add_test(NAME logic       COMMAND test_logic)
