#pragma once
#include "API/HTTPClient.h"
#include "API/HTTPClientCommon.h"

#define HTTP_CLIENT_MAX_URL_LENGTH      256
#define HTTP_CLIENT_MAX_USERNAME_LENGTH 32
#define HTTP_CLIENT_MAX_PASSWORD_LENGTH 32

typedef void *(*HTTP_CLIENT_GET_HEADER)(void);

typedef struct _HTTPParameters {
    CHAR                 Uri[HTTP_CLIENT_MAX_URL_LENGTH];
    HTTP_VERB             HttpVerb;
    UINT32                Verbose;
    CHAR                  UserName[HTTP_CLIENT_MAX_USERNAME_LENGTH];
    CHAR                  Password[HTTP_CLIENT_MAX_PASSWORD_LENGTH];
    HTTP_AUTH_SCHEMA       AuthType;
    BOOL                  isTransfer;
    HTTP_SESSION_HANDLE    pHTTP;
    UINT32                Flags;
    VOID                  *pData;
    UINT32                pLength;
    UINT32                nTimeout;
} HTTPParameters;

/* Controllable transport for do_refresh(): each step's result (0 = success),
 * the response status, and how many body bytes HTTPC_read hands out before it
 * reports the end of the stream. */
static int    _mock_httpc_open_result = 1;
static int    _mock_httpc_request_result;
static int    _mock_httpc_info_result;
static UINT32 _mock_httpc_status;
static UINT32 _mock_httpc_body_len;
static UINT32 _mock_httpc_body_read;

static inline int HTTPC_open(HTTPParameters *p) { (void)p; return _mock_httpc_open_result; }
static inline int HTTPC_request(HTTPParameters *p, HTTP_CLIENT_GET_HEADER cb) { (void)p; (void)cb; return _mock_httpc_request_result; }
static inline int HTTPC_get_request_info(HTTPParameters *p, void *hc)
{
    (void)p;
    ((HTTP_CLIENT *)hc)->HTTPStatusCode = _mock_httpc_status;
    return _mock_httpc_info_result;
}
static inline int HTTPC_write(HTTPParameters *p, VOID *buf, UINT32 n) { (void)p; (void)buf; (void)n; return 1; }
static inline int HTTPC_read(HTTPParameters *p, VOID *buf, UINT32 n, UINT32 *got)
{
    (void)p; (void)buf;
    UINT32 left = _mock_httpc_body_len - _mock_httpc_body_read;
    *got = n < left ? n : left;
    _mock_httpc_body_read += *got;
    return (_mock_httpc_body_read < _mock_httpc_body_len) ? HTTP_CLIENT_SUCCESS : 1;
}
static inline int HTTPC_close(HTTPParameters *p) { (void)p; return 0; }