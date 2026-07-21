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

/* do_refresh()'s HTTP orchestration (streaming download/upload) isn't unit
 * tested (integration-level, needs a real socket) — these just need to link. */
static inline int HTTPC_open(HTTPParameters *p) { (void)p; return 1; }
static inline int HTTPC_request(HTTPParameters *p, HTTP_CLIENT_GET_HEADER cb) { (void)p; (void)cb; return 1; }
static inline int HTTPC_get_request_info(HTTPParameters *p, void *hc) { (void)p; (void)hc; return 1; }
static inline int HTTPC_write(HTTPParameters *p, VOID *buf, UINT32 n) { (void)p; (void)buf; (void)n; return 1; }
static inline int HTTPC_read(HTTPParameters *p, VOID *buf, UINT32 n, UINT32 *got) { (void)p; (void)buf; (void)n; *got = 0; return 1; }
static inline int HTTPC_close(HTTPParameters *p) { (void)p; return 0; }
