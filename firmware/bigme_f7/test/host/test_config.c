/*
 * test_config.c — host-side unit tests for hokku_config.c:
 *   - hokku_config_load  (fdcm unavailable -> defaults; magic/version
 *                          mismatch -> defaults; valid blob -> fields read)
 *   - hokku_config_save  (stamps magic/version; propagates fdcm_write failure)
 *
 * The fdcm mock in mocks/image/fdcm.h exposes _mock_fdcm_* variables that
 * control what fdcm_open/fdcm_read/fdcm_write do.
 *
 * Build: compiled by firmware/bigme_f7/test/host/CMakeLists.txt.
 * Run:   ./test_config   (exit 0 on all pass, 1 if any fail)
 */

#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>

#include "mocks/image/fdcm.h"

#include "../../../common/xr872/hokku_config.c"

/* ── Minimal test framework ────────────────────────────────────────────── */
static int g_pass = 0;
static int g_fail = 0;

#define CHECK(cond, name) do {                                      \
    if (cond) { printf("PASS  %s\n", name); g_pass++; }             \
    else       { printf("FAIL  %s\n", name); g_fail++; }            \
} while (0)

/* ── Helpers ───────────────────────────────────────────────────────────── */

static void reset_mock_fdcm(void)
{
    _mock_fdcm_open_fail = 0;
    memset(_mock_fdcm_read_buf, 0, sizeof(_mock_fdcm_read_buf));
    _mock_fdcm_read_size = 0;
    memset(_mock_fdcm_write_buf, 0, sizeof(_mock_fdcm_write_buf));
    _mock_fdcm_write_size = 0;
    _mock_fdcm_write_call_count = 0;
    memset(&g_cfg, 0, sizeof(g_cfg));
    g_cfg_fdcm = NULL;
}

