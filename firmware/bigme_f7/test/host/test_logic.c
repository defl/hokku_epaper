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
 *   - refresh_thread_fn(): the top-level loop (sleep/hibernate per outcome).
 *     do_refresh() IS covered for how it acts on each server reply (shared
 *     fetch_outcome decision) against the controllable HTTPC mock.
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
#include "mocks/lwip/netifapi.h"
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

#include "../../../common/xr872/hokku_config.c"
#include "../../led.c"    /* led_usb_present() -> _mock_gpio, shared with main.c below */
#include "../../../common/all/firmware_url.c"  /* SoC-agnostic (shared with ESP32) */
#include "../../../common/all/backoff.c"       /* SoC-agnostic (shared with ESP32) */
#include "../../../common/all/fetch_outcome.c"  /* shared reply decision */
#include "../../../common/all/sleep_cal.c"      /* drift calibration math */
#include "../../../common/all/schedule.c"       /* next fetch + drift (shared) */
#include "../../../common/all/messages.c"       /* on-glass texts (shared) */
#include "../../../common/all/ota_confirm.c"    /* new-firmware policy (shared) */
#include "../../../common/all/text_render.c"    /* (epd.c's renderer; linked for completeness) */
#include "../../../common/all/frame_state.c"   /* SoC-agnostic (shared with ESP32) */
#include "../../../common/all/frame_proto.c"   /* SoC-agnostic (shared with ESP32) */
#include "../../../common/all/screen_ident.c"  /* SoC-agnostic (shared with ESP32) */
#include "../../../common/all/interactive.c"   /* SoC-agnostic (shared with ESP32) */
#include "../../../common/all/logbuf.c"        /* SoC-agnostic (shared with ESP32) */
/* Shared XR872 code (firmware/common/xr872) — included before main.c so its
 * (now non-static) symbols are defined when main.c references them. */
#include "../../../common/xr872/log.c"         /* hlog + POST-body accessors */
#include "../../../common/xr872/clock.c"       /* hokku_clock_set/now */
#include "../../../common/xr872/http_util.c"   /* read_resp_header_uint/str */
#include "../../../common/xr872/pm.c"          /* hokku_hibernate */
#include "../../../common/xr872/state.c"       /* persistent runtime state */
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
static int _mock_epd_refresh_calls;
void epd_refresh(void) { _mock_epd_refresh_calls++; }
static int  _mock_epd_text_calls;
static char _mock_epd_text[HOKKU_MSG_MAX];
void epd_show_text(const char *msg)
{
    _mock_epd_text_calls++;
    strncpy(_mock_epd_text, msg, sizeof(_mock_epd_text) - 1);
    _mock_epd_text[sizeof(_mock_epd_text) - 1] = '\0';
}
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
    memset(&g_refresh_kick, 0, sizeof(g_refresh_kick));
    _mock_sem_count = 0;
    _mock_sem_release_calls = 0;
    _mock_sem_last_wait_ms = 0;

    _mock_http_header_present = 0;
    _mock_http_header_value = "";
    _mock_httpc_open_result = 1;     /* transport down unless a test brings it up */
    _mock_httpc_request_result = 0;
    _mock_httpc_info_result = 0;
    _mock_httpc_status = 0;
    _mock_httpc_body_len = 0;
    _mock_httpc_body_read = 0;
    _mock_epd_refresh_calls = 0;
    _mock_epd_text_calls = 0;
    _mock_epd_text[0] = '\0';
    _mock_httpc_header_count = 0;

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

    _mock_uart_avail = 0;
    _mock_uart_delivered = 0;
    _mock_uart_polls = 0;
    _mock_console_disable_called = 0;
    _mock_console_enable_called = 0;
    _mock_console_written_len = 0;

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
    memset(_mock_wlan_calls, 0, sizeof(_mock_wlan_calls));
    _mock_wlan_call_count = 0;
    memset(_mock_wlan_config_ssid, 0, sizeof(_mock_wlan_config_ssid));
    g_wlan_netif = NULL;
    _mock_net_ip4_valid = 0;

    memset(&_mock_sysinfo_state, 0, sizeof(_mock_sysinfo_state));
    _mock_sysinfo_get_null = 0;
    _mock_sysinfo_save_result = 0;
    _mock_sysinfo_save_call_count = 0;

    _mock_wakeup_event = 0;
    _mock_wakeup_timer_sec = 0;

    _mock_pm_enter_mode_called = 0;

    hokku_config_load(); /* fdcm_open_fail=1 above -> loads compile-time defaults */
    hokku_state_load();  /* likewise -> zero state */
    g_boot_seq = 0;
    g_rollback_armed = 0;
    g_ota_pending = 0;
    g_net_up = 0;
    g_timer_wake = 0;
    g_epd_ready = 1;
    g_clk_epoch_base = 0;
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

