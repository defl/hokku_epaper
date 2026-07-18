/*
 * test_logic.c — host-side unit tests for pure logic in hokku_config.c/main.c:
 *   - hokku_xip_offset          (per-slot XIP flash offset arithmetic)
 *   - hokku_should_sleep        (power-mode decision)
 *   - read_resp_header_uint/str (HTTP response header parsing)
 *   - hokku_battery_mv          (ADC scaling + clamp + range check)
 *   - hlog / hlog_reset         (bounded, truncating log ring)
 *   - hokku_build_firmware_url  (server_url -> firmware.bin URL derivation)
 *   - hokku_rollback_arm/commit (A/B try-boot rollback — the brick-prevention
 *                                 logic; the highest-value target here)
 *   - hokku_do_ota's erase-range guard (rejects writes outside the safe
 *                                 window; both A/B directions must be allowed
 *                                 — this exact guard was hardware-tested and
 *                                 fixed once already, see AGENTS/docs)
 *   - hokku_wifi_provision      (sysinfo persistence + length validation)
 *   - hokku_hibernate           (sleep_s clamping, 5..60000)
 *   - net_cb                    (WLAN_CONNECTED static-IP/DHCP branching —
 *                                 the exact class of bug fixed this session:
 *                                 netif_set_addr() vs a no-op netif_set_up())
 *
 * NOT covered here (documented gaps, not silent omissions):
 *   - epd.c: pure hardware SPI/GPIO bit-banging, no host-testable logic
 *     (unlike the ESP32's text_render.c, there's no pure-software module to
 *     extract here).
 *   - do_refresh() / refresh_thread_fn(): top-level HTTP+EPD streaming
 *     orchestration; integration-level, not unit-tested (mirrors huessen's
 *     firmware, which also doesn't unit-test its top-level refresh loop).
 *   - command.c (`cfg`/`wifi`/`ota` console dispatch): argv parsing/routing
 *     over SDK console utilities not otherwise mocked here; a reasonable
 *     follow-up, not included in this pass.
 *
 * Strategy: identical to the ESP32 test_logic.c — include all XR872 SDK mock
 * headers BEFORE redefining `static`, then include the firmware source so
 * static functions/globals become regular symbols in this translation unit.
 *
 * Build: compiled by firmware/bigme_f7/test/host/CMakeLists.txt.
 * Run:   ./test_logic   (exit 0 on all pass, 1 if any fail)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>

/* ── Mock headers (included before #define static; paths mirror the real
 *    XR872 SDK tree so main.c's own #include lines resolve unchanged) ──── */
#include "mocks/kernel/os/os.h"
#include "mocks/common/framework/platform_init.h"
#include "mocks/common/framework/net_ctrl.h"
#include "mocks/net/HTTPClient/HTTPCUsr_api.h"
#include "mocks/net/HTTPClient/API/HTTPClient.h"
#include "mocks/net/HTTPClient/API/HTTPClientCommon.h"
#include "mocks/lwip/netif.h"
#include "mocks/lwip/dhcp.h"
#include "mocks/lwip/ip_addr.h"
#include "mocks/image/image.h"
#include "mocks/image/fdcm.h"
#include "mocks/ota/ota.h"
#include "mocks/driver/chip/hal_wdg.h"
#include "mocks/driver/chip/hal_adc.h"
#include "mocks/driver/chip/hal_wakeup.h"
#include "mocks/net/wlan/wlan.h"
#include "mocks/common/framework/sysinfo.h"
#include "mocks/pm/pm.h"

/* ── Expose all static functions/globals from the firmware source ───────
 * #define static must come AFTER the mock headers so their own static-inline
 * mock functions keep their intended storage class. main.c defines its own
 * `int main(void)`; rename it so it doesn't collide with this file's. */
#define static
#define main hokku_main_unused

#include "../../hokku_config.c"
#include "../../led.c"    /* led_usb_present() -> _mock_gpio, shared with main.c below */
#include "../../../common/all/firmware_url.c"  /* SoC-agnostic (shared with ESP32) */
#include "../../../common/all/frame_state.c"   /* SoC-agnostic (shared with ESP32) */
#include "../../../common/all/logbuf.c"        /* SoC-agnostic (shared with ESP32) */
#include "../../main.c"

