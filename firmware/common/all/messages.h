// What a screen writes on its glass, and when. Every hokku screen draws the
// same words at the same moments; a board only renders them (text_render.h) at
// HOKKU_MSG_X/Y/SCALE in black on white. Pure C (see README.md).
//
// The moments:
//   - start of an outage streak (fetch_outcome first_failure): WiFi or
//     download failure, hokku_msg_fetch_error(). Later backed-off retries stay
//     silent to spare battery and e-paper; KEEP never draws.
//   - config missing / config schema mismatch: at boot, instead of fetching.
//   - OTA: before the download, on failure, and before the reboot.
//
// The one per-board part is how a person asks for a retry right now
// (retry_hint): a button on the ESP32 boards, a power cycle on the Bigme F7,
// whose button is a hardware power latch the firmware cannot see.
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "fetch_outcome.h"

/* Layout, in panel pixels. */
#define HOKKU_MSG_X       20
#define HOKKU_MSG_Y       40
#define HOKKU_MSG_SCALE   3
#define HOKKU_MSG_COLOR   0x0   /* black */
#define HOKKU_MSG_BG      0x1   /* white */

/* Longest message the builders produce, including a 256-byte server URL. */
#define HOKKU_MSG_MAX     448

#define HOKKU_MSG_CONFIG_MISSING \
    "Hokku installed but\ncannot read config.\n\n" \
    "Connect USB and run\nhokku-setup to\nconfigure."

#define HOKKU_MSG_OTA_START \
    "Updating firmware...\n\nDo not unplug.\nThe screen will\nrestart itself."

#define HOKKU_MSG_OTA_DONE  "Update complete.\n\nRestarting..."

/* The error to draw after a fetch, if any. Returns true and fills buf when the
 * outcome starts an outage streak, unless that outage is a failed OTA (which
 * drew its own message). url is the server address the screen tried. */
bool hokku_msg_fetch_error(char *buf, size_t len, const hokku_fetch_result_t *r,
                           const hokku_fetch_outcome_t *o, const char *url,
                           const char *retry_hint);

/* Config written for another schema version (the firmware cannot read it). */
void hokku_msg_config_version(char *buf, size_t len, unsigned expected, unsigned found);

/* A firmware update that failed at `stage` ("download", "commit", ...). */
void hokku_msg_ota_failed(char *buf, size_t len, const char *stage);
