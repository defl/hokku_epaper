// Shared A/B OTA for the ESP32 hokku firmwares.
//
// Full over-the-air update: migrate the NVS config forward (server round-trip),
// stream the app image into the inactive OTA slot, rewrite NVS, flip the boot
// partition, and reboot. Any failure aborts safely with the running slot + NVS
// intact. The bootloader's rollback (CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE) is
// the safety net: a bad new image that can't reach the server + display is
// reverted on the next boot, and ota_mark_valid_if_pending() confirms a good one.
//
// Board-independent: URLs are derived from base_url, identity from
// screen_name/model, and user-facing progress is delegated to a callback.
#pragma once

#include <stdbool.h>

/* Called with user-facing status strings during an OTA (e.g. to draw a progress
 * screen). May be NULL. */
typedef void (*ota_progress_fn)(const char *msg);

/* Perform the full OTA. Assumes WiFi is up. base_url is the screen endpoint
 * (config.image_url); the firmware.bin / firmware-config siblings are derived
 * from it. screen_name/model identify the device to the server. On success this
 * reboots and does not return; on failure it returns false and the caller
 * schedules a retry. target_version is informational. */
bool perform_ota(const char *target_version, const char *base_url,
                 const char *screen_name, const char *screen_model,
                 ota_progress_fn progress);

/* If this boot is a freshly-OTA'd app awaiting verification, confirm it (cancels
 * the bootloader's pending rollback). Call only after a successful refresh —
 * i.e. once the new firmware has proven it can reach the server and display. */
void ota_mark_valid_if_pending(void);