#undef main
#undef static
/* Critical: without this #undef, `static uint8_t buf[65536];` below would be
 * macro-expanded to a plain automatic (stack) array — heap_get_space() would
 * return dangling pointers into a frame that's gone the instant it returns.
 * Caught by cppcheck's autoVariables check, not by the tests themselves (the
 * stack slot happens to survive long enough within a single call to pass). */

/* Hardware I/O stubs: epd.c is pure SPI/GPIO bit-banging (documented gap
 * above, not unit tested), but its symbols are referenced (address-taken/
 * called) inside do_refresh()/refresh_thread_fn(), which ARE compiled into
 * this translation unit even though this file never calls them — so the
 * linker still needs definitions. Also heap_get_space/pm_start/
 * HAL_Flash_Init/HAL_Xip_Init/platform_cache_init, which main.c declares
 * via bare `extern` (no header, so no mock header covers them). */
void epd_send_cmd(uint8_t cmd) { (void)cmd; }
void epd_send_data(uint8_t data) { (void)data; }
void epd_init(void) { }
void epd_refresh(void) { }
void heap_get_space(uint8_t **start, uint8_t **end, uint8_t **current)
{
    static uint8_t buf[65536];
    *start = buf;
    *end = buf + sizeof(buf);
    *current = buf + 4096; /* pretend 4 KB used, rest free */
}
void pm_start(void) { }
int HAL_Flash_Init(uint32_t flash) { (void)flash; return 0; }
int HAL_Xip_Init(uint32_t flash, uint32_t xaddr) { (void)flash; (void)xaddr; return 0; }
void platform_cache_init(void) { }

/* ── Minimal test framework ──────────────────────────────────────────── */
static int g_pass = 0;
static int g_fail = 0;

#define CHECK(cond, name) do {                                      \
    if (cond) { printf("PASS  %s\n", name); g_pass++; }             \
    else       { printf("FAIL  %s\n", name); g_fail++; }            \
} while (0)

/* ── Helpers ─────────────────────────────────────────────────────────── */

