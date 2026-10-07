// ESP-IDF clocks for the shared schedule (common/all/schedule.h).
//
// The scheduling and drift logic is shared by every screen; this file only
// feeds it the ESP32's clocks (time(NULL), esp_timer) and its RTC-resident
// hokku_sched (state.h), and mirrors the calibration to NVS (nvs_cal.h).
#pragma once

#include <time.h>
#include <stdint.h>
#include <stdbool.h>

#include "schedule.h"

/* Current wall-clock epoch, or 0 if the clock has not been set past 2020
 * (guards against reporting/acting on a bogus pre-sync time). */
time_t now_epoch(void);

/* True if the next fetch is due (or nothing is scheduled). */
bool refresh_due(void);

/* Act on one fetch (hokku_sched_apply_fetch) with this boot's clocks, log it,
 * and persist a changed calibration. Drawing is left to the caller. */
hokku_fetch_outcome_t scheduler_apply_fetch(const hokku_fetch_result_t *res);

/* Try again in secs without a fetch (hokku_sched_retry_in), e.g. when the
 * config is unusable. */
void scheduler_retry_in(int32_t secs);

/* Microseconds to arm for the next deep sleep (hokku_sched_arm_sleep);
 * fallback_s when nothing is scheduled. */
int64_t scheduler_next_sleep_us(int64_t fallback_s);
