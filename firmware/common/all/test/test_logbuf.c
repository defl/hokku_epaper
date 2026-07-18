// Unit tests for the pure logbuf circular buffer primitive.
// No mocks needed — logbuf is pure C.
#include <string.h>

#include "test_harness.h"
#include "../logbuf.c"

/* Snapshot into a NUL-terminated C string for easy comparison. */
static const char *snap(const logbuf_t *lb)
{
    static char out[512];
    uint32_t n = logbuf_snapshot(lb, out, sizeof(out) - 1);
    out[n] = '\0';
    return out;
}
static const char *snap_tail(const logbuf_t *lb, uint32_t cap)
{
    static char out[512];
    uint32_t n = logbuf_snapshot_tail(lb, out, cap);
    out[n] = '\0';
    return out;
}
static void append_str(logbuf_t *lb, const char *s) { logbuf_append(lb, s, (uint32_t)strlen(s)); }

static void test_empty(void)
{
    char store[16];
    logbuf_t lb;
    logbuf_init(&lb, store, sizeof(store));
    CHECK(logbuf_len(&lb) == 0, "logbuf: fresh buffer is empty");
    CHECK(strcmp(snap(&lb), "") == 0, "logbuf: empty snapshot is empty");
}

static void test_basic_append_snapshot(void)
{
    char store[16];
    logbuf_t lb;
    logbuf_init(&lb, store, sizeof(store));
    append_str(&lb, "hello");
    CHECK(logbuf_len(&lb) == 5, "logbuf: len tracks appended bytes");
    CHECK(strcmp(snap(&lb), "hello") == 0, "logbuf: snapshot returns appended content");
    append_str(&lb, "!");
    CHECK(strcmp(snap(&lb), "hello!") == 0, "logbuf: sequential appends concatenate");
}

static void test_exact_fill(void)
{
    char store[5];
    logbuf_t lb;
    logbuf_init(&lb, store, sizeof(store));
    append_str(&lb, "abcde");                 /* fills exactly */
    CHECK(logbuf_len(&lb) == 5, "logbuf: fills to capacity exactly");
    CHECK(strcmp(snap(&lb), "abcde") == 0, "logbuf: exact-fill snapshot correct");
}

static void test_circular_eviction(void)
{
    char store[5];
    logbuf_t lb;
    logbuf_init(&lb, store, sizeof(store));
    append_str(&lb, "abcde");                 /* full: abcde */
    append_str(&lb, "fg");                    /* evict a,b -> cdefg */
    CHECK(logbuf_len(&lb) == 5, "logbuf: stays at capacity after overflow");
    CHECK(strcmp(snap(&lb), "cdefg") == 0, "logbuf: circular keeps most-recent bytes");
}

static void test_append_larger_than_cap(void)
{
    char store[4];
    logbuf_t lb;
    logbuf_init(&lb, store, sizeof(store));
    append_str(&lb, "abcdefgh");              /* only last 4 survive */
    CHECK(logbuf_len(&lb) == 4, "logbuf: over-cap append clamps to capacity");
    CHECK(strcmp(snap(&lb), "efgh") == 0, "logbuf: over-cap append keeps last cap bytes");
}

static void test_snapshot_out_cap_limit(void)
{
    char store[16];
    logbuf_t lb;
    logbuf_init(&lb, store, sizeof(store));
    append_str(&lb, "abcdef");
    char out[4];
    uint32_t n = logbuf_snapshot(&lb, out, sizeof(out));
    CHECK(n == 4 && memcmp(out, "abcd", 4) == 0,
          "logbuf: snapshot honours out_cap (oldest-first prefix)");
}

static void test_snapshot_tail(void)
{
    char store[16];
    logbuf_t lb;
    logbuf_init(&lb, store, sizeof(store));
    append_str(&lb, "abcdef");
    CHECK(strcmp(snap_tail(&lb, 3), "def") == 0, "logbuf: snapshot_tail returns most-recent bytes");
    CHECK(strcmp(snap_tail(&lb, 99), "abcdef") == 0, "logbuf: snapshot_tail with big cap returns all");
}

