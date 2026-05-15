/*
 * test_config.c — host-side unit tests for config.c:
 *   - config_load   (NVS unavailable; NVS available with data)
 *   - config_is_valid
 *
 * The NVS mock in mocks/nvs_flash.h exposes _mock_nvs_* variables that
 * control what nvs_open / nvs_get_u8 / nvs_get_str return.
 *
 * Build: compiled by firmware/test/host/CMakeLists.txt.
 * Run:   ./test_config   (exit 0 on all pass, 1 if any fail)
 */

#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>

#include "mocks/nvs_flash.h"

#include "../../main/config.c"

/* ── Minimal test framework ────────────────────────────────────────────── */
static int g_pass = 0;
static int g_fail = 0;

#define CHECK(cond, name) do {                                      \
    if (cond) { printf("PASS  %s\n", name); g_pass++; }            \
    else       { printf("FAIL  %s\n", name); g_fail++; }           \
} while (0)

/* ── Helpers ───────────────────────────────────────────────────────────── */

static void reset_mock_nvs(void)
{
    _mock_nvs_open_fail = 1;
    _mock_nvs_cfg_ver   = 0;
    memset(_mock_nvs_wifi_ssid,   0, sizeof(_mock_nvs_wifi_ssid));
    memset(_mock_nvs_wifi_pass,   0, sizeof(_mock_nvs_wifi_pass));
    memset(_mock_nvs_image_url,   0, sizeof(_mock_nvs_image_url));
    memset(_mock_nvs_screen_name, 0, sizeof(_mock_nvs_screen_name));
    memset(&config, 0, sizeof(config));
}

/* ═══════════════════════════════════════════════════════════════════════
 *  config_load — NVS unavailable
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_config_load_returns_false_when_nvs_open_fails(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 1;
    CHECK(!config_load(), "config_load: returns false when NVS open fails");
}

static void test_config_load_leaves_config_zeroed_when_nvs_unavailable(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 1;
    config_load();
    CHECK(config.cfg_ver == 0 && config.wifi_ssid[0] == '\0',
          "config_load: config unchanged (zeroed) when NVS open fails");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  config_load — NVS available
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_config_load_returns_true_when_nvs_available(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    CHECK(config_load(), "config_load: returns true when NVS open succeeds");
}

static void test_config_load_reads_cfg_ver(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    _mock_nvs_cfg_ver   = 1;
    config_load();
    CHECK(config.cfg_ver == 1, "config_load: reads cfg_ver from NVS");
}

static void test_config_load_reads_wifi_ssid(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    strncpy(_mock_nvs_wifi_ssid, "TestNet", sizeof(_mock_nvs_wifi_ssid) - 1);
    config_load();
    CHECK(strcmp(config.wifi_ssid, "TestNet") == 0,
          "config_load: reads wifi_ssid from NVS");
}

static void test_config_load_reads_wifi_pass(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    strncpy(_mock_nvs_wifi_pass, "s3cr3t!", sizeof(_mock_nvs_wifi_pass) - 1);
    config_load();
    CHECK(strcmp(config.wifi_pass, "s3cr3t!") == 0,
          "config_load: reads wifi_pass from NVS");
}

static void test_config_load_reads_image_url(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    strncpy(_mock_nvs_image_url, "http://10.0.0.1/img.bin",
            sizeof(_mock_nvs_image_url) - 1);
    config_load();
    CHECK(strcmp(config.image_url, "http://10.0.0.1/img.bin") == 0,
          "config_load: reads image_url from NVS");
}

static void test_config_load_reads_screen_name(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    strncpy(_mock_nvs_screen_name, "living-room", sizeof(_mock_nvs_screen_name) - 1);
    config_load();
    CHECK(strcmp(config.screen_name, "living-room") == 0,
          "config_load: reads screen_name from NVS");
}

static void test_config_load_screen_name_optional(void)
{
    /* screen_name left empty in NVS — config still loads and is valid
     * as long as ssid + url are set. */
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    _mock_nvs_cfg_ver   = CONFIG_VERSION;
    strncpy(_mock_nvs_wifi_ssid,  "Net",           sizeof(_mock_nvs_wifi_ssid)  - 1);
    strncpy(_mock_nvs_image_url,  "http://h/i.bin", sizeof(_mock_nvs_image_url) - 1);
    /* _mock_nvs_screen_name stays empty */
    config_load();
    CHECK(config.screen_name[0] == '\0',
          "config_load: screen_name is empty when not set in NVS");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  config_is_valid
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_config_invalid_when_both_empty(void)
{
    memset(&config, 0, sizeof(config));
    CHECK(!config_is_valid(),
          "config_is_valid: returns false when ssid and url are empty");
}

static void test_config_invalid_when_only_ssid_set(void)
{
    memset(&config, 0, sizeof(config));
    strncpy(config.wifi_ssid, "MySSID", sizeof(config.wifi_ssid) - 1);
    CHECK(!config_is_valid(),
          "config_is_valid: returns false when url is empty");
}

static void test_config_invalid_when_only_url_set(void)
{
    memset(&config, 0, sizeof(config));
    strncpy(config.image_url, "http://example.com/img", sizeof(config.image_url) - 1);
    CHECK(!config_is_valid(),
          "config_is_valid: returns false when ssid is empty");
}

static void test_config_valid_when_both_set(void)
{
    memset(&config, 0, sizeof(config));
    strncpy(config.wifi_ssid,  "MySSID",                sizeof(config.wifi_ssid)  - 1);
    strncpy(config.image_url,  "http://example.com/img", sizeof(config.image_url) - 1);
    CHECK(config_is_valid(),
          "config_is_valid: returns true when ssid and url are both set");
}

static void test_config_valid_ignores_pass_and_screen_name(void)
{
    /* wifi_pass and screen_name are optional — valid without them. */
    memset(&config, 0, sizeof(config));
    strncpy(config.wifi_ssid, "Net",  sizeof(config.wifi_ssid)  - 1);
    strncpy(config.image_url, "http://h/i", sizeof(config.image_url) - 1);
    CHECK(config_is_valid(),
          "config_is_valid: true even when wifi_pass and screen_name are empty");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  Entry point
 * ═══════════════════════════════════════════════════════════════════════ */

int main(void)
{
    printf("=== test_config ===\n\n");

    /* config_load — NVS unavailable */
    test_config_load_returns_false_when_nvs_open_fails();
    test_config_load_leaves_config_zeroed_when_nvs_unavailable();

    /* config_load — NVS available */
    test_config_load_returns_true_when_nvs_available();
    test_config_load_reads_cfg_ver();
    test_config_load_reads_wifi_ssid();
    test_config_load_reads_wifi_pass();
    test_config_load_reads_image_url();
    test_config_load_reads_screen_name();
    test_config_load_screen_name_optional();

    /* config_is_valid */
    test_config_invalid_when_both_empty();
    test_config_invalid_when_only_ssid_set();
    test_config_invalid_when_only_url_set();
    test_config_valid_when_both_set();
    test_config_valid_ignores_pass_and_screen_name();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
