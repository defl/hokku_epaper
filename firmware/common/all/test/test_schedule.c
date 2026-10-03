// Unit tests for schedule (pure): when every screen fetches next and how it
// learns its oscillator drift from the server clock.
#include <string.h>

#include "test_harness.h"

#include "../backoff.c"
#include "../fetch_outcome.c"
#include "../sleep_cal.c"
#include "../schedule.c"

#define T0  1700000000LL   /* a server time */

static hokku_sched_now_t at(int64_t now_epoch, int64_t awake_s, bool timer_wake)
{
    hokku_sched_now_t n = { .now_epoch = now_epoch, .mono_us = 5000000LL,
                            .awake_s = awake_s, .timer_wake = timer_wake };
    return n;
}

static hokku_fetch_result_t image(int64_t server_epoch, int32_t sleep_s)
{
    hokku_fetch_result_t r = { .http_status = 200, .image_ok = true, .sleep_s = sleep_s,
                               .server_epoch = server_epoch };
    return r;
}

static void test_anchored_to_server_clock(void)
{
    hokku_sched_t s;
    hokku_sched_init(&s);
    hokku_fetch_result_t r = image(T0, 3600);
    hokku_sched_now_t n = at(T0 + 999, 20, false);   /* device clock off by 999 s */
    hokku_fetch_outcome_t o = hokku_sched_apply_fetch(&s, &r, &n);
    CHECK(o.action == HOKKU_FETCH_DISPLAY && s.next_epoch == T0 + 3600,
          "schedule: next fetch = server time + X-Sleep-Seconds, not device clock");
    CHECK(s.sleep_s == 3600 && s.failures == 0, "schedule: interval and streak stored");

    hokku_fetch_result_t keep = { .http_status = 404, .sleep_s = 21600, .server_epoch = T0 };
    o = hokku_sched_apply_fetch(&s, &keep, &n);
    CHECK(o.action == HOKKU_FETCH_KEEP && s.next_epoch == T0 + 21600,
          "schedule: a keep reply is anchored to the server clock too");
}

static void test_relative_without_server_time(void)
{
    hokku_sched_t s;
    hokku_sched_init(&s);
    hokku_fetch_result_t down = { .http_status = 0 };
    hokku_sched_now_t n = at(T0, 5, false);
    hokku_fetch_outcome_t o = hokku_sched_apply_fetch(&s, &down, &n);
    CHECK(o.action == HOKKU_FETCH_BACKOFF && s.next_epoch == T0 + HOKKU_RETRY_BASE_S &&
          s.failures == 1, "schedule: outage retries relative to the device clock");

    o = hokku_sched_apply_fetch(&s, &down, &n);
    CHECK(s.failures == 2 && s.next_epoch == T0 + 2 * HOKKU_RETRY_BASE_S,
          "schedule: the stored streak doubles the next backoff");

    hokku_sched_now_t unset = at(0, 5, false);
    hokku_sched_apply_fetch(&s, &down, &unset);
    CHECK(s.next_epoch == -(5000000LL + 4LL * HOKKU_RETRY_BASE_S * 1000000LL),
          "schedule: clock unset -> monotonic tick deadline");

    hokku_fetch_result_t r = image(0, 900);   /* image, no server time */
    o = hokku_sched_apply_fetch(&s, &r, &n);
    CHECK(o.action == HOKKU_FETCH_DISPLAY && s.next_epoch == T0 + 900 && s.failures == 0,
          "schedule: image without server time sleeps its interval from now");
}

