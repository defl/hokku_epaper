// XR872 activity-log ring — shared by all XR872/XR872AT hokku screens.
//
// hlog() echoes each line to the UART console AND appends it to a circular
// buffer built on the SoC-agnostic common/all/logbuf primitive. A screen POSTs
// the accumulated buffer to the server as the request body on each fetch, then
// calls hlog_reset() after a 200. XR872 screens reboot on every hibernation
// wake, so a single in-RAM buffer suffices (no cross-sleep carry — that's the
// ESP32's two-tier concern). Circular: once full it evicts the oldest bytes.
#pragma once

#include <stdint.h>

#define HOKKU_XR872_LOG_RING_SZ 2048U

void        hlog(const char *fmt, ...);   // printf-style -> console + ring
void        hlog_reset(void);             // clear the ring (after a successful POST)
uint32_t    hlog_len(void);               // bytes currently held

// Snapshot the (possibly wrapped) ring into an internal contiguous buffer for
// POSTing; returns the buffer and writes its length. Valid until the next call.
const char *hlog_snapshot(uint32_t *len_out);
