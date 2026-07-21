// Diagnostic logger for the ESP32 hokku firmwares.
//
// A single circular log ring in RTC slow memory (hokku_state's s_log_ring[]),
// written directly on every log line so it survives BOTH deep sleep AND an
// unclean reset (panic / watchdog / brownout) — the crashing cycle's tail is
// recovered and uploaded on the next boot. The ring is built on the shared
// SoC-agnostic logbuf primitive (common/all/logbuf); the F7 firmware builds its
// own single-buffer logger on the same primitive.
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "state.h"   /* LOG_RING_SIZE — the RTC ring capacity */

/* Upper bound on a single upload body (the whole ring). Callers malloc this to
 * receive a full snapshot. */
#define HOKKU_LOG_MAX_UPLOAD  LOG_RING_SIZE

/* Allocate the PSRAM format scratch, reconstruct the RTC ring from its persisted
 * state, and install the dual-output (serial + RTC ring) vprintf hook. Call once
 * early in app_main, after hokku_state_validate() and before any log output you
 * want captured. Returns false if the PSRAM scratch could not be allocated
 * (logging then stays serial-only). */
bool hokku_log_init(void);

/* Runtime log gating: INFO when on external power / USB, NONE on battery. */
void log_level_apply(bool verbose);

/* Copy the ring contents (oldest→newest) into out (up to cap). Returns bytes
 * written. Latches the ring metadata under a brief lock, then copies outside it. */
size_t hokku_log_snapshot(char *out, size_t cap);

/* Clear the ring — call after the snapshot has been successfully uploaded so the
 * next cycle starts fresh. */
void hokku_log_reset(void);
