// Unit tests for the shared exponential-backoff policy (backoff.c). Pure C, no
// platform headers or mocks.
#include "test_harness.h"

#include "backoff.c"

int main(void)
{
    /* Doubles from base once per prior failure. */
    CHECK(hokku_backoff_seconds(0, 60, 3600) == 60,   "backoff: 0 prior -> base");
    CHECK(hokku_backoff_seconds(1, 60, 3600) == 120,  "backoff: 1 prior -> 2x base");
    CHECK(hokku_backoff_seconds(2, 60, 3600) == 240,  "backoff: 2 prior -> 4x base");
    CHECK(hokku_backoff_seconds(3, 60, 3600) == 480,  "backoff: 3 prior -> 8x base");

    /* Caps at max_seconds; large / UINT_MAX counts stay capped with no overflow. */
    CHECK(hokku_backoff_seconds(6, 60, 3600) == 3600,        "backoff: 60*64=3840 capped to 3600");
    CHECK(hokku_backoff_seconds(100, 60, 3600) == 3600,      "backoff: large count capped");
    CHECK(hokku_backoff_seconds(0xFFFFFFFFu, 60, 3600) == 3600, "backoff: UINT_MAX count capped, no overflow");

    /* Works for the F7's 30 s base too. */
    CHECK(hokku_backoff_seconds(0, 30, 3600) == 30,   "backoff: F7 base 30");
    CHECK(hokku_backoff_seconds(3, 30, 3600) == 240,  "backoff: 30*8 = 240");

    /* Input guards: max<base -> base; base<1 -> 1 (then still doubles). */
    CHECK(hokku_backoff_seconds(0, 60, 10) == 60,     "backoff: max<base -> base");
    CHECK(hokku_backoff_seconds(0, 0, 3600) == 1,     "backoff: base<1 -> 1");
    CHECK(hokku_backoff_seconds(5, 0, 3600) == 32,    "backoff: base clamped to 1 then 1<<5 = 32");

    TEST_MAIN_END();
}