static void reset_all_mocks(void)
{
    memset(_mock_gpio, 0, sizeof(_mock_gpio));
    _mock_os_time_s = 1000;
    _mock_thread_created = 0;
    memset(&g_refresh_thread, 0, sizeof(g_refresh_thread));

    _mock_http_header_present = 0;
    _mock_http_header_value = "";

    netif_list = NULL;
    _mock_netif_set_addr_called = 0;
    _mock_netif_set_up_called = 0;
    _mock_dhcp_stop_called = 0;

    _mock_image_running_seq = 0;
    _mock_image_check_sections_result = IMAGE_VALID;
    _mock_image_set_cfg_result = 0;
    _mock_image_set_cfg_call_count = 0;
    _mock_image_ota_param = NULL;

    _mock_fdcm_open_fail = 1;
    _mock_fdcm_read_size = 0;
    _mock_fdcm_write_call_count = 0;

    _mock_ota_init_called = 0;
    _mock_ota_get_image_called = 0;
    _mock_ota_verify_image_called = 0;
    _mock_ota_reboot_called = 0;
    _mock_ota_init_result = OTA_STATUS_OK;
    _mock_ota_get_image_result = OTA_STATUS_OK;
    _mock_ota_verify_image_result = OTA_STATUS_OK;

    _mock_wdg_init_called = 0;
    _mock_wdg_start_called = 0;
    _mock_wdg_stop_called = 0;
    _mock_wdg_reboot_called = 0;

    _mock_adc_raw = 0;
    _mock_adc_init_result = HAL_OK;
    _mock_adc_conv_result = HAL_OK;
    g_adc_ready = 0; /* force hokku_battery_mv to re-init the ADC each test */

    _mock_wlan_sta_ap_info_result = 0;
    _mock_wlan_sta_ap_rssi = 0;
    _mock_wlan_sta_config_result = 0;
    _mock_wlan_sta_enable_result = 0;

    memset(&_mock_sysinfo_state, 0, sizeof(_mock_sysinfo_state));
    _mock_sysinfo_get_null = 0;
    _mock_sysinfo_save_result = 0;
    _mock_sysinfo_save_call_count = 0;

    _mock_wakeup_event = 0;
    _mock_wakeup_timer_sec = 0;

    _mock_pm_enter_mode_called = 0;

    hokku_config_load(); /* fdcm_open_fail=1 above -> loads compile-time defaults */
    g_boot_seq = 0;
    g_rollback_armed = 0;
    hlog_reset();
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_xip_offset
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_xip_offset_slot0(void)
{
    CHECK(hokku_xip_offset(0) == 0x13040U, "xip_offset: slot 0 -> 0x13040");
}
static void test_xip_offset_slot1(void)
{
    CHECK(hokku_xip_offset(1) == 0x13040U + 0x179000U,
          "xip_offset: slot 1 -> 0x13040 + XIP_SLOT_STRIDE");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_should_sleep
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_should_sleep_pwr_sleep_always_true(void)
{
    reset_all_mocks();
    hokku_config_get()->power_mode = HOKKU_PWR_SLEEP;
    CHECK(hokku_should_sleep(), "should_sleep: HOKKU_PWR_SLEEP always sleeps");
}
static void test_should_sleep_pwr_awake_always_false(void)
{
    reset_all_mocks();
    hokku_config_get()->power_mode = HOKKU_PWR_AWAKE;
    CHECK(!hokku_should_sleep(), "should_sleep: HOKKU_PWR_AWAKE never sleeps");
}
static void test_should_sleep_auto_sleeps_on_battery(void)
{
    reset_all_mocks();
    hokku_config_get()->power_mode = HOKKU_PWR_AUTO;
    _mock_gpio[GPIO_PORT_A][GPIO_PIN_20] = GPIO_PIN_HIGH; /* PA20 HIGH = no USB */
    CHECK(hokku_should_sleep(), "should_sleep: AUTO sleeps on battery (no USB)");
}
static void test_should_sleep_auto_stays_awake_on_usb(void)
{
    reset_all_mocks();
    hokku_config_get()->power_mode = HOKKU_PWR_AUTO;
    _mock_gpio[GPIO_PORT_A][GPIO_PIN_20] = GPIO_PIN_LOW; /* PA20 LOW = USB present */
    CHECK(!hokku_should_sleep(), "should_sleep: AUTO stays awake on USB");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  read_resp_header_uint / read_resp_header_str
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_read_header_uint_parses_value(void)
{
    reset_all_mocks();
    uint32_t v = 0;
    _mock_http_header_present = 1;
    _mock_http_header_value = "X-Sleep-Seconds: 300";
    CHECK(read_resp_header_uint(0, "X-Sleep-Seconds", &v) == 1 && v == 300,
          "read_resp_header_uint: parses value after the colon");
}
static void test_read_header_uint_absent_returns_zero(void)
{
    reset_all_mocks();
    uint32_t v = 999;
    _mock_http_header_present = 0;
    CHECK(read_resp_header_uint(0, "X-Sleep-Seconds", &v) == 0,
          "read_resp_header_uint: returns 0 (not found) when header is absent");
}
static void test_read_header_str_strips_prefix_and_crlf(void)
{
    reset_all_mocks();
    char out[32];
    _mock_http_header_present = 1;
    _mock_http_header_value = "X-Firmware-Update: 1.2.3\r\n";
    CHECK(read_resp_header_str(0, "X-Firmware-Update", out, sizeof(out)) == 1 &&
          strcmp(out, "1.2.3") == 0,
          "read_resp_header_str: strips 'Name:' prefix and trailing CRLF");
}
static void test_read_header_str_absent_leaves_empty(void)
{
    reset_all_mocks();
    char out[32] = "unchanged";
    _mock_http_header_present = 0;
    CHECK(read_resp_header_str(0, "X-Firmware-Update", out, sizeof(out)) == 0 &&
          out[0] == '\0',
          "read_resp_header_str: returns 0 and empties out[] when absent");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_battery_mv — ADC scaling (mv = raw*295000/1105920*10, clamp 4200,
 *  valid range [3000,4200])
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_battery_mid_range_value(void)
{
    reset_all_mocks();
    _mock_adc_raw = 1500; /* -> exactly 4000 mV pre-clamp, in range */
    CHECK(hokku_battery_mv() == 4000U, "battery_mv: raw=1500 -> 4000 mV");
}
static void test_battery_clamps_to_4200(void)
{
    reset_all_mocks();
    _mock_adc_raw = 4095; /* max 12-bit ADC value -> far above 4200 pre-clamp */
    CHECK(hokku_battery_mv() == 4200U, "battery_mv: high raw clamps to 4200 mV");
}
static void test_battery_below_range_returns_zero(void)
{
    reset_all_mocks();
    _mock_adc_raw = 1000; /* -> 2660 mV, below the 3000 mV plausibility floor */
    CHECK(hokku_battery_mv() == 0U, "battery_mv: implausibly low reading -> 0");
}
static void test_battery_adc_init_failure_returns_zero(void)
{
    reset_all_mocks();
    _mock_adc_init_result = HAL_ERROR;
    _mock_adc_raw = 1500;
    CHECK(hokku_battery_mv() == 0U, "battery_mv: ADC init failure -> 0");
}
static void test_battery_adc_conv_failure_returns_zero(void)
{
    reset_all_mocks();
    _mock_adc_conv_result = HAL_ERROR;
    _mock_adc_raw = 1500;
    CHECK(hokku_battery_mv() == 0U, "battery_mv: ADC conversion failure -> 0");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hlog / hlog_reset — circular log buffer built on the shared logbuf
 *  primitive (firmware/common/all/logbuf.c).
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_hlog_appends_to_ring(void)
{
    reset_all_mocks();
    hlog_reset();
    hlog("hello\n");
    char out[32];
    uint32_t n = logbuf_snapshot(&g_log, out, sizeof(out) - 1);
    out[n] = '\0';
    CHECK(logbuf_len(&g_log) == 6 && strcmp(out, "hello\n") == 0,
          "hlog: appends formatted text to the log buffer");
}
static void test_hlog_reset_clears_length(void)
{
    reset_all_mocks();
    hlog("some text\n");
    hlog_reset();
    CHECK(logbuf_len(&g_log) == 0, "hlog_reset: empties the log buffer");
}
static void test_hlog_evicts_oldest_when_full(void)
{
    reset_all_mocks();
    hlog_reset();
    /* Overfill: each hlog line is capped at ~159 bytes, so >13 lines exceed the
     * 2 KB buffer. Circular must stay bounded AND keep the most-recent line. */
    char filler[180];
    memset(filler, 'x', sizeof(filler));
    filler[sizeof(filler) - 1] = '\0';
    for (int i = 0; i < 20; i++) hlog("%s", filler);
    hlog("TAILMARK\n");
    CHECK(logbuf_len(&g_log) <= HOKKU_LOG_RING_SZ,
          "hlog: stays bounded when full (no overflow)");
    char out[HOKKU_LOG_RING_SZ + 1];
    uint32_t n = logbuf_snapshot(&g_log, out, HOKKU_LOG_RING_SZ);
    out[n] = '\0';
    CHECK(strstr(out, "TAILMARK") != NULL,
          "hlog: circular buffer retains the most-recent line when full");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_build_firmware_url
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_build_firmware_url_normal(void)
{
    reset_all_mocks();
    char out[192];
    strncpy(hokku_config_get()->server_url, "http://host:8080/hokku/screen/",
            HOKKU_URL_MAX - 1);
    hokku_build_firmware_url(out, sizeof(out));
    CHECK(strcmp(out, "http://host:8080/hokku/firmware.bin?model=bigme_f7") == 0,
          "build_firmware_url: derives firmware.bin URL from /hokku/ prefix");
}
static void test_build_firmware_url_fallback_when_no_hokku_prefix(void)
{
    reset_all_mocks();
    char out[192];
    strncpy(hokku_config_get()->server_url, "http://unusual-host/x/y",
            HOKKU_URL_MAX - 1);
    hokku_build_firmware_url(out, sizeof(out));
    CHECK(strcmp(out, "http://unusual-host/x/y") == 0,
          "build_firmware_url: falls back to the raw server_url when unrecognised");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_rollback_arm / hokku_rollback_commit — the brick-prevention logic
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_rollback_arm_skips_when_fallback_invalid(void)
{
    reset_all_mocks();
    _mock_image_check_sections_result = IMAGE_INVALID;
    hokku_rollback_arm();
    CHECK(!g_rollback_armed && _mock_image_set_cfg_call_count == 0 && !_mock_wdg_start_called,
          "rollback_arm: skips entirely when the fallback slot isn't valid");
}
static void test_rollback_arm_does_not_start_wdg_when_set_cfg_fails(void)
{
    reset_all_mocks();
    _mock_image_check_sections_result = IMAGE_VALID;
    _mock_image_set_cfg_result = -1;
    hokku_rollback_arm();
    CHECK(!g_rollback_armed && !_mock_wdg_start_called,
          "rollback_arm: does not start the WDG if repointing the cfg failed");
}
static void test_rollback_arm_succeeds_and_arms_wdg(void)
{
    reset_all_mocks();
    _mock_image_running_seq = 0;
    _mock_image_check_sections_result = IMAGE_VALID;
    _mock_image_set_cfg_result = 0;
    hokku_rollback_arm();
    CHECK(g_rollback_armed == 1, "rollback_arm: g_rollback_armed set on success");
    CHECK(_mock_wdg_init_called == 1 && _mock_wdg_start_called == 1,
          "rollback_arm: WDG initialised and started on success");
    CHECK(_mock_image_set_cfg_last.seq == 1 &&
          _mock_image_set_cfg_last.state == IMAGE_STATE_VERIFIED,
          "rollback_arm: cfg repointed at the OTHER (fallback) slot, VERIFIED");
}
static void test_rollback_commit_noop_when_not_armed(void)
{
    reset_all_mocks();
    g_rollback_armed = 0;
    hokku_rollback_commit();
    CHECK(_mock_image_set_cfg_call_count == 0 && !_mock_wdg_stop_called,
          "rollback_commit: no-op when never armed");
}
static void test_rollback_commit_stops_wdg_on_success(void)
{
    reset_all_mocks();
    g_boot_seq = 0;
    g_rollback_armed = 1;
    _mock_image_set_cfg_result = 0;
    hokku_rollback_commit();
    CHECK(!g_rollback_armed, "rollback_commit: disarms on success");
    CHECK(_mock_wdg_stop_called == 1, "rollback_commit: stops the WDG on success");
    CHECK(_mock_image_set_cfg_last.seq == 0 &&
          _mock_image_set_cfg_last.state == IMAGE_STATE_VERIFIED,
          "rollback_commit: cfg repointed back at our OWN (booted) slot");
}
static void test_rollback_commit_leaves_wdg_running_on_failure(void)
{
    reset_all_mocks();
    g_rollback_armed = 1;
    _mock_image_set_cfg_result = -1;
    hokku_rollback_commit();
    CHECK(g_rollback_armed == 1,
          "rollback_commit: stays armed if the commit write failed");
    CHECK(!_mock_wdg_stop_called,
          "rollback_commit: leaves the WDG running if the commit write failed "
          "(better to roll back than adopt an unconfirmed image)");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_do_ota — erase-range safety guard (must allow BOTH A/B directions,
 *  reject anything that would touch the bootloader or run past the config
 *  partition at 0x300000)
 * ═══════════════════════════════════════════════════════════════════════ */

static image_ota_param_t g_test_iop;

static void test_ota_guard_rejects_when_no_ota_param(void)
{
    reset_all_mocks();
    _mock_image_ota_param = NULL;
    hokku_do_ota("1.0");
    CHECK(_mock_ota_init_called == 0, "ota_guard: refuses when image_get_ota_param() is NULL");
}
static void test_ota_guard_rejects_write_below_bootloader(void)
{
    reset_all_mocks();
    memset(&g_test_iop, 0, sizeof(g_test_iop));
    g_test_iop.bl_size = 0x8000;
    g_test_iop.addr[1] = 0x1000; /* below bl_size -> would erase the bootloader */
    g_test_iop.img_max_size = 100;
    _mock_image_running_seq = 0; /* upd = 1 */
    _mock_image_ota_param = &g_test_iop;
    hokku_do_ota("1.0");
    CHECK(_mock_ota_init_called == 0,
          "ota_guard: refuses a write target below the bootloader");
}
static void test_ota_guard_rejects_write_past_config_partition(void)
{
    reset_all_mocks();
    memset(&g_test_iop, 0, sizeof(g_test_iop));
    g_test_iop.bl_size = 0x8000;
    g_test_iop.addr[1] = 0x181000;
    g_test_iop.img_max_size = 2000; /* 2000*1024 pushes the end past 0x300000 */
    _mock_image_running_seq = 0;
    _mock_image_ota_param = &g_test_iop;
    hokku_do_ota("1.0");
    CHECK(_mock_ota_init_called == 0,
          "ota_guard: refuses a write whose end runs into the config partition");
}
static void test_ota_guard_allows_seq0_to_seq1_direction(void)
{
    reset_all_mocks();
    memset(&g_test_iop, 0, sizeof(g_test_iop));
    g_test_iop.bl_size = 0x8000;
    g_test_iop.addr[1] = 0x181000;
    g_test_iop.img_max_size = 1500; /* end = 0x181000 + 1500*1024 = 0x2F8000, safe */
    _mock_image_running_seq = 0; /* running seq0 -> writes slot1 */
    _mock_image_ota_param = &g_test_iop;
    _mock_ota_init_result = OTA_STATUS_ERROR; /* stop right after the guard passes */
    hokku_do_ota("1.0");
    CHECK(_mock_ota_init_called == 1,
          "ota_guard: allows the seq0->seq1 direction (running seq0 writes slot1)");
}
static void test_ota_guard_allows_seq1_to_seq0_direction(void)
{
    reset_all_mocks();
    memset(&g_test_iop, 0, sizeof(g_test_iop));
    g_test_iop.bl_size = 0x8000;
    g_test_iop.addr[0] = 0x8000; /* == bl_size exactly: boundary must be allowed */
    g_test_iop.img_max_size = 1500;
    _mock_image_running_seq = 1; /* running seq1 -> writes slot0 */
    _mock_image_ota_param = &g_test_iop;
    _mock_ota_init_result = OTA_STATUS_ERROR;
    hokku_do_ota("1.0");
    CHECK(_mock_ota_init_called == 1,
          "ota_guard: allows the seq1->seq0 direction, addr==bl_size boundary included");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_wifi_provision
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_wifi_provision_fails_when_sysinfo_unavailable(void)
{
    reset_all_mocks();
    _mock_sysinfo_get_null = 1;
    CHECK(hokku_wifi_provision("MyNet", "password1") == -1,
          "wifi_provision: fails when sysinfo is unavailable");
}
static void test_wifi_provision_rejects_empty_ssid(void)
{
    reset_all_mocks();
    CHECK(hokku_wifi_provision("", "password1") == -1,
          "wifi_provision: rejects an empty SSID");
}
static void test_wifi_provision_rejects_oversized_psk(void)
{
    reset_all_mocks();
    char big_psk[80];
    memset(big_psk, 'x', sizeof(big_psk) - 1);
    big_psk[sizeof(big_psk) - 1] = '\0';
    CHECK(hokku_wifi_provision("MyNet", big_psk) == -1,
          "wifi_provision: rejects a psk >= SYSINFO_PSK_LEN_MAX");
}
static void test_wifi_provision_persists_creds_on_success(void)
{
    reset_all_mocks();
    hokku_wifi_provision("MyNet", "password1");
    CHECK(_mock_sysinfo_state.wlan_sta_param.ssid_len == 5 &&
          memcmp(_mock_sysinfo_state.wlan_sta_param.ssid, "MyNet", 5) == 0,
          "wifi_provision: persists the SSID to sysinfo");
    CHECK(memcmp(_mock_sysinfo_state.wlan_sta_param.psk, "password1", 9) == 0,
          "wifi_provision: persists the password to sysinfo");
    CHECK(_mock_sysinfo_save_call_count == 1,
          "wifi_provision: calls sysinfo_save() exactly once");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  hokku_hibernate — sleep_s clamping (5..60000)
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_hibernate_clamps_low_sleep(void)
{
    reset_all_mocks();
    hokku_hibernate(1);
    CHECK(_mock_wakeup_timer_sec == 5, "hibernate: clamps sleep_s below 5 up to 5");
}
static void test_hibernate_clamps_high_sleep(void)
{
    reset_all_mocks();
    hokku_hibernate(999999);
    CHECK(_mock_wakeup_timer_sec == 60000,
          "hibernate: clamps sleep_s above 60000 down to 60000");
}
static void test_hibernate_passes_through_normal_value(void)
{
    reset_all_mocks();
    hokku_hibernate(300);
    CHECK(_mock_wakeup_timer_sec == 300, "hibernate: passes an in-range sleep_s through unchanged");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  net_cb — WLAN_CONNECTED static-IP/DHCP branching + NETWORK_UP thread start
 * ═══════════════════════════════════════════════════════════════════════ */

static struct netif g_test_netif;

static void test_net_cb_wlan_connected_no_netif_does_not_crash(void)
{
    reset_all_mocks();
    netif_list = NULL;
    net_cb(NET_CTRL_MSG_WLAN_CONNECTED, 0, NULL);
    CHECK(!_mock_netif_set_addr_called,
          "net_cb: WLAN_CONNECTED with no netif yet does nothing (no crash)");
}
static void test_net_cb_wlan_connected_dhcp_leaves_sdk_dhcp_running(void)
{
    reset_all_mocks();
    memset(&g_test_netif, 0, sizeof(g_test_netif));
    netif_list = &g_test_netif;
    hokku_config_get()->use_dhcp = 1;
    net_cb(NET_CTRL_MSG_WLAN_CONNECTED, 0, NULL);
    CHECK(!_mock_netif_set_addr_called && !_mock_dhcp_stop_called,
          "net_cb: DHCP mode does not touch the netif (leaves SDK DHCP running)");
}
static void test_net_cb_wlan_connected_static_ip_sets_address(void)
{
    reset_all_mocks();
    memset(&g_test_netif, 0, sizeof(g_test_netif));
    netif_list = &g_test_netif;
    hokku_config_get()->use_dhcp = 0;
    strncpy(hokku_config_get()->ip, "192.168.6.199", HOKKU_IP_MAX - 1);
    strncpy(hokku_config_get()->gw, "192.168.6.254", HOKKU_IP_MAX - 1);
    strncpy(hokku_config_get()->nm, "255.255.255.0", HOKKU_IP_MAX - 1);
    net_cb(NET_CTRL_MSG_WLAN_CONNECTED, 0, NULL);
    /* This is the exact fix from this session: a bare netif_set_up() is a
     * no-op on lwIP 2.x once the SDK has already brought the interface up
     * (it starts DHCP on link-up) — only netif_set_addr() fires the status
     * callback the SDK maps to NETWORK_UP. Regression-guards that call. */
    CHECK(_mock_dhcp_stop_called, "net_cb: static IP stops the SDK's DHCP client");
    CHECK(_mock_netif_set_addr_called,
          "net_cb: static IP calls netif_set_addr() (NOT just netif_set_up())");
    CHECK(strcmp(_mock_netif_set_addr_ip.text, "192.168.6.199") == 0,
          "net_cb: netif_set_addr() called with the configured IP");
    CHECK(_mock_netif_set_up_called, "net_cb: static IP also brings the netif up");
}
static void test_net_cb_wlan_connected_bad_static_ip_leaves_dhcp(void)
{
    reset_all_mocks();
    memset(&g_test_netif, 0, sizeof(g_test_netif));
    netif_list = &g_test_netif;
    hokku_config_get()->use_dhcp = 0;
    hokku_config_get()->ip[0] = '\0'; /* unparseable -> ipaddr_aton fails */
    net_cb(NET_CTRL_MSG_WLAN_CONNECTED, 0, NULL);
    CHECK(!_mock_netif_set_addr_called,
          "net_cb: an unparseable static IP leaves DHCP running rather than crash");
}
static void test_net_cb_network_up_starts_refresh_thread_once(void)
{
    reset_all_mocks();
    net_cb(NET_CTRL_MSG_NETWORK_UP, 0, NULL);
    CHECK(_mock_thread_created == 1, "net_cb: NETWORK_UP starts the refresh thread");
    net_cb(NET_CTRL_MSG_NETWORK_UP, 0, NULL); /* a second NETWORK_UP (e.g. reconnect) */
    CHECK(_mock_thread_created == 1,
          "net_cb: a second NETWORK_UP does not start a duplicate thread");
}
static void test_net_cb_network_down_does_not_crash(void)
{
    reset_all_mocks();
    net_cb(NET_CTRL_MSG_NETWORK_DOWN, 0, NULL); /* just logs; nothing to assert beyond no crash */
    CHECK(1, "net_cb: NETWORK_DOWN handled without crashing");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  Entry point
 * ═══════════════════════════════════════════════════════════════════════ */

int main(void)
{
    printf("=== test_logic (bigme_f7) ===\n\n");

    test_xip_offset_slot0();
    test_xip_offset_slot1();

    test_should_sleep_pwr_sleep_always_true();
    test_should_sleep_pwr_awake_always_false();
    test_should_sleep_auto_sleeps_on_battery();
    test_should_sleep_auto_stays_awake_on_usb();

    test_read_header_uint_parses_value();
    test_read_header_uint_absent_returns_zero();
    test_read_header_str_strips_prefix_and_crlf();
    test_read_header_str_absent_leaves_empty();

    test_battery_mid_range_value();
    test_battery_clamps_to_4200();
    test_battery_below_range_returns_zero();
    test_battery_adc_init_failure_returns_zero();
    test_battery_adc_conv_failure_returns_zero();

    test_hlog_appends_to_ring();
    test_hlog_reset_clears_length();
    test_hlog_evicts_oldest_when_full();

    test_build_firmware_url_normal();
    test_build_firmware_url_fallback_when_no_hokku_prefix();

    test_rollback_arm_skips_when_fallback_invalid();
    test_rollback_arm_does_not_start_wdg_when_set_cfg_fails();
    test_rollback_arm_succeeds_and_arms_wdg();
    test_rollback_commit_noop_when_not_armed();
    test_rollback_commit_stops_wdg_on_success();
    test_rollback_commit_leaves_wdg_running_on_failure();

    test_ota_guard_rejects_when_no_ota_param();
    test_ota_guard_rejects_write_below_bootloader();
    test_ota_guard_rejects_write_past_config_partition();
    test_ota_guard_allows_seq0_to_seq1_direction();
    test_ota_guard_allows_seq1_to_seq0_direction();

    test_wifi_provision_fails_when_sysinfo_unavailable();
    test_wifi_provision_rejects_empty_ssid();
    test_wifi_provision_rejects_oversized_psk();
    test_wifi_provision_persists_creds_on_success();

    test_hibernate_clamps_low_sleep();
    test_hibernate_clamps_high_sleep();
    test_hibernate_passes_through_normal_value();

    test_net_cb_wlan_connected_no_netif_does_not_crash();
    test_net_cb_wlan_connected_dhcp_leaves_sdk_dhcp_running();
    test_net_cb_wlan_connected_static_ip_sets_address();
    test_net_cb_wlan_connected_bad_static_ip_leaves_dhcp();
    test_net_cb_network_up_starts_refresh_thread_once();
    test_net_cb_network_down_does_not_crash();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
