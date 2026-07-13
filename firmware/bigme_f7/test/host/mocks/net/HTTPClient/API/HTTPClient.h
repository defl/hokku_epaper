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

static inline UINT32 HTTPClientFindFirstHeader(HTTP_SESSION_HANDLE s, CHAR *clue,
                                                CHAR *buf, UINT32 *len)
{
    (void)s; (void)clue; (void)buf; (void)len;
    return HTTP_CLIENT_SUCCESS;
}
static inline UINT32 HTTPClientGetNextHeader(HTTP_SESSION_HANDLE s, CHAR *buf, UINT32 *len)
{
    (void)s;
    if (!_mock_http_header_present)
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
