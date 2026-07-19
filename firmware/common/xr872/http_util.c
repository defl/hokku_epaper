#include "http_util.h"

#include <string.h>
#include <stdlib.h>

#include "net/HTTPClient/HTTPCUsr_api.h"
#include "net/HTTPClient/API/HTTPClientCommon.h"

int read_resp_header_uint(HTTP_SESSION_HANDLE h, const char *name, uint32_t *out)
{
    char   v[48];
    UINT32 len = sizeof(v);
    int    ok = 0;

    HTTPClientFindFirstHeader(h, (char *)name, v, &len);
    len = sizeof(v);
    if (HTTPClientGetNextHeader(h, v, &len) == HTTP_CLIENT_SUCCESS) {
        char *p = strchr(v, ':');
        p = p ? p + 1 : v;
        while (*p == ' ' || *p == '\t')
            p++;
        *out = (uint32_t)strtoul(p, NULL, 10);
        ok = 1;
    }
    HTTPClientFindCloseHeader(h);
    return ok;
}

int read_resp_header_str(HTTP_SESSION_HANDLE h, const char *name, char *out, size_t outsz)
{
    char   v[64];
    UINT32 len = sizeof(v);
    int    ok = 0;

    out[0] = '\0';
    HTTPClientFindFirstHeader(h, (char *)name, v, &len);
    len = sizeof(v);
    if (HTTPClientGetNextHeader(h, v, &len) == HTTP_CLIENT_SUCCESS) {
        char *p = strchr(v, ':');
        p = p ? p + 1 : v;
        while (*p == ' ' || *p == '\t')
            p++;
        strncpy(out, p, outsz - 1);
        out[outsz - 1] = '\0';
        for (int i = (int)strlen(out) - 1; i >= 0 &&
             (out[i] == '\r' || out[i] == '\n' || out[i] == ' '); i--)
            out[i] = '\0';
        ok = (out[0] != '\0');
    }
    HTTPClientFindCloseHeader(h);
    return ok;
}
