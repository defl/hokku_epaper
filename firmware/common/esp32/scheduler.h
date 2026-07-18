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
