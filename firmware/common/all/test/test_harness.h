// Minimal zero-dependency test harness shared by the common/all unit tests.
// Same CHECK/pass/fail convention the firmware host suites use, factored out so
// the common/all tests (and, later, the firmwares) don't each re-copy it.
#pragma once

#include <stdio.h>

static int g_pass = 0;
static int g_fail = 0;

#define CHECK(cond, name) do {                                  \
    if (cond) { printf("PASS  %s\n", name); g_pass++; }         \
    else      { printf("FAIL  %s\n", name); g_fail++; }         \
} while (0)

#define TEST_MAIN_END() do {                                    \
    printf("\n%d passed, %d failed\n", g_pass, g_fail);         \
    return g_fail > 0 ? 1 : 0;                                  \
} while (0)
