// Shared HTTP image fetch for the ESP32 hokku firmwares.
//
// POSTs the diagnostic log ring as the request body, sends the standard hokku
// headers (common/all/http_headers.h), downloads the image into a
// caller-provided buffer, and reports everything the reply carried in the shared
// hokku_fetch_result_t (common/all/fetch_outcome.h). A reply carrying the server
// time also sets the system clock. Board-independent: the image size, screen
// model, and the pre-built X-Frame-State string are passed in by the caller.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#include "fetch_outcome.h"

#define HTTP_TIMEOUT_MS  30000

/* Format this device's WiFi STA MAC as lowercase "aa:bb:cc:dd:ee:ff" into out
 * (needs >= 18 bytes). Empty string on failure. This is the server's durable
 * per-device key; the screen name is only a mutable label. */
void hokku_screen_mac_str(char *out, size_t len);

/* Fetch the screen image into buf (capacity = expect_bytes; caller owns it).
 * frame_state is the caller-built X-Frame-State JSON. fw_build is the compile
 * stamp (X-Firmware-Build); the version comes from the app descriptor.
 *
 * Fills res (status, image_ok, sleep_s, server_epoch, calibration seed; the
 * caller's other fields are left alone) and fw_update (X-Firmware-Update, ""
 * when absent). image_ok means a 200 whose body was exactly expect_bytes
 * (hokku_image_size_ok); on a 200 the log ring is reset and the server's name
 * for the screen adopted. Returns res->image_ok. */
bool hokku_http_fetch_image(uint8_t *buf, size_t expect_bytes,
                            const char *url, const char *screen_name,
                            const char *screen_model, const char *frame_state,
                            const char *fw_build, hokku_fetch_result_t *res,
                            char *fw_update, size_t fw_update_len);
