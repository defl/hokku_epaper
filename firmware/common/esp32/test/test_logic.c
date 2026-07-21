/*
 * test_logic.c — host-side unit tests for the shared ESP-IDF modules in
 * firmware/common/esp32/ (config, state, scheduler, log), plus a compile+link
 * check of the whole common/esp32 layer (wifi, net, ota) against the shared
 * mock kit. This tests the shared code in ISOLATION — no firmware board layer.
 *
 * Same technique as the firmware suites: include the mock headers before
 * redefining `static`, then include the shared .c files so their static
 * functions/globals become regular symbols in this translation unit.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdarg.h>
#include <time.h>

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

/* common/all deps first (pure), then the common/esp32 modules. Including
 * wifi/net/ota proves the whole shared ESP32 layer compiles + links against the
 * mocks, even though the logic tests below focus on config/state/scheduler/log. */
#include "../../all/logbuf.c"
#include "../../all/json_util.c"
#include "../../all/firmware_url.c"
#include "../../all/frame_state.c"
#include "config.c"
#include "state.c"
#include "scheduler.c"
#include "log.c"
#include "wifi.c"
#include "net.c"
#include "ota.c"

/* ── Minimal test framework ── */
static int g_pass = 0;
static int g_fail = 0;
#define CHECK(cond, name) do {                              \
    if (cond) { printf("PASS  %s\n", name); g_pass++; }     \
    else      { printf("FAIL  %s\n", name); g_fail++; }     \
} while (0)

/* ═══ scheduler.c ═══ */
static void test_now_epoch_post_2020(void)
{
    CHECK(now_epoch() > 1577836800LL, "scheduler: now_epoch returns a post-2020 timestamp");
}
static void test_refresh_due(void)
{
    next_refresh_epoch = 0;
    CHECK(refresh_due(), "scheduler: refresh_due true when unscheduled (0)");
    next_refresh_epoch = 1;
    CHECK(refresh_due(), "scheduler: refresh_due true when epoch in the past");
    next_refresh_epoch = 9999999999LL;   /* year 2286 */
    CHECK(!refresh_due(), "scheduler: refresh_due false when epoch far in the future");
}
static void test_schedule_retry_in(void)
{
    next_refresh_epoch = 0;
    pre_sleep_server_epoch = 123;
    last_sleep_err_known = true;
    time_t before = time(NULL);
    schedule_retry_in(60, "test");
    time_t after = time(NULL);
    CHECK(next_refresh_epoch >= (int64_t)before + 60 && next_refresh_epoch <= (int64_t)after + 60,
          "scheduler: schedule_retry_in sets next_refresh_epoch to now + seconds");
    CHECK(pre_sleep_server_epoch == 0 && !last_sleep_err_known,
          "scheduler: schedule_retry_in clears the sleep-error snapshot");
}
static void test_save_pre_sleep_epoch(void)
{
    save_pre_sleep_epoch(0, 0);
    CHECK(pre_sleep_server_epoch == 0 && !last_sleep_err_known,
          "scheduler: save_pre_sleep_epoch(0) clears the snapshot");
    save_pre_sleep_epoch(1700000000LL, esp_timer_get_time());
    CHECK(pre_sleep_server_epoch >= 1700000000LL,
          "scheduler: save_pre_sleep_epoch stores a server-anchored epoch");
}

/* ═══ log.c (single crash-safe RTC ring) ═══ */
static int call_log(const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt);
    int n = log_vprintf(fmt, ap);
    va_end(ap);
    return n;
}
static void test_log_ring_lifecycle(void)
{
    s_log_ring_head = 0; s_log_ring_used = 0;
    hokku_log_init();
    call_log("aa"); call_log("bb");
    CHECK(s_log_ring_used > 0, "log: append persists ring position to RTC each line");
    char body[HOKKU_LOG_MAX_UPLOAD];
    size_t n = hokku_log_snapshot(body, sizeof(body)); body[n] = '\0';
    CHECK(strcmp(body, "aabb") == 0, "log: snapshot returns the ring contents");

    /* Reconstruct from RTC (simulated reboot) — pre-reboot logs must survive. */
    hokku_log_init();
    call_log("cc");
    n = hokku_log_snapshot(body, sizeof(body)); body[n] = '\0';
    CHECK(strcmp(body, "aabbcc") == 0, "log: RTC ring survives a reboot (crash-safe)");

    hokku_log_reset();
    n = hokku_log_snapshot(body, sizeof(body));
    CHECK(n == 0 && s_log_ring_used == 0, "log: reset clears the ring");
}

/* ═══ config.c ═══ */
static void test_config_valid(void)
{
    memset(&config, 0, sizeof(config));
    config.cfg_ver = CONFIG_VERSION;
    CHECK(!config_is_valid(), "config: invalid with no SSID / image_url");
    strcpy(config.wifi_ssid[0], "net");
    strcpy(config.image_url, "http://h/hokku/screen/");
    CHECK(config_is_valid(), "config: valid with primary SSID + image_url + cfg_ver");
    config.cfg_ver = CONFIG_VERSION + 1;
    CHECK(!config_is_valid(), "config: invalid on cfg_ver mismatch");
}

int main(void)
{
    printf("=== test_logic (common/esp32) ===\n\n");
    test_now_epoch_post_2020();
    test_refresh_due();
    test_schedule_retry_in();
    test_save_pre_sleep_epoch();
    test_log_ring_lifecycle();
    test_config_valid();
    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
