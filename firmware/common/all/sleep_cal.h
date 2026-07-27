// SoC-agnostic deep-sleep oscillator-drift calibration, shared by ALL hokku
// firmwares. Pure C — no ESP-IDF or XR872 SDK headers, integer-only math so it
// is bit-identical on every board and fully host-testable.
//
// Screens deep-sleep on their SoC's low-power RC oscillator, which drifts by
// several minutes over a multi-hour sleep. Open-loop scheduling ("sleep N s")
// therefore wakes the device early/late relative to its scheduled slot; an early
// wake near a slot used to trigger a near-immediate second refresh. This module
// closes the loop: the firmware measures how long it *actually* slept versus what
// it armed, learns the oscillator's error, and pre-distorts the next timer so the
// device lands on the slot.
//
// The correction is a ppm deviation from nominal:
//     cal_ppm = (m - 1) * 1e6,   m = actual_wall_seconds / seconds_armed
//   cal_ppm > 0  -> oscillator runs slow  (sleeps longer than armed)
//   cal_ppm < 0  -> oscillator runs fast  (wakes early)
// ppm (not a float) is the canonical representation everywhere — RTC, NVS, and
// the wire — so no board needs floating point and the value round-trips exactly.
#pragma once

#include <stdint.h>
#include <stdbool.h>

/* Correction is clamped to +/-15%: beyond this the reading is a fault (bad
 * server clock, missed cycle, dying oscillator), not real drift. */
#define SLEEP_CAL_CLAMP_PPM     150000

/* Steady-state EMA weight for a new sample is 1/ALPHA_DEN. Early samples use a
 * larger weight (1/(n+1)) so a fresh device converges in a few cycles instead of
 * crawling; see sleep_cal_update. */
#define SLEEP_CAL_ALPHA_DEN     4

/* Sleeps shorter than this are dominated by fixed wake/settle overhead and give
 * a noisy ratio, so they are not used to learn drift. */
#define SLEEP_CAL_MIN_SAMPLE_S  1800

/* Server-side sample count required before a device adopts a seed. */
#define SLEEP_CAL_MIN_SEED_N    3

typedef struct {
    int32_t cal_ppm;   /* new correction (clamped) */
    bool    updated;   /* true if the sample was accepted into the estimate */
} sleep_cal_result_t;

/* Fold one observed sleep into the running estimate. The sample is rejected
 * (result.updated == false, cal_ppm unchanged) when armed_s is below
 * SLEEP_CAL_MIN_SAMPLE_S or the observed ratio actual/armed falls outside
 * [0.5, 2.0] (a missed or doubled cycle, not drift). On acceptance the estimate
 * moves toward the observation by 1/min(sample_count+1, ALPHA_DEN) — a full jump
 * on the first sample, easing to a stable EMA once warmed up. Result is clamped
 * to +/-SLEEP_CAL_CLAMP_PPM. Integer-only. */
sleep_cal_result_t sleep_cal_update(int32_t cal_ppm, uint16_t sample_count,
                                    int64_t actual_slept_s, int32_t armed_s);

/* Timer value (microseconds) to arm so the *actual* sleep lands on
 * desired_wall_s despite drift: armed = desired / (1 + cal_ppm/1e6). Clamped to
 * at least 1 s. desired_wall_s <= 0 returns 1 s. */
int64_t sleep_cal_apply_us(int32_t cal_ppm, int64_t desired_wall_s);

/* Clamp a raw ppm (from NVS or a server seed header) to the valid range. */
int32_t sleep_cal_clamp_ppm(int32_t cal_ppm);

/* Seed-adoption policy: a device adopts the server's pinned mean only when it has
 * no calibration of its own yet and the mean is backed by enough measurements. */
bool sleep_cal_should_adopt(bool locally_calibrated, int server_n);
