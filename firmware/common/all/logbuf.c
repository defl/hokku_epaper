#include "logbuf.h"

#include <string.h>

void logbuf_attach(logbuf_t *lb, char *storage, uint32_t cap, uint32_t head, uint32_t used)
{
    lb->buf = storage;
    lb->cap = cap;
    if (cap == 0 || used > cap || head >= cap) {
        lb->head = 0;
        lb->used = 0;
    } else {
        lb->head = head;
        lb->used = used;
    }
}

void logbuf_init(logbuf_t *lb, char *storage, uint32_t cap)
{
    lb->buf  = storage;
    lb->cap  = cap;
    lb->head = 0;
    lb->used = 0;
}

void logbuf_append(logbuf_t *lb, const char *data, uint32_t len)
{
    if (lb->cap == 0 || len == 0) return;

    /* If the incoming data alone exceeds capacity, only its last cap bytes can
     * survive — skip straight to them and reset the buffer to a clean wrap. */
    if (len >= lb->cap) {
        data += (len - lb->cap);
        len   = lb->cap;
        lb->head = 0;
        lb->used = 0;
    }

    for (uint32_t i = 0; i < len; i++) {
        lb->buf[lb->head] = data[i];
        lb->head = (lb->head + 1) % lb->cap;
        if (lb->used < lb->cap) lb->used++;
        /* when full, head has advanced over the oldest byte (evicted) */
    }
}

void logbuf_append_buf(logbuf_t *dst, const logbuf_t *src)
{
    /* Walk src oldest->newest and append. src may itself be wrapped. Snapshot
     * the bound and start into locals up front so a dst==src self-append (or
     * dst growing to evict into src's range) can't chase a moving src->used. */
    uint32_t n = src->used;
    if (n == 0) return;
    uint32_t cap = src->cap;
    uint32_t start = (n < cap) ? 0 : (src->head % cap);
    for (uint32_t i = 0; i < n; i++) {
        char c = src->buf[(start + i) % cap];
        logbuf_append(dst, &c, 1);
    }
}

uint32_t logbuf_snapshot(const logbuf_t *lb, char *out, uint32_t out_cap)
{
    if (out_cap == 0 || lb->used == 0) return 0;
    uint32_t start = (lb->used < lb->cap) ? 0 : (lb->head % lb->cap);
    uint32_t n = (lb->used < out_cap) ? lb->used : out_cap;
    for (uint32_t i = 0; i < n; i++) {
        out[i] = lb->buf[(start + i) % lb->cap];
    }
    return n;
}

uint32_t logbuf_snapshot_tail(const logbuf_t *lb, char *out, uint32_t out_cap)
{
    if (out_cap == 0 || lb->used == 0) return 0;
    uint32_t n    = (lb->used < out_cap) ? lb->used : out_cap;
    uint32_t skip = lb->used - n;                      /* drop this many oldest */
    uint32_t start = (lb->used < lb->cap) ? 0 : (lb->head % lb->cap);
    start = (start + skip) % lb->cap;
    for (uint32_t i = 0; i < n; i++) {
        out[i] = lb->buf[(start + i) % lb->cap];
    }
    return n;
}

void logbuf_reset(logbuf_t *lb)
{
    lb->head = 0;
    lb->used = 0;
}
