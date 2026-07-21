// Shared HTTP image fetch for the ESP32 hokku firmwares.
//
// POSTs the diagnostic log ring as the request body, sends the standard hokku
// headers (X-Screen-Name/Model, X-Frame-State, X-Firmware-Version/Build),
// downloads exactly the expected image size into a caller-provided buffer, and
// captures the response headers (X-Sleep-Seconds, X-Server-Time-Epoch — which
// also sets the system clock — and X-Firmware-Update). Board-independent: the
// image size, screen model, and the pre-built X-Frame-State string are passed
// in by the caller (which gathers its own board state).
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#define HTTP_TIMEOUT_MS  30000

/* Outputs captured from the response. Any pointer may be NULL. */
typedef struct {
    int32_t *out_sleep_seconds;   /* X-Sleep-Seconds (if > 0) */
    int64_t *out_server_epoch;    /* X-Server-Time-Epoch (if > 0); also sets the clock */
    int     *out_http_status;     /* HTTP status code */
    char    *out_fw_update;       /* X-Firmware-Update version string (empty if none) */
    size_t   fw_update_buflen;    /* size of out_fw_update */
} hokku_fetch_out_t;

/* Fetch the screen image into buf (capacity = expect_bytes; caller owns it).
 * frame_state is the caller-built X-Frame-State JSON. fw_build is the compile
 * stamp (X-Firmware-Build); the version comes from the app descriptor. On a 200
 * with exactly expect_bytes received, resets the log ring and returns true;
 * otherwise returns false (buf left untouched for the caller to free). */
bool hokku_http_fetch_image(uint8_t *buf, size_t expect_bytes,
                            const char *url, const char *screen_name,
                            const char *screen_model, const char *frame_state,
                            const char *fw_build, hokku_fetch_out_t *out);
