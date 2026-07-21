#include "log.h"

#include <stdio.h>
#include <stdarg.h>

#include "logbuf.h"   /* SoC-agnostic ring primitive (common/all) */

static char     g_log_storage[HOKKU_XR872_LOG_RING_SZ];
static logbuf_t g_log;
static char     g_log_body[HOKKU_XR872_LOG_RING_SZ];   /* contiguous POST scratch */

/* Lazy one-time attach so hlog() works whether called from the firmware (after
 * boot) or a host test (which calls hlog directly). */
static void hlog_ensure(void)
{
    if (g_log.cap == 0)
        logbuf_init(&g_log, g_log_storage, sizeof(g_log_storage));
}

void hlog(const char *fmt, ...)
{
    char    line[160];
    va_list ap;
    int     n;

    hlog_ensure();
    va_start(ap, fmt);
    n = vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    if (n < 0)
        return;
    if (n > (int)sizeof(line) - 1)
        n = (int)sizeof(line) - 1;

    printf("%s", line);                       /* still to serial console */
    logbuf_append(&g_log, line, (uint32_t)n);
}

void hlog_reset(void) { hlog_ensure(); logbuf_reset(&g_log); }

uint32_t hlog_len(void) { hlog_ensure(); return logbuf_len(&g_log); }

const char *hlog_snapshot(uint32_t *len_out)
{
    hlog_ensure();
    uint32_t n = logbuf_snapshot(&g_log, g_log_body, sizeof(g_log_body));
    if (len_out)
        *len_out = n;
    return g_log_body;
}
