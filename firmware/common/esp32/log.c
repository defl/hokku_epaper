#include "log.h"
#include "state.h"      /* s_log_ring[] + s_log_ring_head/used (the RTC ring) */
#include "logbuf.h"     /* common/all: SoC-agnostic buffer primitive */

#include <stdio.h>
#include <stdarg.h>

#include "freertos/FreeRTOS.h"
#include "esp_log.h"
#include "esp_heap_caps.h"

/* Format scratch buffers live in PSRAM. A pool of LOG_BUF_COUNT slots lets
 * concurrent callers — typically the main task and the WiFi/lwIP system-event
 * task — each format without contention. */
#define LOG_BUF_COUNT 2
#define LOG_BUF_SIZE  512

static portMUX_TYPE  s_log_mux = portMUX_INITIALIZER_UNLOCKED;
static char         *s_fmt_bufs[LOG_BUF_COUNT];
static volatile bool s_fmt_used[LOG_BUF_COUNT];
static bool          s_fmt_ready;

/* The log ring: a single logbuf over the RTC-persistent s_log_ring[] in
 * hokku_state, so it survives deep sleep and unclean resets. Reconstructed each
 * boot from the persisted head/used; its metadata is written back to the RTC
 * vars on every mutation so a crash mid-cycle is recovered on the next boot. */
static logbuf_t s_ring;

/* Write the ring's metadata back to the RTC-persistent vars. (The storage bytes
 * are already in RTC; only head/used live in the RAM logbuf_t.) */
static void ring_persist(void)
{
    s_log_ring_head = (uint16_t)s_ring.head;
    s_log_ring_used = (uint16_t)s_ring.used;
}

/* Dual-output vprintf hook: serial (always) + the RTC ring. vsnprintf/fwrite
 * run outside the lock; only the (short) ring append + metadata persist are
 * locked. Writing straight into RTC memory here is what makes the log
 * crash-safe — no buffering in RAM that a panic would lose. */
static int log_vprintf(const char *fmt, va_list args)
{
    int slot = -1;
    taskENTER_CRITICAL(&s_log_mux);
    for (int i = 0; i < LOG_BUF_COUNT; i++) {
        if (!s_fmt_used[i]) { s_fmt_used[i] = true; slot = i; break; }
    }
    taskEXIT_CRITICAL(&s_log_mux);

    if (slot < 0) return vprintf(fmt, args);  /* all slots busy — serial only */

    int len = vsnprintf(s_fmt_bufs[slot], LOG_BUF_SIZE, fmt, args);
    if (len < 0) len = 0;
    if (len >= LOG_BUF_SIZE) len = LOG_BUF_SIZE - 1;

    if (len > 0) fwrite(s_fmt_bufs[slot], 1, (size_t)len, stdout);

    taskENTER_CRITICAL(&s_log_mux);
    if (len > 0) {
        logbuf_append(&s_ring, s_fmt_bufs[slot], (uint32_t)len);
        ring_persist();
    }
    s_fmt_used[slot] = false;
    taskEXIT_CRITICAL(&s_log_mux);

    return len;
}

bool hokku_log_init(void)
{
    /* Reconstruct the RTC ring in place from its persisted metadata (the
     * s_log_ring storage bytes survived; head/used were persisted per append). */
    logbuf_attach(&s_ring, s_log_ring, LOG_RING_SIZE,
                  (uint32_t)s_log_ring_head, (uint32_t)s_log_ring_used);

    s_fmt_ready = true;
    for (int i = 0; i < LOG_BUF_COUNT; i++) {
        s_fmt_bufs[i] = heap_caps_malloc(LOG_BUF_SIZE, MALLOC_CAP_SPIRAM);
        if (!s_fmt_bufs[i]) s_fmt_ready = false;
    }

    if (s_fmt_ready) esp_log_set_vprintf(log_vprintf);
    return s_fmt_ready;
}

void log_level_apply(bool verbose)
{
    esp_log_level_set("*", verbose ? ESP_LOG_INFO : ESP_LOG_NONE);
}

size_t hokku_log_snapshot(char *out, size_t cap)
{
    if (!out || cap == 0) return 0;
    /* Latch the ring metadata under the lock (an O(1) struct copy), then copy
     * the bytes OUTSIDE the lock — holding the portMUX across a ~6 KB byte-copy
     * would disable interrupts with WiFi running. A concurrent append during
     * the out-of-lock copy can at worst tear a few bytes of diagnostics. */
    logbuf_t ring_copy;
    taskENTER_CRITICAL(&s_log_mux);
    ring_copy = s_ring;
    taskEXIT_CRITICAL(&s_log_mux);
    return logbuf_snapshot(&ring_copy, out, (uint32_t)cap);
}

void hokku_log_reset(void)
{
    taskENTER_CRITICAL(&s_log_mux);
    logbuf_reset(&s_ring);
    ring_persist();
    taskEXIT_CRITICAL(&s_log_mux);
}
