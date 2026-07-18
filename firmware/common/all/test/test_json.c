// Unit tests for json_escape (pure).
#include <string.h>

#include "test_harness.h"
#include "../json_util.c"

static const char *esc(const char *src)
{
    static char out[128];
    json_escape(out, sizeof(out), src);
    return out;
}

static void test_plain_passthrough(void)
{
    CHECK(strcmp(esc("hello world"), "hello world") == 0, "json_escape: plain text passes through");
}

static void test_escapes_quote_and_backslash(void)
{
    CHECK(strcmp(esc("a\"b"), "a\\\"b") == 0, "json_escape: escapes double-quote");
    CHECK(strcmp(esc("a\\b"), "a\\\\b") == 0, "json_escape: escapes backslash");
}

static void test_drops_control_chars(void)
{
    CHECK(strcmp(esc("a\nb\tc"), "abc") == 0, "json_escape: drops control chars (< 0x20)");
}

static void test_truncation_is_bounded(void)
{
    char out[4];
    json_escape(out, sizeof(out), "abcdefgh");
    CHECK(strlen(out) < sizeof(out) && out[sizeof(out) - 1] == '\0',
          "json_escape: truncates safely within dstlen and NUL-terminates");
}

static void test_quote_not_split_across_boundary(void)
{
    /* A quote needs 2 output bytes; if only 1 slot remains it must not be
     * half-written. dst[3] is the NUL slot, so effective room is small. */
    char out[3];   /* room for 2 chars + NUL */
    json_escape(out, sizeof(out), "\"\"");
    CHECK(out[sizeof(out) - 1] == '\0', "json_escape: never splits a 2-byte escape at the buffer edge");
}

int main(void)
{
    test_plain_passthrough();
    test_escapes_quote_and_backslash();
    test_drops_control_chars();
    test_truncation_is_bounded();
    test_quote_not_split_across_boundary();
    TEST_MAIN_END();
}