static void test_learns_drift_on_timer_wake(void)
{
    hokku_sched_t s;
    hokku_sched_init(&s);
    s.next_epoch = T0 + 43200;            /* the slot */
    s.sleep_start_epoch = T0;
    s.armed_s = 43200;
    /* Woke 432 s late (1% slow); the reply arrived after 30 s awake. */
    hokku_fetch_result_t r = image(T0 + 43200 + 432 + 30, 3600);
    hokku_sched_now_t n = at(T0 + 43200 + 432 + 30, 30, true);
    hokku_sched_apply_fetch(&s, &r, &n);
    CHECK(s.cal_samples == 1 && s.cal_ppm == 10000,
          "drift: +1% slow learned from the server clock (awake time excluded)");
    CHECK(s.sleep_err_known && s.sleep_err_s == 432, "drift: wake landed 432 s after the slot");
    CHECK(s.sleep_start_epoch == 0, "drift: the sample is consumed");

    /* The same reply again (a retry in the same boot) must not count twice. */
    hokku_sched_apply_fetch(&s, &r, &n);
    CHECK(s.cal_samples == 1, "drift: one sample per sleep");
}

static void test_no_sample_without_timer_or_server(void)
{
    hokku_sched_t s;
    hokku_sched_init(&s);
    s.sleep_start_epoch = T0;
    s.armed_s = 43200;
    hokku_fetch_result_t r = image(T0 + 43632, 3600);
    hokku_sched_now_t button = at(T0 + 43632, 10, false);
    hokku_sched_apply_fetch(&s, &r, &button);
    CHECK(s.cal_samples == 0 && s.sleep_start_epoch == 0,
          "drift: a non-timer wake is no sample, and drops the start");

    s.sleep_start_epoch = T0;
    hokku_fetch_result_t down = { .http_status = 0 };
    hokku_sched_now_t timer = at(T0 + 43632, 10, true);
    hokku_sched_apply_fetch(&s, &down, &timer);
    CHECK(s.cal_samples == 0 && s.sleep_start_epoch == 0,
          "drift: no server time, no sample (the device clock would measure itself)");
}

static void test_seed_adoption(void)
{
    hokku_sched_t s;
    hokku_sched_init(&s);
    hokku_sched_now_t n = at(T0, 1, false);
    hokku_fetch_result_t r = image(T0, 3600);
    r.cal_seed_ppm = 8000;
    r.cal_seed_n = 5;
    hokku_sched_apply_fetch(&s, &r, &n);
    CHECK(s.cal_ppm == 8000 && s.cal_samples == 1, "seed: uncalibrated screen adopts it");

    r.cal_seed_ppm = -3000;
    hokku_sched_apply_fetch(&s, &r, &n);
    CHECK(s.cal_ppm == 8000, "seed: a calibrated screen keeps its own");

    hokku_sched_init(&s);
    r.cal_seed_n = 2;
    hokku_sched_apply_fetch(&s, &r, &n);
    CHECK(s.cal_samples == 0, "seed: too few server samples -> ignored");

    hokku_sched_init(&s);
    hokku_fetch_result_t keep = { .http_status = 404, .sleep_s = 600, .server_epoch = T0,
                                  .cal_seed_ppm = 500, .cal_seed_n = 9 };
    hokku_sched_apply_fetch(&s, &keep, &n);
    CHECK(s.cal_ppm == 500, "seed: a keep reply (server reached) adopts too");

    hokku_sched_init(&s);
    hokku_fetch_result_t bad = { .http_status = 500, .server_epoch = T0,
                                 .cal_seed_ppm = 500, .cal_seed_n = 9 };
    hokku_sched_apply_fetch(&s, &bad, &n);
    CHECK(s.cal_samples == 0, "seed: an outage reply does not adopt");

    hokku_sched_init(&s);
    r.cal_seed_ppm = 999999;
    r.cal_seed_n = 9;
    hokku_sched_apply_fetch(&s, &r, &n);
    CHECK(s.cal_ppm == SLEEP_CAL_CLAMP_PPM, "seed: clamped on adoption");
}

