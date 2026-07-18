/*
 * test_logic.c — host-side unit tests for the seeedstudio_e1004 firmware.
 *
 * Strategy (same as the huessen suite): include all ESP-IDF mock headers BEFORE
 * redefining `static`, then include the shared modules + this firmware's main.c
 * so the static functions/globals become regular symbols in this translation
 * unit. This both unit-tests the board-specific frame-state gatherer AND proves
 * the whole firmware links against the shared common/{all,esp32} modules.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <time.h>

/* ── Mock headers (before #define static) ── */
#include "mocks/freertos/FreeRTOS.h"
#include "mocks/freertos/task.h"
#include "mocks/freertos/event_groups.h"
#include "mocks/driver/gpio.h"
#include "mocks/driver/spi_master.h"
#include "mocks/driver/rtc_io.h"
#include "mocks/esp_adc/adc_oneshot.h"
#include "mocks/esp_adc/adc_cali.h"
#include "mocks/esp_adc/adc_cali_scheme.h"
#include "mocks/esp_log.h"
#include "mocks/esp_sleep.h"
#include "mocks/esp_wifi.h"
#include "mocks/esp_event.h"
#include "mocks/esp_netif.h"
#include "mocks/esp_http_client.h"
#include "mocks/esp_heap_caps.h"
#include "mocks/nvs_flash.h"
#include "mocks/esp_timer.h"
#include "mocks/esp_app_desc.h"
#include "mocks/esp_ota_ops.h"
#include "mocks/esp_partition.h"

#define static

#include "../../../common/esp32/text_render.c"
#include "../../../common/esp32/config.c"
#include "../../../common/esp32/state.c"
#include "../../../common/esp32/scheduler.c"
#include "../../../common/esp32/log.c"
#include "../../../common/esp32/wifi.c"
#include "../../../common/esp32/net.c"
#include "../../../common/esp32/ota.c"
#include "../../../common/all/firmware_url.c"
#include "../../../common/all/frame_state.c"
#include "../../../common/all/json_util.c"
#include "../../../common/all/logbuf.c"
#include "../../main/main.c"

/* ── Minimal test framework ── */
static int g_pass = 0;
static int g_fail = 0;
#define CHECK(cond, name) do {                              \
    if (cond) { printf("PASS  %s\n", name); g_pass++; }     \
    else      { printf("FAIL  %s\n", name); g_fail++; }     \
} while (0)

/* ═══════════════════════════════════════════════════════════════════════
 *  build_frame_state_json — the board's telemetry gatherer feeding the shared
 *  builder. Verifies the E1004-specific wiring (usb is always "none" — no
 *  USB-host-detect GPIO) and that the shared schema is produced.
 * ═══════════════════════════════════════════════════════════════════════ */
static void test_frame_state_schema(void)
{
    boot_count           = 5;
    last_battery_mv      = 3900;
    next_refresh_epoch   = 0;
    last_sleep_err_known = false;
    last_wifi_used_cache = false;
    last_sleep_mode      = LAST_SLEEP_MODE_TIMER_WAKE;
    current_regime       = "battery_idle";
    config.cfg_ver       = CONFIG_VERSION;

    char buf[512];
    build_frame_state_json(buf, sizeof(buf), "timer", 0);

    CHECK(strstr(buf, "\"usb\":\"none\"") != NULL,
          "frame_state: E1004 always reports usb:none (no USB-host-detect GPIO)");
    CHECK(strstr(buf, "\"fw\":\"test\"") != NULL,
          "frame_state: fw comes from the app descriptor");
    CHECK(strstr(buf, "\"boot\":5") != NULL,
          "frame_state: boot counter reported");
    CHECK(strstr(buf, "\"bat_mv\":3900") != NULL,
          "frame_state: battery mV reported");
    CHECK(strstr(buf, "\"wake\":\"timer\"") != NULL,
          "frame_state: wake label reported");
    CHECK(strstr(buf, "\"last_sleep\":\"timer_wake\"") != NULL,
          "frame_state: last_sleep mapped from last_sleep_mode");
    CHECK(strstr(buf, "\"ota\":1}") != NULL,
          "frame_state: advertises OTA capability");
}

static void test_frame_state_bat_omitted_when_unknown(void)
{
    /* If the ADC read is gated out, last_battery_mv is 0 — huessen/seeed both
     * emit bat_mv unconditionally (0 is a valid reading), so 0 is present. */
    boot_count = 1; last_battery_mv = 0; next_refresh_epoch = 0;
    last_sleep_err_known = false; last_wifi_used_cache = false;
    last_sleep_mode = LAST_SLEEP_MODE_NONE; current_regime = "battery_idle";
    char buf[512];
    build_frame_state_json(buf, sizeof(buf), "first_boot", 0);
    CHECK(strstr(buf, "\"bat_mv\":0") != NULL,
          "frame_state: bat_mv:0 emitted (0 is a real reading, not unknown)");
    CHECK(strstr(buf, "\"sleep_err_s\":null") != NULL,
          "frame_state: sleep_err_s is null when not measured");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  read_battery_mv — divider + sanity gate. Under the mock, adc_oneshot_read
 *  returns raw=2000 and adc_cali_raw_to_voltage does raw*2200/4095 = 1074 mV;
 *  ×2 divider = 2148 mV, which is below the 2500 mV sanity floor, so the
 *  function gates it to 0. This locks the gate behaviour.
 * ═══════════════════════════════════════════════════════════════════════ */
static void test_battery_sanity_gate(void)
{
    int mv = read_battery_mv();
    CHECK(mv == 0,
          "read_battery_mv: gates an implausibly-low reading (2148mV < 2500) to 0");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  config — the shared NVS config store, exercised through seeed's build so
 *  its version.h (CONFIG_VERSION) and config wiring are validated too.
 * ═══════════════════════════════════════════════════════════════════════ */
static void test_config_validity(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION;
    CHECK(!config_is_valid(), "config: invalid with no SSID / image_url");
    strcpy(config.wifi_ssid[0], "net");
    strcpy(config.image_url, "http://host/hokku/screen/");
    CHECK(config_is_valid(), "config: valid with primary SSID + image_url + matching cfg_ver");
    config.cfg_ver = CONFIG_VERSION + 1;
    CHECK(!config_is_valid(), "config: invalid on cfg_ver mismatch");
}

int main(void)
{
    memset(_mock_gpio, 0, sizeof(_mock_gpio));
    printf("=== test_logic (seeedstudio_e1004) ===\n\n");

    test_frame_state_schema();
    test_frame_state_bat_omitted_when_unknown();
    test_battery_sanity_gate();
    test_config_validity();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
