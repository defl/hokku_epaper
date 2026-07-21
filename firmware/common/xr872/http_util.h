// HTTP response-header parsing helpers for XR872 hokku screens (XR872 SDK
// HTTPClient). FindFirstHeader alone never yields the value — you must chase it
// with GetNextHeader and strip the "Name:" prefix — so these wrap the two-step
// dance once, correctly. Shared by all XR872/XR872AT screens.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "net/HTTPClient/API/HTTPClient.h"   /* HTTP_SESSION_HANDLE */

/* Read a numeric response header into *out. Returns 1 if a value was found. */
int read_resp_header_uint(HTTP_SESSION_HANDLE h, const char *name, uint32_t *out);

/* Read a string response header into out[] (whitespace/CRLF-trimmed).
 * Returns 1 if a non-empty value was found. */
int read_resp_header_str(HTTP_SESSION_HANDLE h, const char *name, char *out, size_t outsz);
