cmake_minimum_required(VERSION 3.16)
project(hokku_common_all_tests C)
set(CMAKE_C_STANDARD 11)

if (NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# common/all is pure C with no platform headers, so its tests need NO mocks —
# each test #include-s the unit-under-test's .c directly and links standalone.
if (CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-Werror=implicit-function-declaration -Wall)
endif()

enable_testing()

foreach(t logbuf firmware_url json frame_state backoff)
    add_executable(test_${t} test_${t}.c)
    target_include_directories(test_${t} PRIVATE .. .)
    add_test(NAME ${t} COMMAND test_${t})
endforeach()