/* USB-interactive mode outranks the configured power mode, because it is an
 * instruction about right now rather than a standing preference. Hibernating
 * closes the console, which is precisely what the host asked us not to do. */
static void test_should_sleep_interactive_overrides_pwr_sleep(void)
{
    reset_all_mocks();
    hokku_config_get()->power_mode = HOKKU_PWR_SLEEP;
    _mock_gpio[GPIO_PORT_A][GPIO_PIN_20] = GPIO_PIN_LOW; /* USB present */
    hokku_interactive_set(true);
    CHECK(!hokku_should_sleep(),
          "should_sleep: interactive beats PWR_SLEEP while on USB");
    hokku_interactive_set(false);
}

/* The safety rule, at the point where it actually protects something: the mode
 * is set and the cable is pulled. If interactive still suppressed sleep here the
 * screen would sit awake on battery until it was flat. */
static void test_should_sleep_interactive_inert_on_battery(void)
{
    reset_all_mocks();
    hokku_config_get()->power_mode = HOKKU_PWR_SLEEP;
    _mock_gpio[GPIO_PORT_A][GPIO_PIN_20] = GPIO_PIN_HIGH; /* no USB */
    hokku_interactive_set(true);
    CHECK(hokku_should_sleep(),
          "should_sleep: interactive is inert on battery — sleeps as configured");
    hokku_interactive_set(false);
}

/* And it must not leak: clearing the mode restores the configured behaviour
 * exactly, or a campaign would leave the screen permanently awake. */
