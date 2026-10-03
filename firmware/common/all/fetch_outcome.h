// What a screen does with the server's answer to an image fetch. Shared by every
// hokku screen firmware so they all react to the same reply the same way.
//
// The board does the transport (ESP-IDF esp_http_client, XR872 HTTPClient) and
// reports what came back in a hokku_fetch_result_t; hokku_fetch_decide() turns
// that into one of three actions plus the next sleep and the new outage streak.
// The board keeps only the mechanics: how to put the image on the glass, how to
// sleep, where the streak counter lives (RTC memory on ESP32, a thread local on
// the F7).
//
// The rule: a server that answered with a valid X-Sleep-Seconds is reachable.
// Without an image that means "keep your picture, come back in this long" —
// converting (503), the screen's label filter matches nothing (404, normal
// interval), empty library (404) or an unknown model (400). The screen keeps
// what is on the glass, draws nothing, clears the outage streak and sleeps
// exactly that long. Only a transport failure, a broken 200 body, a failed OTA,
// or a non-200 with no usable X-Sleep-Seconds counts as an outage, and outages
// back off exponentially (backoff.h).
//
// Decision table (sleep = seconds until the next fetch):
//
//   response               image   X-Sleep-Seconds   action    sleep            streak
//   none (transport fail)    -          -            BACKOFF   backoff(streak)  +1
//   200                     ok        valid          DISPLAY   header           0
//   200                     ok        absent         DISPLAY   RETRY_BASE       0
//   200                     bad        any           BACKOFF   backoff(streak)  +1
//   other (404/503/400...)   -        valid          KEEP      header           0
//   other                    -        absent         BACKOFF   backoff(streak)  +1
//   OTA requested + failed   -          -            BACKOFF   backoff(streak)  +1
//
// On-glass messages: KEEP never draws. Only the start of an outage streak
// (first_failure) may draw an error, and whether it does is the board's UI
// choice. The Hokku/Huessen's old "Server not ready, retrying in N s" message
// for long 503 waits is gone: a busy server is not an error.
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* First outage retry; doubles per consecutive outage up to the cap. Also the
 * sleep after an image that came without a schedule. */
#define HOKKU_RETRY_BASE_S   60
#define HOKKU_RETRY_MAX_S    3600

/* Largest X-Sleep-Seconds a screen accepts; larger values are clamped. A month
 * is far beyond any interval the server hands out, and keeps every board's
 * arithmetic (int32 seconds, microsecond timers) well clear of overflow. */
#define HOKKU_SLEEP_MAX_S    (31 * 24 * 3600)

typedef enum {
    HOKKU_FETCH_DISPLAY,   /* show the new image, then sleep */
    HOKKU_FETCH_KEEP,      /* server answered without an image: keep the picture */
    HOKKU_FETCH_BACKOFF,   /* outage: keep the picture, retry after the backoff */
} hokku_fetch_action_t;

typedef struct {
    /* Status code of the response, or 0 when none arrived (WiFi down, DNS,
     * connect or timeout before the status line). Anything below 100 is "none". */
    int     http_status;
    /* A 200 delivered exactly the expected number of image bytes. */
    bool    image_ok;
    /* X-Sleep-Seconds as returned by hokku_sleep_seconds_parse(): 0 = absent
     * or invalid. */
    int32_t sleep_s;
    /* The server asked for an OTA (X-Firmware-Update) and it did not complete.
     * A successful OTA reboots, so it never gets here. */
    bool    ota_failed;
} hokku_fetch_result_t;

typedef struct {
    hokku_fetch_action_t action;
    int32_t      sleep_s;        /* seconds until the next fetch (always >= 1) */
    unsigned     failures;       /* the outage streak to store (saturates at 255) */
    bool         first_failure;  /* BACKOFF that starts a new streak */
    const char  *reason;         /* short log text */
} hokku_fetch_outcome_t;

/* Decide what to do with one fetch. prior_failures is the stored outage streak
 * (consecutive BACKOFFs before this fetch). Pure; never returns sleep_s < 1. */
hokku_fetch_outcome_t hokku_fetch_decide(const hokku_fetch_result_t *r,
                                         unsigned prior_failures);

/* Parse an X-Sleep-Seconds header value (NULL = header absent). Accepts optional
 * surrounding whitespace around a run of decimal digits, nothing else. Returns
 * the value clamped to HOKKU_SLEEP_MAX_S, or 0 when absent, empty, zero,
 * signed, or not a plain number. */
int32_t hokku_sleep_seconds_parse(const char *value);