/* Fill the mock's "flash" bytes with a valid, load-able hokku_config_t. */
static void seed_valid_saved_config(void)
{
    hokku_config_t c;
    memset(&c, 0, sizeof(c));
    c.magic   = HOKKU_CFG_MAGIC;
    c.version = HOKKU_CFG_VERSION;
    strncpy(c.server_url, "http://10.0.0.5:8080/hokku/screen/", HOKKU_URL_MAX - 1);
    strncpy(c.screen_name, "kitchen", HOKKU_NAME_MAX - 1);
    c.use_dhcp = 1;
    c.power_mode = HOKKU_PWR_SLEEP;
    c.default_sleep_s = 600;
    memcpy(_mock_fdcm_read_buf, &c, sizeof(c));
    _mock_fdcm_read_size = sizeof(c);
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_config_load — fdcm unavailable
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_load_uses_defaults_when_fdcm_open_fails(void)
{
    reset_mock_fdcm();
    _mock_fdcm_open_fail = 1;
    hokku_config_load();
    CHECK(g_cfg.magic == HOKKU_CFG_MAGIC && g_cfg.version == HOKKU_CFG_VERSION,
          "config_load: falls back to defaults when fdcm_open fails");
    CHECK(strcmp(g_cfg.screen_name, "bigme-f7") == 0,
          "config_load: default screen_name is 'bigme-f7'");
    CHECK(g_cfg.power_mode == HOKKU_PWR_AUTO,
          "config_load: default power_mode is AUTO");
    CHECK(g_cfg.default_sleep_s == 300,
          "config_load: default default_sleep_s is 300");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_config_load — saved blob invalid (magic/version mismatch, or short read)
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_load_uses_defaults_when_magic_wrong(void)
{
    reset_mock_fdcm();
    seed_valid_saved_config();
    /* Corrupt the magic in the "flash" bytes directly. */
    hokku_config_t *seeded = (hokku_config_t *)_mock_fdcm_read_buf;
    seeded->magic = 0xDEADBEEFU;
    hokku_config_load();
    CHECK(strcmp(g_cfg.screen_name, "bigme-f7") == 0,
          "config_load: falls back to defaults when the magic doesn't match");
}
static void test_load_uses_defaults_when_version_wrong(void)
{
    reset_mock_fdcm();
    seed_valid_saved_config();
    hokku_config_t *seeded = (hokku_config_t *)_mock_fdcm_read_buf;
    seeded->version = HOKKU_CFG_VERSION - 1; /* a stale v1 blob */
    hokku_config_load();
    CHECK(strcmp(g_cfg.screen_name, "bigme-f7") == 0,
          "config_load: falls back to defaults on a version mismatch (rejects a stale blob)");
}
static void test_load_uses_defaults_on_short_read(void)
{
    reset_mock_fdcm();
    seed_valid_saved_config();
    _mock_fdcm_read_size = sizeof(hokku_config_t) - 1; /* short read */
    hokku_config_load();
    CHECK(strcmp(g_cfg.screen_name, "bigme-f7") == 0,
          "config_load: falls back to defaults when fdcm_read returns fewer bytes than expected");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_config_load — valid saved blob
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_load_reads_saved_fields(void)
{
    reset_mock_fdcm();
    seed_valid_saved_config();
    hokku_config_load();
    CHECK(strcmp(g_cfg.server_url, "http://10.0.0.5:8080/hokku/screen/") == 0,
          "config_load: reads server_url from the saved blob");
    CHECK(strcmp(g_cfg.screen_name, "kitchen") == 0,
          "config_load: reads screen_name from the saved blob");
    CHECK(g_cfg.use_dhcp == 1, "config_load: reads use_dhcp from the saved blob");
    CHECK(g_cfg.power_mode == HOKKU_PWR_SLEEP,
          "config_load: reads power_mode from the saved blob");
    CHECK(g_cfg.default_sleep_s == 600,
          "config_load: reads default_sleep_s from the saved blob");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_config_get
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_get_returns_pointer_to_live_config(void)
{
    reset_mock_fdcm();
    _mock_fdcm_open_fail = 1;
    hokku_config_load();
    hokku_config_t *c = hokku_config_get();
    strncpy(c->screen_name, "changed-via-get", HOKKU_NAME_MAX - 1);
    CHECK(strcmp(g_cfg.screen_name, "changed-via-get") == 0,
          "config_get: returns a pointer to the live (mutable) config, not a copy");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_config_save
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_save_stamps_magic_and_version(void)
{
    reset_mock_fdcm();
    memset(&g_cfg, 0, sizeof(g_cfg));
    g_cfg.magic = 0; g_cfg.version = 0; /* not yet stamped */
    strncpy(g_cfg.screen_name, "test", HOKKU_NAME_MAX - 1);
    int rc = hokku_config_save();
    CHECK(rc == 0, "config_save: returns 0 on success");
    const hokku_config_t *written = (const hokku_config_t *)_mock_fdcm_write_buf;
    CHECK(written->magic == HOKKU_CFG_MAGIC && written->version == HOKKU_CFG_VERSION,
          "config_save: stamps magic/version into the written blob");
    CHECK(_mock_fdcm_write_size == sizeof(hokku_config_t),
          "config_save: writes exactly sizeof(hokku_config_t) bytes");
}
static void test_save_opens_fdcm_lazily_if_not_yet_open(void)
{
    reset_mock_fdcm();
    g_cfg_fdcm = NULL; /* simulate save() called before load() ever opened it */
    hokku_config_save();
    CHECK(_mock_fdcm_write_call_count == 1,
          "config_save: opens the fdcm handle lazily if load() hasn't already");
}
static void test_save_fails_when_fdcm_open_fails(void)
{
    reset_mock_fdcm();
    g_cfg_fdcm = NULL;
    _mock_fdcm_open_fail = 1;
    CHECK(hokku_config_save() == -1,
          "config_save: returns -1 when fdcm_open fails and no handle exists yet");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  Entry point
 * ═══════════════════════════════════════════════════════════════════════ */

int main(void)
{
    printf("=== test_config (bigme_f7) ===\n\n");

    test_load_uses_defaults_when_fdcm_open_fails();
    test_load_uses_defaults_when_magic_wrong();
    test_load_uses_defaults_when_version_wrong();
    test_load_uses_defaults_on_short_read();
    test_load_reads_saved_fields();
    test_get_returns_pointer_to_live_config();
    test_save_stamps_magic_and_version();
    test_save_opens_fdcm_lazily_if_not_yet_open();
    test_save_fails_when_fdcm_open_fails();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