static void test_snapshot_tail_wrapped(void)
{
    char store[5];
    logbuf_t lb;
    logbuf_init(&lb, store, sizeof(store));
    append_str(&lb, "abcde");
    append_str(&lb, "fg");                    /* wrapped: cdefg */
    CHECK(strcmp(snap_tail(&lb, 2), "fg") == 0, "logbuf: snapshot_tail correct on a wrapped buffer");
}

static void test_reset(void)
{
    char store[8];
    logbuf_t lb;
    logbuf_init(&lb, store, sizeof(store));
    append_str(&lb, "xyz");
    logbuf_reset(&lb);
    CHECK(logbuf_len(&lb) == 0 && strcmp(snap(&lb), "") == 0, "logbuf: reset empties the buffer");
    append_str(&lb, "new");
    CHECK(strcmp(snap(&lb), "new") == 0, "logbuf: usable again after reset");
}

static void test_append_buf_join(void)
{
    char sa[16], sb[16], sd[16];
    logbuf_t a, b, d;
    logbuf_init(&a, sa, sizeof(sa));
    logbuf_init(&b, sb, sizeof(sb));
    logbuf_init(&d, sd, sizeof(sd));
    append_str(&a, "CARRY");
    append_str(&b, "active");
    logbuf_append_buf(&d, &a);
    logbuf_append_buf(&d, &b);
    CHECK(strcmp(snap(&d), "CARRYactive") == 0,
          "logbuf: append_buf joins carry+active in order (the ESP32 upload model)");
}

static void test_append_buf_from_wrapped(void)
{
    char sa[5], sd[16];
    logbuf_t a, d;
    logbuf_init(&a, sa, sizeof(sa));
    logbuf_init(&d, sd, sizeof(sd));
    append_str(&a, "abcde");
    append_str(&a, "fg");                     /* a is wrapped: cdefg */
    logbuf_append_buf(&d, &a);
    CHECK(strcmp(snap(&d), "cdefg") == 0, "logbuf: append_buf reads a wrapped source oldest-first");
}

static void test_attach_resume(void)
{
    /* Simulate RTC persistence: storage bytes + (head,used) survive; a fresh
     * logbuf_t is reconstructed via attach and must resume in place. */
    char store[8];
    logbuf_t lb;
    logbuf_init(&lb, store, sizeof(store));
    append_str(&lb, "boot1");                 /* used=5, head=5 */
    uint32_t saved_head = lb.head, saved_used = lb.used;

    logbuf_t resumed;                         /* storage unchanged, metadata restored */
    logbuf_attach(&resumed, store, sizeof(store), saved_head, saved_used);
    CHECK(strcmp(snap(&resumed), "boot1") == 0, "logbuf: attach resumes persisted content");
    append_str(&resumed, "23");
    CHECK(strcmp(snap(&resumed), "boot123") == 0, "logbuf: resumed buffer appends correctly");
}

static void test_attach_rejects_bad_metadata(void)
{
    char store[8];
    logbuf_t lb;
    logbuf_attach(&lb, store, sizeof(store), 99, 99);   /* garbage (POR) */
    CHECK(logbuf_len(&lb) == 0, "logbuf: attach clamps invalid persisted metadata to empty");
}

int main(void)
{
    test_empty();
    test_basic_append_snapshot();
    test_exact_fill();
    test_circular_eviction();
    test_append_larger_than_cap();
    test_snapshot_out_cap_limit();
    test_snapshot_tail();
    test_snapshot_tail_wrapped();
    test_reset();
    test_append_buf_join();
    test_append_buf_from_wrapped();
    test_attach_resume();
    test_attach_rejects_bad_metadata();
    TEST_MAIN_END();
}
