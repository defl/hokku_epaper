#pragma once
#include <string.h>
#include "HTTPClientCommon.h"

typedef UINT32 HTTP_SESSION_HANDLE;
typedef UINT32 HTTP_AUTH_SCHEMA;

/* ── Controllable mock state for header parsing (read_resp_header_uint/str) ──
 * GetNextHeader writes _mock_http_header_value verbatim into the caller's
 * buffer (as the real SDK does: the "Name: value" line, not just the value)
 * and reports success iff _mock_http_header_present. */
static const char *_mock_http_header_value;
static int          _mock_http_header_present;
static const char *_mock_http_header_clue;   /* name asked for by FindFirstHeader */

static inline UINT32 HTTPClientFindFirstHeader(HTTP_SESSION_HANDLE s, CHAR *clue,
                                                CHAR *buf, UINT32 *len)
{
    (void)s; (void)buf; (void)len;
    _mock_http_header_clue = clue;
    return HTTP_CLIENT_SUCCESS;
}
/* The one mocked header only answers to its own name, so a test can set
 * "X-Sleep-Seconds: 60" without it also reading as X-Firmware-Update. */
static inline int _mock_http_header_matches(void)
{
    const char *v = _mock_http_header_value, *c = _mock_http_header_clue;
    if (!v || !c) return 1;
    for (; *c; c++, v++) {
        char a = *c, b = *v;
        if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
        if (b >= 'A' && b <= 'Z') b = (char)(b - 'A' + 'a');
        if (a != b) return 0;
    }
    return *v == ':';
}
static inline UINT32 HTTPClientGetNextHeader(HTTP_SESSION_HANDLE s, CHAR *buf, UINT32 *len)
{
    (void)s;
    if (!_mock_http_header_present || !_mock_http_header_matches())
        return 1; /* any non-HTTP_CLIENT_SUCCESS value */
    strncpy(buf, _mock_http_header_value, *len - 1);
    buf[*len - 1] = '\0';
    return HTTP_CLIENT_SUCCESS;
}
static inline UINT32 HTTPClientFindCloseHeader(HTTP_SESSION_HANDLE s) { (void)s; return HTTP_CLIENT_SUCCESS; }
static inline UINT32 HTTPClientAddRequestHeaders(HTTP_SESSION_HANDLE s, CHAR *name,
                                                  CHAR *data, BOOL insert)
{
    (void)s; (void)name; (void)data; (void)insert;
    return HTTP_CLIENT_SUCCESS;
}
