// SoC-agnostic JSON helpers shared by ALL hokku firmwares (both ESP32 boards
// and the XR872 F7). Pure C — no ESP-IDF or XR872 SDK headers. See
// firmware/common/all/README.md for the common/all contract.
#pragma once

#include <stddef.h>

/* Minimal JSON string-escaper: escapes '"' and '\\', drops control chars
 * (< 0x20). Writes a NUL-terminated result into dst (truncated to dstlen). */
void json_escape(char *dst, size_t dstlen, const char *src);
