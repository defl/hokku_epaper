/*
 * test_config.c — host-side unit tests for config.c:
 *   - config_load   (NVS unavailable; all six fields; primary/secondary wifi)
 *   - config_is_valid (version check; primary required; secondary optional)
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
    _mock_nvs_open_fail  = 1;
    _mock_nvs_cfg_ver    = 0;
    _mock_nvs_wifi_order = 0;
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
    CHECK(config.cfg_ver == 0 && config.wifi_ssid[0][0] == '\0',
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
    _mock_nvs_cfg_ver   = CONFIG_VERSION;
    config_load();
    CHECK(config.cfg_ver == CONFIG_VERSION, "config_load: reads cfg_ver from NVS");
}

static void test_config_load_reads_primary_wifi_ssid(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    strncpy(_mock_nvs_wifi_ssid[0], "PrimaryNet", sizeof(_mock_nvs_wifi_ssid[0]) - 1);
    config_load();
    CHECK(strcmp(config.wifi_ssid[0], "PrimaryNet") == 0,
          "config_load: reads wifi_ssid1 into slot 0");
}

static void test_config_load_reads_primary_wifi_pass(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    strncpy(_mock_nvs_wifi_pass[0], "s3cr3t!", sizeof(_mock_nvs_wifi_pass[0]) - 1);
    config_load();
    CHECK(strcmp(config.wifi_pass[0], "s3cr3t!") == 0,
          "config_load: reads wifi_pass1 into slot 0");
}

static void test_config_load_reads_secondary_wifi_ssid(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    strncpy(_mock_nvs_wifi_ssid[1], "BackupNet", sizeof(_mock_nvs_wifi_ssid[1]) - 1);
    config_load();
    CHECK(strcmp(config.wifi_ssid[1], "BackupNet") == 0,
          "config_load: reads wifi_ssid2 into slot 1");
}

static void test_config_load_reads_secondary_wifi_pass(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    strncpy(_mock_nvs_wifi_pass[1], "backup_pw", sizeof(_mock_nvs_wifi_pass[1]) - 1);
    config_load();
    CHECK(strcmp(config.wifi_pass[1], "backup_pw") == 0,
          "config_load: reads wifi_pass2 into slot 1");
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

static void test_config_load_secondary_absent_leaves_slot1_empty(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail = 0;
    _mock_nvs_cfg_ver   = CONFIG_VERSION;
    strncpy(_mock_nvs_wifi_ssid[0], "PrimaryNet", sizeof(_mock_nvs_wifi_ssid[0]) - 1);
    /* _mock_nvs_wifi_ssid[1] stays empty */
    config_load();
    CHECK(config.wifi_ssid[1][0] == '\0',
          "config_load: secondary slot is empty when wifi_ssid2 not in NVS");
}

static void test_config_load_reads_wifi_order_primary_first(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail  = 0;
    _mock_nvs_wifi_order = WIFI_ORDER_PRIMARY_FIRST;
    config_load();
    CHECK(config.wifi_order == WIFI_ORDER_PRIMARY_FIRST,
          "config_load: reads wifi_order=PRIMARY_FIRST from NVS");
}

