#pragma once
#include <string.h>
#include "HTTPClientCommon.h"

typedef UINT32 HTTP_SESSION_HANDLE;
typedef UINT32 HTTP_AUTH_SCHEMA;

/* ── Controllable mock state for response-header parsing (read_resp_header_str) ──
 * GetNextHeader writes a "Name: value" line verbatim into the caller's buffer
 * (as the real SDK does, not just the value). Headers come from the single
 * _mock_http_header_value (when _mock_http_header_present) and from the
 * _mock_httpc_headers[] list; each answers only to its own name. */
static const char *_mock_http_header_value;
static int          _mock_http_header_present;
static const char *_mock_http_header_clue;   /* name asked for by FindFirstHeader */
static const char *_mock_httpc_headers[8];
static int          _mock_httpc_header_count;

static inline UINT32 HTTPClientFindFirstHeader(HTTP_SESSION_HANDLE s, CHAR *clue,
                                                CHAR *buf, UINT32 *len)
{
    (void)s; (void)buf; (void)len;
    _mock_http_header_clue = clue;
    return HTTP_CLIENT_SUCCESS;
}
/* Whether header line v ("Name: value") is the one FindFirstHeader asked for. */
static inline int _mock_http_header_matches(const char *v)
{
    const char *c = _mock_http_header_clue;
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
    const char *hit = NULL;
    (void)s;
    if (_mock_http_header_present && _mock_http_header_matches(_mock_http_header_value))
        hit = _mock_http_header_value;
    for (int i = 0; !hit && i < _mock_httpc_header_count; i++)
        if (_mock_http_header_matches(_mock_httpc_headers[i]))
            hit = _mock_httpc_headers[i];
    if (!hit)
        return 1; /* any non-HTTP_CLIENT_SUCCESS value */
    strncpy(buf, hit, *len - 1);
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
