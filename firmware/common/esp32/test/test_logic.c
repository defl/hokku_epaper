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
#include "../../all/screen_ident.c"
#include "../../all/backoff.c"
#include "../../all/fetch_outcome.c"
#include "../../all/schedule.c"
#include "../../all/messages.c"
#include "../../all/ota_confirm.c"
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

/* ═══ scheduler.c — ESP-IDF clocks around common/all/schedule.c ═══
 * The schedule/drift logic itself is tested in common/all/test/test_schedule.c;
 * these check the glue: this board's clocks and RTC state reach it. */
static void test_now_epoch_post_2020(void)
{
    CHECK(now_epoch() > HOKKU_EPOCH_MIN, "scheduler: now_epoch returns a post-2020 timestamp");
}
static void test_refresh_due(void)
{
    hokku_sched_init(&hokku_sched);
    CHECK(refresh_due(), "scheduler: refresh_due true when unscheduled (0)");
    hokku_sched.next_epoch = 1;
    CHECK(refresh_due(), "scheduler: refresh_due true when epoch in the past");
    hokku_sched.next_epoch = 9999999999LL;   /* year 2286 */
    CHECK(!refresh_due(), "scheduler: refresh_due false when epoch far in the future");
}

static void test_apply_fetch(void)
{
    /* Image with a full schedule: anchored to the server clock, streak cleared. */
    hokku_sched_init(&hokku_sched);
    hokku_sched.failures = 3;
    last_sleep_mode = LAST_SLEEP_MODE_NONE;
    hokku_fetch_result_t img = { .http_status = 200, .image_ok = true, .sleep_s = 3600,
                                 .server_epoch = 1700000000LL };
    hokku_fetch_outcome_t o = scheduler_apply_fetch(&img);
    CHECK(o.action == HOKKU_FETCH_DISPLAY && hokku_sched.next_epoch == 1700003600LL &&
          hokku_sched.failures == 0,
          "scheduler: apply_fetch anchors a displayed image to the server clock");

    /* Outage: streak grows, backoff doubles, relative to the device clock. */
    hokku_sched.failures = 1;
    hokku_fetch_result_t down = { .http_status = 0 };
    time_t before = time(NULL);
    o = scheduler_apply_fetch(&down);
    CHECK(o.action == HOKKU_FETCH_BACKOFF && hokku_sched.failures == 2 &&
          o.sleep_s == 2 * HOKKU_RETRY_BASE_S &&
          hokku_sched.next_epoch >= (int64_t)before + 2 * HOKKU_RETRY_BASE_S,
          "scheduler: apply_fetch outage bumps the streak and backs off");
}

static void test_timer_wake_learns_drift(void)
{
    /* Armed 43200 s; the server says it is now 43632 s after sleep start and we
     * have been awake 10 s: the oscillator ran ~1% slow. */
    hokku_sched_init(&hokku_sched);
    hokku_sched.sleep_start_epoch = 1700000000LL;
    hokku_sched.armed_s = 43200;
    hokku_sched.next_epoch = 1700000000LL + 43200;
    last_sleep_mode = LAST_SLEEP_MODE_TIMER_WAKE;
    _mock_timer_us = 10LL * 1000000LL;
    hokku_fetch_result_t img = { .http_status = 200, .image_ok = true, .sleep_s = 3600,
                                 .server_epoch = 1700000000LL + 43200 + 432 + 10 };
    scheduler_apply_fetch(&img);
    CHECK(hokku_sched.cal_samples == 1 && hokku_sched.cal_ppm >= 9900 &&
          hokku_sched.cal_ppm <= 10100,
          "scheduler: a timer wake learns the drift from the server clock");
    CHECK(hokku_sched.sleep_err_known && hokku_sched.sleep_err_s == 432,
          "scheduler: records how late the wake landed");

    last_sleep_mode = LAST_SLEEP_MODE_BUTTON_WAKE;
    hokku_sched.sleep_start_epoch = 1700000000LL;
    scheduler_apply_fetch(&img);
    CHECK(hokku_sched.cal_samples == 1, "scheduler: a button wake is not a drift sample");
    _mock_timer_us = 0;
}

