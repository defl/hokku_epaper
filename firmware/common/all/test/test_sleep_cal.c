// Unit tests for the shared oscillator-drift calibration (sleep_cal.c). Pure C,
// integer-only, no platform headers or mocks.
#include "test_harness.h"

#include "sleep_cal.c"

int main(void)
{
    const int64_t H12 = 43200;  /* a realistic 12 h sleep, seconds */

    /* ── sleep_cal_update: learning ─────────────────────────────── */

    /* Perfect clock: actual == armed -> 0 ppm, accepted. */
    sleep_cal_result_t r = sleep_cal_update(0, 0, H12, (int32_t)H12);
    CHECK(r.updated && r.cal_ppm == 0, "update: perfect clock -> 0 ppm");

    /* First sample jumps fully (den = count+1 = 1). +1% slow = +10000 ppm. */
    r = sleep_cal_update(0, 0, H12 + 432, (int32_t)H12);
    CHECK(r.updated && r.cal_ppm == 10000, "update: +1% slow, first sample full jump");

    /* Fast clock (woke early): -1% = -10000 ppm. */
    r = sleep_cal_update(0, 0, H12 - 432, (int32_t)H12);
    CHECK(r.updated && r.cal_ppm == -10000, "update: -1% fast -> -10000 ppm");

    /* Warmed-up EMA: count>=ALPHA_DEN uses weight 1/4. From 0, obs 10000. */
    r = sleep_cal_update(0, 3, 40000 + 400, 40000);
    CHECK(r.updated && r.cal_ppm == 2500, "update: warmed EMA moves 1/4 toward obs");

    /* EMA converges toward a steady observation (0 -> ... -> ~obs). */
    {
        int32_t cal = 0;
        for (int i = 0; i < 20; i++) {
            r = sleep_cal_update(cal, 10, 40000 + 400, 40000);
            cal = r.cal_ppm;
        }
        CHECK(cal >= 9900 && cal <= 10000, "update: EMA converges to steady obs");
    }

    /* ── sleep_cal_update: rejection ────────────────────────────── */

    /* Too-short sleep is noise -> rejected, value unchanged. */
    r = sleep_cal_update(5000, 0, 1000, 1000);
    CHECK(!r.updated && r.cal_ppm == 5000, "update: sub-min sleep rejected");

    /* Doubled cycle (actual > 2x armed) -> rejected. */
    r = sleep_cal_update(5000, 5, 100000, 40000);
    CHECK(!r.updated && r.cal_ppm == 5000, "update: doubled cycle rejected");

    /* Missed cycle (actual < 0.5x armed) -> rejected. */
    r = sleep_cal_update(5000, 5, 10000, 40000);
    CHECK(!r.updated && r.cal_ppm == 5000, "update: halved cycle rejected");

    /* Extreme-but-in-band drift is clamped to the max. */
    r = sleep_cal_update(0, 0, 80000, 40000);  /* ratio exactly 2.0 -> +1e6 ppm */
    CHECK(r.updated && r.cal_ppm == SLEEP_CAL_CLAMP_PPM, "update: extreme drift clamped");

    /* ── sleep_cal_apply_us ─────────────────────────────────────── */

    CHECK(sleep_cal_apply_us(0, 3600) == 3600000000LL, "apply: no drift -> desired");
    /* Slow clock -> arm LESS so the long sleep lands on target. */
    CHECK(sleep_cal_apply_us(10000, 3600) == 3564000000LL, "apply: slow clock arms less");
    /* Fast clock -> arm MORE. */
    CHECK(sleep_cal_apply_us(-10000, 3600) == 3636000000LL, "apply: fast clock arms more");
    CHECK(sleep_cal_apply_us(0, 0) == 1000000LL, "apply: non-positive desired -> 1 s");

    /* ── sleep_cal_clamp_ppm ────────────────────────────────────── */

    CHECK(sleep_cal_clamp_ppm(999999) == SLEEP_CAL_CLAMP_PPM,  "clamp: high -> +max");
    CHECK(sleep_cal_clamp_ppm(-999999) == -SLEEP_CAL_CLAMP_PPM, "clamp: low -> -max");
    CHECK(sleep_cal_clamp_ppm(12345) == 12345, "clamp: in-range unchanged");

    /* ── sleep_cal_should_adopt ─────────────────────────────────── */

    CHECK(sleep_cal_should_adopt(false, 5),  "adopt: uncalibrated + enough samples");
    CHECK(!sleep_cal_should_adopt(true, 5),  "adopt: already calibrated -> no");
    CHECK(!sleep_cal_should_adopt(false, 2), "adopt: too few server samples -> no");
    CHECK(sleep_cal_should_adopt(false, SLEEP_CAL_MIN_SEED_N), "adopt: exactly min samples");

    TEST_MAIN_END();
}