static void test_arm_sleep(void)
{
    hokku_sched_t s;
    hokku_sched_init(&s);
    s.next_epoch = T0 + 3600;
    int64_t armed = hokku_sched_arm_sleep(&s, T0 + 30, 0, HOKKU_FALLBACK_SLEEP_S, 0);
    CHECK(armed == 3570 && s.armed_s == 3570 && s.sleep_start_epoch == T0 + 30,
          "arm: remaining time to the slot, recorded as a sample");

    s.cal_ppm = 10000;                    /* oscillator 1% slow: arm less */
    armed = hokku_sched_arm_sleep(&s, T0, 0, HOKKU_FALLBACK_SLEEP_S, 0);
    CHECK(armed == 3564, "arm: drift taken out of the timer");

    s.next_epoch = T0 + 100000;
    s.cal_ppm = 0;
    armed = hokku_sched_arm_sleep(&s, T0, 0, HOKKU_FALLBACK_SLEEP_S, 60000);
    CHECK(armed == 60000 && s.armed_s == 60000, "arm: capped at the board's timer limit");

    s.next_epoch = T0 - 50;               /* already due */
    armed = hokku_sched_arm_sleep(&s, T0, 0, HOKKU_FALLBACK_SLEEP_S, 0);
    CHECK(armed == 1, "arm: an overdue slot sleeps 1 s");

    s.next_epoch = T0 + 3600;
    armed = hokku_sched_arm_sleep(&s, 0, 0, HOKKU_FALLBACK_SLEEP_S, 0);
    CHECK(s.sleep_start_epoch == 0 && s.armed_s == 0,
          "arm: clock unset -> not a drift sample");

    s.next_epoch = -(2000000LL + 45000000LL);   /* tick deadline 45 s after mono 2 s */
    armed = hokku_sched_arm_sleep(&s, T0, 2000000LL, HOKKU_FALLBACK_SLEEP_S, 0);
    CHECK(armed == 45 && s.armed_s == 0, "arm: tick deadline honoured, not a sample");

    s.next_epoch = 0;
    armed = hokku_sched_arm_sleep(&s, T0, 0, HOKKU_FALLBACK_SLEEP_S, 0);
    CHECK(armed == HOKKU_FALLBACK_SLEEP_S && s.armed_s == 0, "arm: unscheduled -> fallback");
}

static void test_due_and_remaining(void)
{
    hokku_sched_t s;
    hokku_sched_init(&s);
    CHECK(hokku_sched_due(&s, T0, 0), "due: unscheduled is due");
    s.next_epoch = T0 + 10;
    CHECK(!hokku_sched_due(&s, T0, 0) && hokku_sched_due(&s, T0 + 10, 0), "due: at the slot");
    CHECK(!hokku_sched_due(&s, 0, 0), "due: clock unset -> not due");
    CHECK(hokku_sched_remaining_s(&s, T0, 0, 99) == 10, "remaining: to the slot");
    s.next_epoch = -3000000LL;
    CHECK(hokku_sched_due(&s, 0, 3000000LL) && !hokku_sched_due(&s, 0, 2999999LL),
          "due: tick deadline");
    hokku_sched_retry_in(&s, 120, T0, 0);
    CHECK(s.next_epoch == T0 + 120, "retry_in: from the device clock");
}

static void test_wake_estimate(void)
{
    hokku_sched_t s;
    hokku_sched_init(&s);
    CHECK(hokku_sched_wake_estimate(&s) == 0, "estimate: unknown without a sample");
    s.sleep_start_epoch = T0;
    s.armed_s = 3564;
    s.cal_ppm = 10000;
    CHECK(hokku_sched_wake_estimate(&s) == T0 + 3599,
          "estimate: armed time stretched by the learned drift");
}

int main(void)
{
    test_anchored_to_server_clock();
    test_relative_without_server_time();
    test_learns_drift_on_timer_wake();
    test_no_sample_without_timer_or_server();
    test_seed_adoption();
    test_arm_sleep();
    test_due_and_remaining();
    test_wake_estimate();
    TEST_MAIN_END();
}
