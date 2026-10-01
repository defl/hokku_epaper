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
#include "../../all/sleep_cal.c"
#include "config.c"
#include "state.c"
#include "scheduler.c"
#include "nvs_cal.c"
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

/* ═══ scheduler.c — consolidated schedule + drift calibration ═══ */
static void test_set_after_refresh(void)
{
    next_refresh_epoch = 42;   /* sentinel to prove the reject path changes nothing */
    bool ok = scheduler_set_after_refresh(1700000000LL, 3600, esp_timer_get_time());
    CHECK(ok && next_refresh_epoch == 1700003600LL && last_sleep_seconds == 3600 &&
          pre_sleep_server_epoch >= 1700000000LL,
          "scheduler: set_after_refresh anchors next_ep = server_epoch + sleep");

    next_refresh_epoch = 42;
    ok = scheduler_set_after_refresh(0, 3600, esp_timer_get_time());
    CHECK(!ok && next_refresh_epoch == 42,
          "scheduler: set_after_refresh rejects server_epoch<=0, leaves state for caller fallback");
}

static void test_observe_sleep_learns_drift(void)
{
    /* Slept ~1% long (slow oscillator). intended=armed=43200; actual=+432. */
    const int64_t armed = 43200, actual = 43200 + 432;
    last_sleep_mode = LAST_SLEEP_MODE_TIMER_WAKE;
    last_sleep_seconds = (int32_t)armed;
    last_armed_sleep_s = (int32_t)armed;
    pre_sleep_server_epoch = (int64_t)time(NULL) - actual;
    cal_ppm = 0; cal_samples = 0; last_sleep_err_known = false;

    scheduler_observe_sleep();

    CHECK(last_sleep_err_known && last_sleep_err_s >= 430 && last_sleep_err_s <= 435,
          "scheduler: observe_sleep records the slot error (~+432 s)");
    CHECK(cal_ppm >= 9900 && cal_ppm <= 10100 && cal_samples == 1,
          "scheduler: observe_sleep learns +1% drift (~+10000 ppm) on first sample");
}

static void test_observe_sleep_skips_non_timer(void)
{
    last_sleep_mode = LAST_SLEEP_MODE_BUTTON_WAKE;
    last_sleep_seconds = 43200; last_armed_sleep_s = 43200;
    pre_sleep_server_epoch = (int64_t)time(NULL) - 43600;
    cal_ppm = 1234; cal_samples = 5; last_sleep_err_known = false;

    scheduler_observe_sleep();

    CHECK(cal_ppm == 1234 && cal_samples == 5 && !last_sleep_err_known,
          "scheduler: observe_sleep is a no-op on a non-timer wake");
}

static void test_observe_sleep_skips_without_armed(void)
{
    /* Retry/fallback sleeps leave last_armed_sleep_s == 0: err is still recorded
     * but no drift sample is taken. */
    last_sleep_mode = LAST_SLEEP_MODE_TIMER_WAKE;
    last_sleep_seconds = 43200; last_armed_sleep_s = 0;
    pre_sleep_server_epoch = (int64_t)time(NULL) - 43600;
    cal_ppm = 1234; cal_samples = 5; last_sleep_err_known = false;

    scheduler_observe_sleep();

    CHECK(last_sleep_err_known && cal_ppm == 1234 && cal_samples == 5,
          "scheduler: observe_sleep records err but skips calibration without an armed value");
}

static void test_next_sleep_us_calibrated(void)
{
    /* No drift: arms ~the desired interval and records it. */
    cal_ppm = 0; cal_samples = 0;
    next_refresh_epoch = (int64_t)time(NULL) + 3600;
    int64_t us = scheduler_next_sleep_us(9999000000LL);
    CHECK(us >= 3590000000LL && us <= 3600000000LL && last_armed_sleep_s >= 3590 &&
          last_armed_sleep_s <= 3600,
          "scheduler: next_sleep_us arms ~desired and records last_armed_sleep_s");

    /* Slow oscillator (+10000 ppm) -> arm LESS than desired. */
    cal_ppm = 10000;
    next_refresh_epoch = (int64_t)time(NULL) + 3600;
    us = scheduler_next_sleep_us(9999000000LL);
    CHECK(us >= 3554000000LL && us <= 3566000000LL,
          "scheduler: next_sleep_us arms less for a slow clock");

    /* Tick-deadline (negative) -> not calibrated, last_armed cleared. */
    cal_ppm = 10000;
    next_refresh_epoch = -(esp_timer_get_time() + 5000000LL);
    us = scheduler_next_sleep_us(9999000000LL);
    CHECK(us > 0 && us <= 5000000LL && last_armed_sleep_s == 0,
          "scheduler: next_sleep_us honours a tick deadline without calibrating");

    /* Unscheduled (zero) -> board fallback, last_armed cleared. */
    next_refresh_epoch = 0;
    us = scheduler_next_sleep_us(7200000000LL);
    CHECK(us == 7200000000LL && last_armed_sleep_s == 0,
          "scheduler: next_sleep_us returns the board fallback when unscheduled");
}

static void test_adopt_cal_seed(void)
{
    cal_ppm = 0; cal_samples = 0;
    CHECK(scheduler_adopt_cal_seed(8000, 5) && cal_ppm == 8000 && cal_samples == 1,
          "scheduler: uncalibrated device adopts a well-backed server seed");

    cal_ppm = 3000; cal_samples = 4;   /* already calibrated */
    CHECK(!scheduler_adopt_cal_seed(8000, 5) && cal_ppm == 3000,
          "scheduler: a calibrated device ignores the seed");

    cal_ppm = 0; cal_samples = 0;
    CHECK(!scheduler_adopt_cal_seed(8000, 2) && cal_ppm == 0,
          "scheduler: seed rejected when server sample count is too low");
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
    test_set_after_refresh();
    test_observe_sleep_learns_drift();
    test_observe_sleep_skips_non_timer();
    test_observe_sleep_skips_without_armed();
    test_next_sleep_us_calibrated();
    test_adopt_cal_seed();
    test_log_ring_lifecycle();
    test_config_valid();
    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
