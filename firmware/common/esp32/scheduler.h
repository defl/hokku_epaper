// Shared next-refresh scheduling math for hokku ESP32 firmwares.
//
// Operates on the RTC-persistent schedule anchor in hokku_state
// (next_refresh_epoch, pre_sleep_server_epoch, ...). Pure logic — no board
// pins, no display. Both firmwares' regime code uses these to decide when the
// next wake/refresh is due and to reschedule on failure. Unit-tested on host.
#pragma once

#include <time.h>
#include <stdint.h>
#include <stdbool.h>

/* Current wall-clock epoch, or 0 if the clock has not been synced past 2020
 * (guards against reporting/acting on a bogus pre-sync time). */
time_t now_epoch(void);

/* True if the scheduled next-refresh moment has passed (or isn't scheduled).
 * See next_refresh_epoch's encoding in state.h. */
bool refresh_due(void);

/* Reschedule the next refresh N seconds from now (failure-backoff paths). Uses
 * an absolute epoch when the clock is synced, else a monotonic tick deadline.
 * Clears the pre-sleep snapshot so the next boot doesn't log a bogus error. */
void schedule_retry_in(int seconds, const char *reason);

/* Snapshot the server epoch at sleep entry so the next wake can compute
 * actual-vs-expected sleep error. server_epoch <= 0 clears the snapshot. */
void save_pre_sleep_epoch(int64_t server_epoch, int64_t local_time_at_download_us);

/* ── Schedule + drift calibration (consolidated from the board mains) ──────────
 *
 * These three carry the whole next-wake lifecycle so both boards behave
 * identically. Each cycle:
 *   1. scheduler_observe_sleep()      — early in app_main, learns oscillator drift
 *   2. scheduler_set_after_refresh()  — in perform_refresh, anchors the next slot
 *   3. scheduler_next_sleep_us()      — at the sleep site, arms the (calibrated) timer
 */

/* Anchor the next refresh to the server's absolute clock (drift-free):
 * next_refresh_epoch = server_epoch + sleep_seconds, record last_sleep_seconds,
 * and snapshot the pre-sleep epoch. Returns false (setting nothing) when
 * server_epoch <= 0 or sleep_seconds <= 0 so the caller can run its own
 * malformed-response fallback. */
bool scheduler_set_after_refresh(int64_t server_epoch, int32_t sleep_seconds,
                                 int64_t local_time_at_download_us);

/* On a timer wake, compute the actual-vs-expected sleep error (last_sleep_err_s)
 * and fold the observed oscillator ratio into the drift calibration
 * (cal_ppm/cal_samples). No-op on non-timer wakes or when the pre-sleep snapshot
 * is absent. Call once, early in app_main, before perform_refresh overwrites the
 * snapshot. */
void scheduler_observe_sleep(void);

/* Microseconds to arm for the next deep sleep, derived from next_refresh_epoch
 * (all three states) with drift calibration applied to a normal scheduled sleep.
 * Records last_armed_sleep_s so the next wake can measure drift. fallback_us is
 * the board's no-schedule default (next_refresh_epoch == 0). */
int64_t scheduler_next_sleep_us(int64_t fallback_us);

/* Adopt a server-provided calibration seed into cal_ppm when this device has no
 * calibration of its own yet and the server's mean is backed by enough samples
 * (server_n). Returns true if the seed was adopted (caller then persists it).
 * seed_ppm is clamped. Marks the device calibrated (cal_samples = 1). */
bool scheduler_adopt_cal_seed(int32_t seed_ppm, int server_n);
