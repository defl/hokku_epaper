// Unit tests for screen_ident (pure): the X-Screen-Mac format and the rule a
// server-sent rename must pass before a board persists it.
#include <string.h>

#include "test_harness.h"
#include "../screen_ident.c"

static void test_mac_format(void)
{
    const uint8_t mac[6] = {0x02, 0x0A, 0xBC, 0x0D, 0xE0, 0x05};
    char out[HOKKU_MAC_STR_LEN];
    hokku_mac_format(mac, out, sizeof(out));
    CHECK(strcmp(out, "02:0a:bc:0d:e0:05") == 0, "mac: lowercase, colon-separated, zero-padded");
}

static void test_mac_zero_is_unknown(void)
{
    const uint8_t mac[6] = {0};
    char out[HOKKU_MAC_STR_LEN] = "junk";
    hokku_mac_format(mac, out, sizeof(out));
    CHECK(out[0] == '\0', "mac: an all-zero MAC formats as empty (unknown)");
}

static void test_mac_short_buffer(void)
{
    const uint8_t mac[6] = {1, 2, 3, 4, 5, 6};
    char out[10] = "junk";
    hokku_mac_format(mac, out, sizeof(out));
    CHECK(out[0] == '\0', "mac: a buffer too small for the full string yields empty, not a prefix");
}

static void test_name_accepts(void)
{
    CHECK(hokku_screen_name_valid("living-room"), "name: plain name accepted");
    CHECK(hokku_screen_name_valid("Kid's room 2 (east)_v1.0"),
          "name: inner spaces and the allowed punctuation accepted");
    char max[HOKKU_SCREEN_NAME_MAX + 1];
    memset(max, 'a', HOKKU_SCREEN_NAME_MAX);
    max[HOKKU_SCREEN_NAME_MAX] = '\0';
    CHECK(hokku_screen_name_valid(max), "name: exactly the max length accepted");
}

static void test_name_rejects(void)
{
    CHECK(!hokku_screen_name_valid(NULL), "name: NULL rejected");
    CHECK(!hokku_screen_name_valid(""), "name: empty rejected");
    CHECK(!hokku_screen_name_valid(" lead"), "name: leading space rejected");
    CHECK(!hokku_screen_name_valid("trail "), "name: trailing space rejected");
    CHECK(!hokku_screen_name_valid("a/b"), "name: slash rejected (breaks per-screen URLs)");
    CHECK(!hokku_screen_name_valid("a\r\nX-Evil: 1"), "name: CR/LF rejected (header injection)");
    CHECK(!hokku_screen_name_valid("caf\xc3\xa9"), "name: non-ASCII rejected");
    CHECK(!hokku_screen_name_valid("<b>x</b>"), "name: markup characters rejected");
    CHECK(!hokku_screen_name_valid("a\"b"), "name: double quote rejected");
    CHECK(!hokku_screen_name_valid("a\\b"), "name: backslash rejected");
    char over[HOKKU_SCREEN_NAME_MAX + 2];
    memset(over, 'a', HOKKU_SCREEN_NAME_MAX + 1);
    over[HOKKU_SCREEN_NAME_MAX + 1] = '\0';
    CHECK(!hokku_screen_name_valid(over), "name: one byte over the max rejected");
}

int main(void)
{
    test_mac_format();
    test_mac_zero_is_unknown();
    test_mac_short_buffer();
    test_name_accepts();
    test_name_rejects();
    TEST_MAIN_END();
}
