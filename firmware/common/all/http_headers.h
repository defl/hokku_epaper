// The HTTP header names of the screen <-> server protocol, in one place for
// every hokku firmware. The server side lives in python/hokku/webserver
// (screen_headers.py, flask_app.py). Pure C (see README.md).
#pragma once

/* ── Request (screen -> server) ─────────────────────────────────────────── */
#define HOKKU_HDR_SCREEN_MODEL    "X-Screen-Model"      /* model id, e.g. "bigme_f7" */
#define HOKKU_HDR_SCREEN_MAC      "X-Screen-Mac"        /* durable per-device key */
#define HOKKU_HDR_FRAME_STATE     "X-Frame-State"       /* telemetry JSON (frame_state.h) */
#define HOKKU_HDR_FW_VERSION      "X-Firmware-Version"
#define HOKKU_HDR_FW_BUILD        "X-Firmware-Build"
#define HOKKU_HDR_CONFIG_STATE    "X-Config-State"      /* ESP32 OTA config migration */

/* ── Both directions ────────────────────────────────────────────────────── */
/* The screen reports its name; the server answers with its own name for the
 * screen, which the screen adopts (screen_ident.h). */
#define HOKKU_HDR_SCREEN_NAME     "X-Screen-Name"

/* ── Response (server -> screen) ────────────────────────────────────────── */
#define HOKKU_HDR_SLEEP_SECONDS   "X-Sleep-Seconds"     /* hokku_sleep_seconds_parse */
#define HOKKU_HDR_SERVER_TIME     "X-Server-Time-Epoch" /* hokku_server_epoch_parse */
#define HOKKU_HDR_FW_UPDATE       "X-Firmware-Update"   /* version to OTA to */
#define HOKKU_HDR_SLEEP_CAL_PPM   "X-Sleep-Cal-PPM"     /* hokku_cal_seed_parse */
#define HOKKU_HDR_SLEEP_CAL_N     "X-Sleep-Cal-N"