static void test_config_load_reads_wifi_order_last_first(void)
{
    reset_mock_nvs();
    _mock_nvs_open_fail  = 0;
    _mock_nvs_wifi_order = WIFI_ORDER_LAST_FIRST;
    config_load();
    CHECK(config.wifi_order == WIFI_ORDER_LAST_FIRST,
          "config_load: reads wifi_order=LAST_FIRST from NVS");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  config_version_ok
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_config_version_ok_when_matches(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION;
    CHECK(config_version_ok(), "config_version_ok: returns true when cfg_ver == CONFIG_VERSION");
}

static void test_config_version_not_ok_when_zero(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = 0;
    CHECK(!config_version_ok(), "config_version_ok: returns false when cfg_ver is 0");
}

static void test_config_version_not_ok_when_old(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION - 1;
    CHECK(!config_version_ok(),
          "config_version_ok: returns false when cfg_ver is one version behind");
}

static void test_config_version_not_ok_when_future(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION + 1;
    CHECK(!config_version_ok(),
          "config_version_ok: returns false when cfg_ver is ahead of firmware");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  config_is_valid
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_config_invalid_when_cfg_ver_wrong(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION - 1;
    strncpy(config.wifi_ssid[0], "Net",       sizeof(config.wifi_ssid[0]) - 1);
    strncpy(config.image_url,    "http://h/i", sizeof(config.image_url)   - 1);
    CHECK(!config_is_valid(),
          "config_is_valid: returns false when cfg_ver != CONFIG_VERSION");
}

static void test_config_invalid_when_both_empty(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION;
    CHECK(!config_is_valid(),
          "config_is_valid: returns false when primary ssid and url are empty");
}

static void test_config_invalid_when_only_primary_ssid_set(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION;
    strncpy(config.wifi_ssid[0], "Net", sizeof(config.wifi_ssid[0]) - 1);
    CHECK(!config_is_valid(),
          "config_is_valid: returns false when url is empty");
}

static void test_config_invalid_when_only_url_set(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION;
    strncpy(config.image_url, "http://example.com/img", sizeof(config.image_url) - 1);
    CHECK(!config_is_valid(),
          "config_is_valid: returns false when primary ssid is empty");
}

static void test_config_invalid_when_only_secondary_set(void)
{
    /* Secondary alone is not enough — primary is required */
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION;
    strncpy(config.wifi_ssid[1], "BackupNet",   sizeof(config.wifi_ssid[1]) - 1);
    strncpy(config.image_url,    "http://h/i",  sizeof(config.image_url)    - 1);
    CHECK(!config_is_valid(),
          "config_is_valid: returns false when only secondary ssid is set");
}

static void test_config_valid_primary_only(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION;
    strncpy(config.wifi_ssid[0], "PrimaryNet",  sizeof(config.wifi_ssid[0]) - 1);
    strncpy(config.image_url,    "http://h/i",  sizeof(config.image_url)    - 1);
    CHECK(config_is_valid(),
          "config_is_valid: returns true with only primary ssid and url set");
}

static void test_config_valid_both_networks(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION;
    strncpy(config.wifi_ssid[0], "PrimaryNet",  sizeof(config.wifi_ssid[0]) - 1);
    strncpy(config.wifi_ssid[1], "BackupNet",   sizeof(config.wifi_ssid[1]) - 1);
    strncpy(config.image_url,    "http://h/i",  sizeof(config.image_url)    - 1);
    CHECK(config_is_valid(),
          "config_is_valid: returns true with both networks and url set");
}

static void test_config_valid_ignores_pass_and_screen_name(void)
{
    /* wifi_pass and screen_name are optional */
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION;
    strncpy(config.wifi_ssid[0], "Net",       sizeof(config.wifi_ssid[0]) - 1);
    strncpy(config.image_url,    "http://h/i", sizeof(config.image_url)   - 1);
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
    test_config_load_reads_primary_wifi_ssid();
    test_config_load_reads_primary_wifi_pass();
    test_config_load_reads_secondary_wifi_ssid();
    test_config_load_reads_secondary_wifi_pass();
    test_config_load_reads_image_url();
    test_config_load_reads_screen_name();
    test_config_load_secondary_absent_leaves_slot1_empty();
    test_config_load_reads_wifi_order_primary_first();
    test_config_load_reads_wifi_order_last_first();

    /* config_version_ok */
    test_config_version_ok_when_matches();
    test_config_version_not_ok_when_zero();
    test_config_version_not_ok_when_old();
    test_config_version_not_ok_when_future();

    /* config_is_valid */
    test_config_invalid_when_cfg_ver_wrong();
    test_config_invalid_when_both_empty();
    test_config_invalid_when_only_primary_ssid_set();
    test_config_invalid_when_only_url_set();
    test_config_invalid_when_only_secondary_set();
    test_config_valid_primary_only();
    test_config_valid_both_networks();
    test_config_valid_ignores_pass_and_screen_name();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
