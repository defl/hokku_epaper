// Shared RTC-persistent state for hokku ESP32 firmwares.
//
// All state that must survive deep sleep AND esp_restart() lives here as
// RTC_NOINIT_ATTR globals, validated together exactly once per boot chain by
// hokku_state_validate(). Both huessen_epf1301 and seeedstudio_e1004 link this;
// their board-specific app_main / regime code reads and writes these globals.
//
// RTC_NOINIT_ATTR (not RTC_DATA_ATTR): initialisers are NOT respected and the
// section is not reloaded on any reset. On a true power-on the values are
// garbage — hokku_state_validate() catches that via the magic sentinel and
// zero-initialises everything exactly once. RTC_DATA_ATTR would be wrong: it is
// re-initialised on every esp_restart(), the root cause of the historical
// "boot_count always 1 / clk_now always 0" bug.
#pragma once

#include <stdint.h>
#include <stdbool.h>

#include "schedule.h"   /* common/all: hokku_sched_t */

/* Validates RTC memory after POR / flash. Bumped whenever the RTC layout changes
 * (last: hokku_sched_t replaced the separate schedule globals), so an OTA into a
 * new layout starts from clean state instead of misreading the old bytes. */
#define RTC_MAGIC            0x484F4B56  /* "HOKV" */
#define MAX_SPURIOUS_RESETS  3           /* cap on repeated spurious deep-sleep wakes */
#define LOG_RING_SIZE        6144        /* log ring buffer size (RTC slow memory) */

/* How the previous boot ended — set before every esp_restart() / deep-sleep
 * entry, read by the next boot's X-Frame-State. */
#define LAST_SLEEP_MODE_NONE          0  /* POR / fresh flash */
#define LAST_SLEEP_MODE_TIMER_WAKE    1  /* timer-triggered restart from deep sleep */
#define LAST_SLEEP_MODE_BUTTON_WAKE   2  /* button-triggered restart from deep sleep */
#define LAST_SLEEP_MODE_USB_PLUG      3  /* USB plug wake from deep sleep (no restart) */
#define LAST_SLEEP_MODE_BUTTON_USB    4  /* button pressed in USB_AWAKE */
#define LAST_SLEEP_MODE_BUTTON_BATT   5  /* button pressed in BATTERY_IDLE */
#define LAST_SLEEP_MODE_USB_SCHED     6  /* scheduled refresh restart from USB_AWAKE */
#define LAST_SLEEP_MODE_SPURIOUS      7  /* spurious deep-sleep wake restart */
#define LAST_SLEEP_MODE_POST_REFRESH  8  /* restart after perform_refresh completed */

/* What this boot must do. Set before every esp_restart(); the board's app_main
 * captures and clears it early so a crashing boot doesn't loop on the action. */
#define ACTION_NONE          0  /* POR / no instruction — treat as first boot */
#define ACTION_REFRESH       1  /* call perform_refresh() then restart */
#define ACTION_ENTER_REGIME  2  /* skip refresh, enter regime based on power state */

/* ── RTC-persistent globals (defined in state.c) ─────────────── */
extern uint32_t rtc_magic;
extern uint32_t boot_count;

/* Cached WiFi state for fast reconnect (owned by hokku_wifi logic). */
extern uint8_t  wifi_channel;
extern uint8_t  wifi_bssid[6];
extern bool     has_wifi_cache;
extern uint8_t  last_wifi_index;   /* 0 or 1 — which network last succeeded */

/* Most-recent battery reading (mV), produced by the board and reported in
 * X-Frame-State. */
extern uint16_t last_battery_mv;

/* Next-fetch schedule, outage streak and oscillator-drift calibration — the
 * shared common/all/schedule.h state, kept in RTC memory across deep sleep and
 * esp_restart(). Its cal_ppm/cal_samples are mirrored to NVS so they survive
 * power loss (nvs_cal.h); the RTC copy is authoritative across deep sleep /
 * restart and is only re-hydrated from NVS on a cold POR (see
 * hokku_state_validate). */
extern hokku_sched_t hokku_sched;

extern uint8_t  consecutive_spurious_resets;

extern uint8_t  last_sleep_mode;    /* LAST_SLEEP_MODE_* */
extern uint8_t  pending_action;     /* ACTION_* */

/* Log ring buffer contents (drained by hokku_log / uploaded by hokku_net). */
extern char     s_log_ring[LOG_RING_SIZE];
extern uint16_t s_log_ring_head;    /* next write position */
extern uint16_t s_log_ring_used;    /* bytes currently held */

/* Runtime regime string for X-Frame-State ("boot" until a regime is entered).
 * NOT RTC-persistent — set by the board's regime code each boot. */
extern const char *current_regime;

/* Validate RTC memory for this boot chain. On POR (magic mismatch) zero every
 * global above and reset the wall clock to 0; then stamp the magic valid.
 * Idempotent across deep-sleep / esp_restart (magic already valid → no zeroing).
 * Call once at the very top of app_main, before any state is read.
 *
 * Returns true on a cold POR (state was just zeroed). The caller uses this to
 * decide whether to re-hydrate the calibration (cal_ppm/cal_samples) from NVS:
 * on POR the RTC copy is garbage-then-zeroed, so NVS is authoritative; on a
 * deep-sleep/restart wake the RTC copy is newer and must be kept. */
bool hokku_state_validate(void);
