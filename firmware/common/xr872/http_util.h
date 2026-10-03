// HTTP response-header parsing helper for XR872 hokku screens (XR872 SDK
// HTTPClient). FindFirstHeader alone never yields the value — you must chase it
// with GetNextHeader and strip the "Name:" prefix — so this wraps the two-step
// dance once, correctly. Values are then parsed by the shared parsers in
// common/all/fetch_outcome.h. Shared by all XR872/XR872AT screens.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "net/HTTPClient/API/HTTPClient.h"   /* HTTP_SESSION_HANDLE */

/* Read a string response header into out[] (whitespace/CRLF-trimmed).
 * Returns 1 if a non-empty value was found; out[] is "" otherwise. */
int read_resp_header_str(HTTP_SESSION_HANDLE h, const char *name, char *out, size_t outsz);
