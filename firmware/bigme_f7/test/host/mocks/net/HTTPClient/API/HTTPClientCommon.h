#pragma once
#include <stdint.h>

typedef char           CHAR;
typedef uint32_t       UINT32;
typedef uint16_t       UINT16;
typedef int32_t        INT32;
typedef int             BOOL;
typedef void            VOID;

#define HTTP_CLIENT_SUCCESS 0

typedef enum { VerbGet = 0, VerbHead, VerbPost, VerbNotSupported } HTTP_VERB;

typedef struct _HTTP_CLIENT {
    UINT32 HTTPStatusCode;
    UINT32 RequestBodyLengthSent;
    UINT32 ResponseBodyLengthReceived;
    UINT32 TotalResponseBodyLength;
    UINT32 HttpState;
} HTTP_CLIENT;
