// SoC-agnostic X-Frame-State telemetry JSON builder, shared by ALL hokku
// firmwares. Pure C — no ESP-IDF or XR872 SDK headers. Each firmware gathers
// the values from its own SDK (WiFi RSSI, heap, clock, battery, ...) into a
// frame_state_t and calls frame_state_build(); this guarantees every board
// reports the same schema to the server. The server parses the result as an
// opaque dict for the dashboard "Details" view.
#pragma once

#include <stddef.h>
#include <stdbool.h>

typedef struct {
    const char *fw;           /* firmware version string */
    unsigned    boot;         /* boot counter (0 if the platform doesn't track it) */
    const char *wake;         /* how this boot was entered (wake label) */
    const char *regime;       /* what the firmware is doing right now */
    long long   uptime_s;     /* seconds since boot */
    int         bat_mv;       /* battery mV; < 0 OMITS the field (unknown/no sense) */
    const char *usb;          /* "host" / "none" (external-power/USB state) */
    const char *last_sleep;   /* how the previous boot ended */
    int         rssi;         /* WiFi RSSI, dBm */
    unsigned    heap_kb;      /* free heap, KiB */
    unsigned    spurious;     /* consecutive spurious wakes (0 if N/A) */
    unsigned    cfg_ver;      /* config schema version */
    long long   clk_now;      /* wall-clock epoch seconds, 0 if unsynced */
    long long   next_ep;      /* scheduled next-refresh epoch, 0 if unscheduled */
    bool        sleep_err_known;  /* whether sleep_err_s carries a value */
    int         sleep_err_s;      /* actual-vs-expected sleep error, seconds */
    bool        wifi_cached;      /* last connect used the fast-reconnect cache */
} frame_state_t;

/* Serialise fs into buf (truncated to buflen) as the X-Frame-State JSON object.
 * Always emits "ota":1 (the firmwares are OTA-capable). bat_mv < 0 omits the
 * bat_mv field. */
void frame_state_build(char *buf, size_t buflen, const frame_state_t *fs);
