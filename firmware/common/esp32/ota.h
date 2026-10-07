// Shared A/B OTA for the ESP32 hokku firmwares.
//
// Full over-the-air update: migrate the NVS config forward (server round-trip),
// stream the app image into the inactive OTA slot, rewrite NVS, flip the boot
// partition, and reboot. Any failure aborts safely with the running slot + NVS
// intact. The bootloader's rollback (CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE) is
// the safety net: the new image boots PENDING_VERIFY and confirms itself under
// the shared policy (common/all/ota_confirm.h) via ota_first_fetch().
//
// Board-independent: URLs are derived from base_url, identity from
// screen_name/model, and user-facing progress is delegated to a callback.
#pragma once

#include <stdbool.h>

#include "fetch_outcome.h"

/* Called with the on-glass status texts during an OTA (common/all/messages.h).
 * May be NULL. */
typedef void (*ota_progress_fn)(const char *msg);

/* Perform the full OTA. Assumes WiFi is up. base_url is the screen endpoint
 * (config.image_url); the firmware.bin / firmware-config siblings are derived
 * from it. screen_name/model identify the device to the server. On success this
 * reboots and does not return; on failure it returns false and the caller
 * schedules a retry. Refused while this image is itself unconfirmed: the other
 * slot is its rollback. target_version is informational. */
bool perform_ota(const char *target_version, const char *base_url,
                 const char *screen_name, const char *screen_model,
                 ota_progress_fn progress);

/* True if this boot is a freshly-OTA'd app awaiting confirmation (any reset
 * before it confirms rolls it back). Always false when rollback is disabled. */
bool ota_is_pending_verify(void);

/* A boot's first fetch under the shared confirm policy: fetch(ctx) does one
 * complete fetch and acts on it. An unconfirmed image is confirmed by the first
 * fetch that reaches the server, retried otherwise, and rolled back when the
 * attempts run out (that path reboots). Returns the last fetch's action. */
hokku_fetch_action_t ota_first_fetch(hokku_fetch_action_t (*fetch)(void *ctx), void *ctx);