static void test_should_sleep_interactive_cleared_restores_config(void)
{
    reset_all_mocks();
    hokku_config_get()->power_mode = HOKKU_PWR_SLEEP;
    _mock_gpio[GPIO_PORT_A][GPIO_PIN_20] = GPIO_PIN_LOW; /* USB present */
    hokku_interactive_set(true);
    hokku_interactive_set(false);
    CHECK(hokku_should_sleep(), "should_sleep: config honoured again once cleared");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  read_resp_header_uint / read_resp_header_str
 * ═══════════════════════════════════════════════════════════════════════ */

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
    _mock_adc_raw = 1000; /* -> 2660 mV: reported; frame_state judges plausibility */
    CHECK(hokku_battery_mv() == 2660U, "battery_mv: low reading reported as read");
    _mock_adc_raw = 500;  /* -> 1330 mV: a floating ADC, not a battery */
    char fs[384];
    build_frame_state(fs, sizeof(fs));
    CHECK(strstr(fs, "bat_mv") == NULL,
          "battery_mv: an implausible reading is left out of X-Frame-State (shared rule)");
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
    CHECK(logbuf_len(&g_log) <= HOKKU_XR872_LOG_RING_SZ,
          "hlog: stays bounded when full (no overflow)");
    char out[HOKKU_XR872_LOG_RING_SZ + 1];
    uint32_t n = logbuf_snapshot(&g_log, out, HOKKU_XR872_LOG_RING_SZ);
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
/* Issue #44 regression: `wifi <ssid> <pw>` while already associated must
 * disable the station before reconfiguring it, then re-enable. Config-then-
 * enable on a running station left the unit on the old AP, no longer checking in. */
static void test_wifi_provision_live_switch_disables_config_enables(void)
{
    reset_all_mocks();
    static struct netif live_netif;
    memcpy(_mock_sysinfo_state.wlan_sta_param.ssid, "MMIOT", 5); /* currently joined */
    _mock_sysinfo_state.wlan_sta_param.ssid_len = 5;
    g_wlan_netif = &live_netif;
    _mock_net_ip4_valid = 1;                                      /* holding a lease */

    CHECK(hokku_wifi_provision("McMansion", "password1") == 0,
          "wifi_provision: live switch succeeds");
    /* Without the address drop the SDK reconnects with "netif is already up":
     * no DHCP, no NETWORK_UP (seen on hardware with 1.2.14 before this). */
    CHECK(_mock_wlan_call_count == 5 &&
          _mock_wlan_calls[0] == MOCK_NET_CONFIG_DOWN &&
          _mock_wlan_calls[1] == MOCK_NETIF_CLEAR_ADDR &&
          _mock_wlan_calls[2] == MOCK_WLAN_DISABLE &&
          _mock_wlan_calls[3] == MOCK_WLAN_CONFIG &&
          _mock_wlan_calls[4] == MOCK_WLAN_ENABLE,
          "wifi_provision: drops the address, then disable -> config -> enable");
    CHECK(strcmp((const char *)_mock_wlan_config_ssid, "McMansion") == 0,
          "wifi_provision: configures the NEW ssid");
}
static void test_wifi_provision_config_failure_reenables_station(void)
{
    reset_all_mocks();
    _mock_wlan_sta_config_result = -1;
    CHECK(hokku_wifi_provision("McMansion", "password1") == -1,
          "wifi_provision: reports a wlan_sta_config failure");
    CHECK(_mock_wlan_call_count == 3 &&
          _mock_wlan_calls[0] == MOCK_WLAN_DISABLE &&
          _mock_wlan_calls[1] == MOCK_WLAN_CONFIG &&
          _mock_wlan_calls[2] == MOCK_WLAN_ENABLE,
          "wifi_provision: a failed config still re-enables the station (radio not left off)");
}
static void test_wifi_connect_saved_uses_same_sequence(void)
{
    reset_all_mocks();
    memcpy(_mock_sysinfo_state.wlan_sta_param.ssid, "McMansion", 9);
    _mock_sysinfo_state.wlan_sta_param.ssid_len = 9;
    hokku_wifi_connect_saved();
    CHECK(_mock_wlan_call_count == 3 &&
          _mock_wlan_calls[0] == MOCK_WLAN_DISABLE &&
          _mock_wlan_calls[1] == MOCK_WLAN_CONFIG &&
          _mock_wlan_calls[2] == MOCK_WLAN_ENABLE,
          "wifi_connect_saved: boot connect uses disable -> config -> enable");
}
static void test_wifi_provision_rejected_input_leaves_station_alone(void)
{
    reset_all_mocks();
    hokku_wifi_provision("", "password1");
    CHECK(_mock_wlan_call_count == 0,
          "wifi_provision: rejected input never touches the running station");
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
static void test_net_cb_network_up_marks_network(void)
{
    reset_all_mocks();
    net_cb(NET_CTRL_MSG_NETWORK_UP, 0, NULL);
    CHECK(g_net_up && _mock_thread_created == 0,
          "net_cb: NETWORK_UP marks the network up (main starts the thread)");
    net_cb(NET_CTRL_MSG_NETWORK_DOWN, 0, NULL);
    CHECK(!g_net_up, "net_cb: NETWORK_DOWN marks it down");
}
/* Issue #44: after a `wifi` switch the refresh thread was mid-way through a
 * server-given sleep (can be ~9 h overnight) and did not check in until it ended. */
static void test_net_cb_network_up_kicks_running_refresh_thread(void)
{
    reset_all_mocks();
    OS_SemaphoreCreateBinary(&g_refresh_kick);
    net_cb(NET_CTRL_MSG_NETWORK_UP, 0, NULL);   /* no thread yet: nothing to kick */
    CHECK(_mock_sem_release_calls == 0,
          "net_cb: NETWORK_UP before the thread exists kicks nothing");
    g_refresh_thread.handle = (OS_Handle_t)1;
    net_cb(NET_CTRL_MSG_NETWORK_UP, 0, NULL);   /* up again after a switch */
    CHECK(_mock_sem_release_calls == 1,
          "net_cb: NETWORK_UP with the thread running kicks the refresh wait");
}
static void test_refresh_wait_returns_early_when_kicked(void)
{
    reset_all_mocks();
    OS_SemaphoreCreateBinary(&g_refresh_kick);
    OS_SemaphoreRelease(&g_refresh_kick);
    hokku_refresh_wait(33092U * 1000U);
    CHECK(_mock_sem_last_wait_ms == 33092U * 1000U,
          "refresh_wait: waits on the kick with the server-given sleep as timeout");
    CHECK(_mock_sem_count == 0, "refresh_wait: consumes the kick");
}
static void test_refresh_wait_without_semaphore_falls_back_to_sleep(void)
{
    reset_all_mocks();   /* g_refresh_kick invalid */
    hokku_refresh_wait(1000);
    CHECK(_mock_sem_last_wait_ms == 0,
          "refresh_wait: no semaphore -> plain sleep, never waits on an invalid handle");
}
static void test_net_cb_network_down_does_not_crash(void)
{
    reset_all_mocks();
    net_cb(NET_CTRL_MSG_NETWORK_DOWN, 0, NULL); /* just logs; nothing to assert beyond no crash */
    CHECK(1, "net_cb: NETWORK_DOWN handled without crashing");
}

/* ── hokku_frame_receive: the console handover ────────────────────────────
 *
 * The frame upload borrows the UART from the console for the length of a
 * transfer. The property worth pinning is that the borrow is always balanced:
 * every path out of hokku_frame_receive() must re-enable the console. If one
 * does not, the device is left with no console — and on real hardware that
 * means no way back in short of a USB replug, during a routine that exists to
 * be run repeatedly during colour measurement. */

static void test_frame_receive_acks_every_chunk(void)
{
    uint32_t expect_chunks = (EPD_IMAGE_BYTES + FRAME_PROTO_CHUNK_BYTES - 1)
                             / FRAME_PROTO_CHUNK_BYTES;

    reset_all_mocks();
    OS_MutexCreate(&g_ota_lock);
    _mock_uart_avail = EPD_IMAGE_BYTES;

    CHECK(hokku_frame_receive() == 0, "frame: complete transfer succeeds");
    CHECK(_mock_uart_delivered == EPD_IMAGE_BYTES, "frame: consumes the whole image");
    CHECK(_mock_console_written_len == expect_chunks,
          "frame: one ACK per chunk");
    CHECK(_mock_console_written[0] == FRAME_PROTO_ACK, "frame: ACK byte is 'K'");
    CHECK(_mock_console_disable_called == 1 && _mock_console_enable_called == 1,
          "frame: console handover is balanced on success");
}

static void test_frame_receive_restores_console_when_host_dies(void)
{
    reset_all_mocks();
    OS_MutexCreate(&g_ota_lock);
    _mock_uart_avail = FRAME_PROTO_CHUNK_BYTES + 10; /* one chunk, then silence */

    CHECK(hokku_frame_receive() != 0, "frame: truncated transfer reports failure");
    CHECK(_mock_console_enable_called == 1,
          "frame: console restored even when the host vanishes mid-transfer");
    CHECK(_mock_console_disable_called == 1, "frame: console disabled exactly once");
}

static void test_frame_receive_refuses_while_ota_lock_held(void)
{
    reset_all_mocks();
    memset(&g_ota_lock, 0, sizeof(g_ota_lock)); /* invalid: never created */
    _mock_uart_avail = EPD_IMAGE_BYTES;

    CHECK(hokku_frame_receive() != 0, "frame: refused when the OTA lock is unavailable");
    CHECK(_mock_console_disable_called == 0,
          "frame: console untouched when the transfer never starts");
    CHECK(_mock_uart_polls == 0, "frame: UART untouched when the transfer never starts");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  Entry point
 * ═══════════════════════════════════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════════════════════════
 *  refresh_once — one fetch acted on through the shared decision, schedule
 *  and messages, exactly like the ESP32 boards
 * ═══════════════════════════════════════════════════════════════════════ */

#define T0 1700000000LL

static void mock_server_reply(UINT32 status, const char *header, UINT32 body_len)
{
    g_net_up = 1;
    _mock_httpc_open_result = 0;
    _mock_httpc_status = status;
    _mock_http_header_present = header != NULL;
    _mock_http_header_value = header ? header : "";
    _mock_httpc_body_len = body_len;
    _mock_httpc_body_read = 0;
}

static hokku_sched_t *sched(void) { return &hokku_state_get()->sched; }

static void test_refresh_404_label_filter_keeps_picture(void)
{
    reset_all_mocks();
    sched()->failures = 2;
    mock_server_reply(404, "X-Sleep-Seconds: 21600", 0);
    hokku_fetch_action_t a = refresh_once();
    CHECK(a == HOKKU_FETCH_KEEP && sched()->sleep_s == 21600 && sched()->failures == 0 &&
          _mock_epd_refresh_calls == 0 && _mock_epd_text_calls == 0,
          "refresh: 404 + X-Sleep-Seconds keeps the picture, draws nothing, sleeps the interval");
}

static void test_refresh_503_busy_honours_header(void)
{
    reset_all_mocks();
    mock_server_reply(503, "X-Sleep-Seconds: 45", 0);
    hokku_fetch_action_t a = refresh_once();
    CHECK(a == HOKKU_FETCH_KEEP && sched()->sleep_s == 45 && _mock_epd_refresh_calls == 0,
          "refresh: 503 busy sleeps X-Sleep-Seconds");
}

static void test_refresh_error_without_header_backs_off(void)
{
    reset_all_mocks();
    sched()->failures = 1;
    mock_server_reply(500, NULL, 0);
    hokku_fetch_action_t a = refresh_once();
    CHECK(a == HOKKU_FETCH_BACKOFF && sched()->failures == 2 &&
          sched()->sleep_s == 2 * HOKKU_RETRY_BASE_S && _mock_epd_text_calls == 0,
          "refresh: error without X-Sleep-Seconds backs off; a later failure draws nothing");
}

static void test_refresh_first_outage_draws_shared_message(void)
{
    reset_all_mocks();
    g_net_up = 1;                       /* HTTPC_open fails: server unreachable */
    hokku_fetch_action_t a = refresh_once();
    CHECK(a == HOKKU_FETCH_BACKOFF && sched()->failures == 1 &&
          sched()->sleep_s == HOKKU_RETRY_BASE_S,
          "refresh: no connection backs off from the shared base");
    CHECK(_mock_epd_text_calls == 1 && strstr(_mock_epd_text, "Image download failed.") &&
          strstr(_mock_epd_text, hokku_config_get()->server_url) &&
          strstr(_mock_epd_text, HOKKU_RETRY_HINT),
          "refresh: the first outage draws the shared message with the F7's retry hint");
}

static void test_refresh_wifi_failure_draws_wifi_message(void)
{
    reset_all_mocks();                  /* network never comes up */
    hokku_fetch_action_t a = refresh_once();
    CHECK(a == HOKKU_FETCH_BACKOFF && _mock_epd_text_calls == 1 &&
          strstr(_mock_epd_text, "WiFi connect failed."),
          "refresh: no network is an outage that draws the WiFi message");
}

static void test_refresh_image_displays(void)
{
    reset_all_mocks();
    sched()->failures = 3;
    mock_server_reply(200, "X-Sleep-Seconds: 3600", EPD_IMAGE_BYTES);
    hokku_fetch_action_t a = refresh_once();
    CHECK(a == HOKKU_FETCH_DISPLAY && sched()->sleep_s == 3600 && sched()->failures == 0 &&
          _mock_epd_refresh_calls == 1,
          "refresh: full image is refreshed onto the panel, sleeps X-Sleep-Seconds");
}

static void test_refresh_short_image_not_displayed(void)
{
    reset_all_mocks();
    mock_server_reply(200, "X-Sleep-Seconds: 3600", EPD_IMAGE_BYTES / 2);
    hokku_fetch_action_t a = refresh_once();
    CHECK(a == HOKKU_FETCH_BACKOFF && _mock_epd_refresh_calls == 0,
          "refresh: short image is not refreshed and backs off");
}

static void test_refresh_long_image_not_displayed(void)
{
    reset_all_mocks();
    mock_server_reply(200, "X-Sleep-Seconds: 3600", EPD_IMAGE_BYTES + 100);
    hokku_fetch_action_t a = refresh_once();
    CHECK(a == HOKKU_FETCH_BACKOFF && _mock_epd_refresh_calls == 0,
          "refresh: an over-long body is not an image either (exact size, like ESP32)");
}

static void test_refresh_anchors_to_server_clock(void)
{
    reset_all_mocks();
    mock_server_reply(404, "X-Sleep-Seconds: 600", 0);
    _mock_httpc_headers[0] = "X-Server-Time-Epoch: 1700000000";
    _mock_httpc_header_count = 1;
    refresh_once();
    CHECK(hokku_clock_now() == (uint32_t)T0,
          "refresh: any reply carrying the server time sets the clock (not just a 200)");
    CHECK(sched()->next_epoch == T0 + 600,
          "refresh: next fetch anchored to the server clock");
}

static void test_refresh_learns_drift_and_seed(void)
{
    reset_all_mocks();
    g_timer_wake = 1;
    sched()->sleep_start_epoch = T0;
    sched()->armed_s = 43200;
    sched()->next_epoch = T0 + 43200;
    _mock_os_time_s = 20;               /* the reply arrives 20 s after the wake */
    mock_server_reply(200, "X-Sleep-Seconds: 3600", EPD_IMAGE_BYTES);
    _mock_httpc_headers[0] = "X-Server-Time-Epoch: 1700043652";   /* T0 + 43200 + 432 + 20 */
    _mock_httpc_headers[1] = "X-Sleep-Cal-PPM: 5000";
    _mock_httpc_headers[2] = "X-Sleep-Cal-N: 9";
    _mock_httpc_header_count = 3;
    refresh_once();
    CHECK(sched()->cal_samples == 1 && sched()->cal_ppm == 10000,
          "refresh: a timer wake learns the drift from the server clock (seed not needed)");
    CHECK(sched()->sleep_err_known && sched()->sleep_err_s == 432,
          "refresh: reports how late the wake landed");

    reset_all_mocks();
    mock_server_reply(200, "X-Sleep-Seconds: 3600", EPD_IMAGE_BYTES);
    _mock_httpc_headers[0] = "X-Sleep-Cal-PPM: 5000";
    _mock_httpc_headers[1] = "X-Sleep-Cal-N: 9";
    _mock_httpc_header_count = 2;
    refresh_once();
    CHECK(sched()->cal_samples == 1 && sched()->cal_ppm == 5000,
          "refresh: an uncalibrated F7 adopts the server's seed");
}

static void test_hibernate_timer_is_drift_corrected(void)
{
    reset_all_mocks();
    hokku_clock_set((uint32_t)T0);
    sched()->next_epoch = T0 + 3600;
    sched()->cal_ppm = 10000;
    int64_t secs = hokku_sched_arm_sleep(sched(), (int64_t)hokku_clock_now(), 0,
                                         HOKKU_FALLBACK_SLEEP_S, HOKKU_HIBERNATE_MAX_S);
    CHECK(secs == 3564 && sched()->sleep_start_epoch == T0,
          "hibernate: armed for the server's slot, drift taken out, start recorded");
    sched()->next_epoch = T0 + 100000;
    secs = hokku_sched_arm_sleep(sched(), (int64_t)hokku_clock_now(), 0,
                                 HOKKU_FALLBACK_SLEEP_S, HOKKU_HIBERNATE_MAX_S);
    CHECK(secs == HOKKU_HIBERNATE_MAX_S && sched()->armed_s == (int32_t)HOKKU_HIBERNATE_MAX_S,
          "hibernate: capped at the wake timer's range, and the cap is what is recorded");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  Persistent state record (common/xr872/state.c)
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_state_round_trip_and_wear_guard(void)
{
    reset_all_mocks();
    _mock_fdcm_open_fail = 0;
    hokku_state_load();                 /* nothing in flash: zero state */
    hokku_state_get()->boot_count = 7;
    hokku_state_get()->sched.cal_ppm = -1234;
    CHECK(hokku_state_save() == 0 && _mock_fdcm_write_call_count == 1,
          "state: a changed record is written");
    CHECK(hokku_state_save() == 0 && _mock_fdcm_write_call_count == 1,
          "state: an unchanged record is not rewritten (flash wear)");

    memcpy(_mock_fdcm_read_buf, _mock_fdcm_write_buf, sizeof(hokku_state_t));
    _mock_fdcm_read_size = sizeof(hokku_state_t);
    hokku_state_load();
    CHECK(hokku_state_get()->boot_count == 7 && hokku_state_get()->sched.cal_ppm == -1234,
          "state: reloads what was written");

    _mock_fdcm_read_buf[20] ^= 0x40;    /* a torn write */
    hokku_state_load();
    CHECK(hokku_state_get()->boot_count == 0 && hokku_state_get()->sched.cal_samples == 0,
          "state: a corrupt record (CRC) starts from zero");
    _mock_fdcm_open_fail = 1;
}

/* ═══════════════════════════════════════════════════════════════════════
 *  New-firmware confirmation (common/all/ota_confirm.h) with the F7's
 *  A/B mechanics
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_boot_ok_confirms_a_normal_boot(void)
{
    reset_all_mocks();
    g_boot_seq = 1;
    g_rollback_armed = 1;
    hokku_rollback_boot_ok(0);
    CHECK(!g_rollback_armed && _mock_wdg_stop_called == 1 &&
          _mock_image_set_cfg_last.seq == 1,
          "boot_ok: a normal boot points the cfg back at itself and stops the WDG");
}

static void test_boot_ok_keeps_previous_slot_while_pending(void)
{
    reset_all_mocks();
    g_boot_seq = 1;
    g_rollback_armed = 1;
    hokku_rollback_boot_ok(1);
    CHECK(g_rollback_armed && _mock_wdg_stop_called == 1 && _mock_image_set_cfg_call_count == 0,
          "boot_ok: a pending image stops the WDG but leaves the cfg on the previous slot");
}

static void test_pending_confirms_when_server_reached(void)
{
    reset_all_mocks();
    g_boot_seq = 1;
    g_rollback_armed = 1;
    g_ota_pending = 1;
    hokku_state_get()->ota_pending = 2;           /* slot 1, unconfirmed */
    mock_server_reply(404, "X-Sleep-Seconds: 21600", 0);   /* keep reply */
    hokku_first_fetch();
    CHECK(!g_ota_pending && !g_rollback_armed && hokku_state_get()->ota_pending == 0 &&
          _mock_image_set_cfg_last.seq == 1 && _mock_wdg_reboot_called == 0,
          "pending: the first fetch reaching the server confirms (cfg -> own slot, marker cleared)");
}

static void test_pending_rolls_back_when_server_never_reached(void)
{
    reset_all_mocks();
    g_boot_seq = 1;
    g_rollback_armed = 1;
    g_ota_pending = 1;
    hokku_state_get()->ota_pending = 2;
    g_net_up = 1;                                 /* WiFi up, server unreachable */
    hokku_first_fetch();
    CHECK(_mock_wdg_reboot_called == 1 && g_rollback_armed &&
          _mock_image_set_cfg_call_count == 0,
          "pending: never reaching the server reboots with the cfg on the previous slot");
    CHECK(_mock_epd_text_calls == 1,
          "pending: the outage message is drawn once, not per attempt");
}

static void test_not_pending_fetches_once(void)
{
    reset_all_mocks();
    g_net_up = 1;                                 /* server unreachable */
    hokku_first_fetch();
    CHECK(_mock_wdg_reboot_called == 0 && sched()->failures == 1,
          "not pending: one fetch, normal backoff, no rollback");
}

static void test_ota_refused_while_pending(void)
{
    reset_all_mocks();
    memset(&g_test_iop, 0, sizeof(g_test_iop));
    g_test_iop.bl_size = 0x8000;
    g_test_iop.addr[1] = 0x181000;
    g_test_iop.img_max_size = 1500;
    _mock_image_ota_param = &g_test_iop;
    g_ota_pending = 1;
    hokku_do_ota("1.0");
    CHECK(_mock_ota_init_called == 0 && _mock_epd_text_calls == 0,
          "ota: refused while this image is unconfirmed (the other slot is its rollback)");
}

static void test_ota_success_marks_new_slot_pending(void)
{
    reset_all_mocks();
    memset(&g_test_iop, 0, sizeof(g_test_iop));
    g_test_iop.bl_size = 0x8000;
    g_test_iop.addr[1] = 0x181000;
    g_test_iop.img_max_size = 1500;
    _mock_image_ota_param = &g_test_iop;
    _mock_image_running_seq = 0;                  /* writes slot 1 */
    hokku_do_ota("1.0");
    CHECK(hokku_state_get()->ota_pending == 2 && _mock_ota_reboot_called == 1,
          "ota: the new slot is marked unconfirmed before the reboot");
    CHECK(strcmp(_mock_epd_text, HOKKU_MSG_OTA_DONE) == 0 && _mock_epd_text_calls == 2,
          "ota: the shared start/complete messages are drawn");

    reset_all_mocks();
    _mock_image_ota_param = &g_test_iop;
    _mock_ota_get_image_result = OTA_STATUS_ERROR;
    hokku_do_ota("1.0");
    CHECK(hokku_state_get()->ota_pending == 0 && strstr(_mock_epd_text, "(download)"),
          "ota: a failed download leaves nothing pending and says so");
}

int main(void)
{
    printf("=== test_logic (bigme_f7) ===\n\n");

    test_xip_offset_slot0();
    test_xip_offset_slot1();

    test_should_sleep_pwr_sleep_always_true();
    test_should_sleep_pwr_awake_always_false();
    test_should_sleep_auto_sleeps_on_battery();
    test_should_sleep_auto_stays_awake_on_usb();
    test_should_sleep_interactive_overrides_pwr_sleep();
    test_should_sleep_interactive_inert_on_battery();
    test_should_sleep_interactive_cleared_restores_config();

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
    test_wifi_provision_live_switch_disables_config_enables();
    test_wifi_provision_config_failure_reenables_station();
    test_wifi_connect_saved_uses_same_sequence();
    test_wifi_provision_rejected_input_leaves_station_alone();

    test_hibernate_clamps_low_sleep();
    test_hibernate_clamps_high_sleep();
    test_hibernate_passes_through_normal_value();

    test_net_cb_wlan_connected_no_netif_does_not_crash();
    test_net_cb_wlan_connected_dhcp_leaves_sdk_dhcp_running();
    test_net_cb_wlan_connected_static_ip_sets_address();
    test_net_cb_wlan_connected_bad_static_ip_leaves_dhcp();
    test_net_cb_network_up_marks_network();
    test_net_cb_network_up_kicks_running_refresh_thread();
    test_refresh_wait_returns_early_when_kicked();
    test_refresh_wait_without_semaphore_falls_back_to_sleep();
    test_net_cb_network_down_does_not_crash();

    test_frame_receive_acks_every_chunk();
    test_frame_receive_restores_console_when_host_dies();
    test_frame_receive_refuses_while_ota_lock_held();

    test_refresh_404_label_filter_keeps_picture();
    test_refresh_503_busy_honours_header();
    test_refresh_error_without_header_backs_off();
    test_refresh_first_outage_draws_shared_message();
    test_refresh_wifi_failure_draws_wifi_message();
    test_refresh_image_displays();
    test_refresh_short_image_not_displayed();
    test_refresh_long_image_not_displayed();
    test_refresh_anchors_to_server_clock();
    test_refresh_learns_drift_and_seed();
    test_hibernate_timer_is_drift_corrected();

    test_state_round_trip_and_wear_guard();

    test_boot_ok_confirms_a_normal_boot();
    test_boot_ok_keeps_previous_slot_while_pending();
    test_pending_confirms_when_server_reached();
    test_pending_rolls_back_when_server_never_reached();
    test_not_pending_fetches_once();
    test_ota_refused_while_pending();
    test_ota_success_marks_new_slot_pending();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
