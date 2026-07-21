// SoC-agnostic circular log buffer primitive, shared by ALL hokku firmwares.
// Pure C — no ESP-IDF or XR872 SDK headers. This is the unified logging core:
//   - the ESP32 logger (common/esp32/log.c) builds a two-tier scheme on it
//     (a large active buffer in PSRAM + a small carry buffer in RTC memory);
//   - the F7 logger builds a single-buffer logger on it.
//
// The buffer is CIRCULAR: when an append would overflow, the oldest bytes are
// evicted so the most-recent content is always retained (recent log lines are
// the most useful for diagnostics). Storage is caller-owned — that lets the
// caller place it wherever it must live (PSRAM, RTC-persistent memory, a plain
// static array). This primitive does NO locking; a caller with concurrent
// writers wraps the calls in its own critical section.
#pragma once

#include <stdint.h>
#include <stddef.h>

typedef struct {
    char    *buf;    /* caller-owned storage */
    uint32_t cap;    /* storage capacity in bytes */
    uint32_t head;   /* index of the next write (0..cap-1) */
    uint32_t used;   /* bytes currently held (0..cap) */
} logbuf_t;

/* Attach a logbuf to caller-owned storage, resuming an existing (head,used)
 * state. Use this to reconstruct a buffer whose STORAGE + (head,used) were
 * persisted across a reset (e.g. the ESP32 RTC carry buffer): the storage
 * bytes survive, and passing the persisted head/used resumes it in place.
 * Invalid (head>=cap or used>cap) is clamped to empty. */
void logbuf_attach(logbuf_t *lb, char *storage, uint32_t cap, uint32_t head, uint32_t used);

/* Attach + reset to empty (fresh buffer). */
void logbuf_init(logbuf_t *lb, char *storage, uint32_t cap);

/* Append len bytes, evicting the oldest bytes if needed to fit (circular).
 * If len >= cap, only the last cap bytes of data are kept. */
void logbuf_append(logbuf_t *lb, const char *data, uint32_t len);

/* Append the entire contents of src onto dst (as a byte sequence). Convenience
 * for joining/spilling one buffer into another. */
void logbuf_append_buf(logbuf_t *dst, const logbuf_t *src);

/* Copy contents oldest->newest into out (up to out_cap bytes). Returns the
 * number of bytes written. Does not NUL-terminate. */
uint32_t logbuf_snapshot(const logbuf_t *lb, char *out, uint32_t out_cap);

/* Copy the MOST-RECENT bytes into out (up to out_cap). If used <= out_cap this
 * is the whole buffer; otherwise the last out_cap bytes. Returns bytes written.
 * Used when spilling a large active buffer into a smaller carry buffer. */
uint32_t logbuf_snapshot_tail(const logbuf_t *lb, char *out, uint32_t out_cap);

/* Clear to empty. */
void logbuf_reset(logbuf_t *lb);

/* Bytes currently held. */
static inline uint32_t logbuf_len(const logbuf_t *lb) { return lb->used; }
