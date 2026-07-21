// SoC-agnostic firmware-URL derivation shared by ALL hokku firmwares.
// Pure C — no ESP-IDF or XR872 SDK headers.
#pragma once

#include <stddef.h>

/* Derive a sibling firmware endpoint from the server base URL by replacing its
 * last path segment with `leaf`:
 *   base "http://host/hokku/screen/", leaf "firmware.bin"
 *     -> "http://host/hokku/firmware.bin"
 * Strips any trailing '/' and the final path segment of base, then appends
 * leaf. Writes a NUL-terminated result into out (truncated to outsz).
 *
 * `leaf` carries any query string the caller wants, e.g.
 * "firmware.bin?model=seeedstudio_e1004" or "firmware-config". */
void firmware_url_build(char *out, size_t outsz, const char *base_url, const char *leaf);