static void test_next_sleep_us_calibrated(void)
{
    hokku_sched_init(&hokku_sched);
    hokku_sched.next_epoch = (int64_t)time(NULL) + 3600;
    int64_t us = scheduler_next_sleep_us(9999);
    CHECK(us >= 3590000000LL && us <= 3600000000LL && hokku_sched.armed_s >= 3590,
          "scheduler: next_sleep_us arms ~the remaining time and records it");

    hokku_sched.cal_ppm = 10000;
    hokku_sched.next_epoch = (int64_t)time(NULL) + 3600;
    us = scheduler_next_sleep_us(9999);
    CHECK(us >= 3554000000LL && us <= 3566000000LL,
          "scheduler: next_sleep_us arms less for a slow clock");

    hokku_sched.next_epoch = 0;
    us = scheduler_next_sleep_us(7200);
    CHECK(us == 7200000000LL && hokku_sched.armed_s == 0,
          "scheduler: next_sleep_us returns the fallback when unscheduled");
}

static void test_retry_in(void)
{
    hokku_sched_init(&hokku_sched);
    time_t before = time(NULL);
    scheduler_retry_in(HOKKU_FALLBACK_SLEEP_S);
    CHECK(hokku_sched.next_epoch >= (int64_t)before + HOKKU_FALLBACK_SLEEP_S,
          "scheduler: retry_in schedules the next try from now");
}

/* ═══ ota.c — the shared confirm policy with ESP-IDF mechanics ═══ */
static int g_fetches;
static hokku_fetch_action_t fetch_fails(void *ctx)
{
    (void)ctx;
    g_fetches++;
    return HOKKU_FETCH_BACKOFF;
}

static void test_ota_first_fetch_not_pending(void)
{
    /* The mock reports no pending image: one fetch, no retries, even if it failed. */
    g_fetches = 0;
    hokku_fetch_action_t a = ota_first_fetch(fetch_fails, NULL);
    CHECK(a == HOKKU_FETCH_BACKOFF && g_fetches == 1,
          "ota: a confirmed image fetches once, whatever the outcome");
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

static void test_config_set_screen_name(void)
{
    memset(&config, 0, sizeof(config));
    strcpy(config.screen_name, "old");
    _mock_nvs_open_fail = 0;
    _mock_nvs_set_str_fail = 0;
    _mock_nvs_set_screen_name[0] = '\0';

    CHECK(config_set_screen_name("kitchen"), "rename: valid name accepted");
    CHECK(strcmp(config.screen_name, "kitchen") == 0, "rename: live config updated");
    CHECK(strcmp(_mock_nvs_set_screen_name, "kitchen") == 0, "rename: persisted to NVS");

    CHECK(!config_set_screen_name("bad/name"), "rename: invalid name refused");
    CHECK(strcmp(config.screen_name, "kitchen") == 0, "rename: refused name leaves config alone");

    /* The server echoes the name on every response, including a legacy name
     * (set over USB) the rename rule would refuse: that must be a quiet no-op. */
    strcpy(config.screen_name, "caf\xc3\xa9/1");
    _mock_nvs_set_screen_name[0] = '\0';
    CHECK(config_set_screen_name("caf\xc3\xa9/1"), "rename: unchanged legacy name is a no-op");
    CHECK(_mock_nvs_set_screen_name[0] == '\0', "rename: unchanged name is never rewritten");
    strcpy(config.screen_name, "kitchen");

    _mock_nvs_set_str_fail = 1;
    CHECK(!config_set_screen_name("hall"), "rename: NVS write failure reported");
    CHECK(strcmp(config.screen_name, "kitchen") == 0,
          "rename: failed write leaves the old name (RAM never ahead of flash)");
    _mock_nvs_set_str_fail = 0;
    _mock_nvs_open_fail = 1;
}

int main(void)
{
    printf("=== test_logic (common/esp32) ===\n\n");
    test_now_epoch_post_2020();
    test_refresh_due();
    test_apply_fetch();
    test_timer_wake_learns_drift();
    test_next_sleep_us_calibrated();
    test_retry_in();
    test_ota_first_fetch_not_pending();
    test_log_ring_lifecycle();
    test_config_valid();
    test_config_set_screen_name();
    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
