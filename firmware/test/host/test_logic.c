/*
 * test_logic.c — host-side unit tests for pure logic functions in main.c:
 *   - config_is_valid
 *   - now_epoch / refresh_due / schedule_retry_in
 *   - usb_host_present_stable  (debounce state machine)
 *   - button1_pressed_debounced (debounce state machine)
 *
 * Strategy: include all ESP-IDF mock headers BEFORE redefining `static`,
 * then include the firmware source so the static functions are exposed as
 * regular symbols in this translation unit.
 *
 * Build: compiled by firmware/test/host/CMakeLists.txt.
 * Run:   ./test_logic   (exit 0 on all pass, 1 if any fail)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <time.h>

/* ── Mock headers (included before #define static so their own
 *    static/static-inline functions are compiled with proper storage class) ── */
#include "mocks/freertos/FreeRTOS.h"
#include "mocks/freertos/task.h"
#include "mocks/freertos/event_groups.h"
#include "mocks/driver/gpio.h"
#include "mocks/driver/spi_master.h"
#include "mocks/driver/rtc_io.h"
#include "mocks/esp_adc/adc_oneshot.h"
#include "mocks/esp_adc/adc_cali.h"
#include "mocks/esp_adc/adc_cali_scheme.h"
#include "mocks/esp_private/esp_clk.h"
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

/* ── Expose all static functions and variables from the firmware source ──
 * #define static must come AFTER the mock headers so their own static-inline
 * functions keep their intended storage class (and include guards prevent
 * re-processing when main.c re-includes the same headers). */
#define static

#include "../../main/text_render.c"  /* font table + draw_char + draw_string */
#include "../../main/main.c"         /* all firmware logic                   */

/* ── Minimal test framework ──────────────────────────────────────────── */
static int g_pass = 0;
static int g_fail = 0;

#define CHECK(cond, name) do {                                      \
    if (cond) { printf("PASS  %s\n", name); g_pass++; }            \
    else       { printf("FAIL  %s\n", name); g_fail++; }           \
} while (0)

/* ── Helpers ─────────────────────────────────────────────────────────── */

/* Reset the USB debounce state machine to its power-on defaults. */
static void reset_usb_debounce(void)
{
    s_usb_stable_level    = 1;   /* assume no USB host (GPIO 14 HIGH) */
    s_usb_opposite_streak = 0;
}

/* Reset the button debounce state machine to its power-on defaults. */
static void reset_btn_debounce(void)
{
    s_btn_low_count = 0;
    s_btn_reported  = false;
}

/* Drive GPIO pin to the given level for the next mock read. */
static void gpio_set_mock(int pin, int level) { _mock_gpio[pin] = level; }

/* ═══════════════════════════════════════════════════════════════════════
 *  config_is_valid tests
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_config_invalid_when_both_empty(void)
{
    memset(&config, 0, sizeof(config));
    CHECK(!config_is_valid(), "config_is_valid: returns false when ssid and url are empty");
}

static void test_config_invalid_when_only_ssid_set(void)
{
    memset(&config, 0, sizeof(config));
    strncpy(config.wifi_ssid, "MySSID", sizeof(config.wifi_ssid) - 1);
    CHECK(!config_is_valid(), "config_is_valid: returns false when url is empty");
}

static void test_config_invalid_when_only_url_set(void)
{
    memset(&config, 0, sizeof(config));
    strncpy(config.image_url, "http://example.com/img", sizeof(config.image_url) - 1);
    CHECK(!config_is_valid(), "config_is_valid: returns false when ssid is empty");
}

static void test_config_valid_when_both_set(void)
{
    memset(&config, 0, sizeof(config));
    strncpy(config.wifi_ssid, "MySSID",               sizeof(config.wifi_ssid)   - 1);
    strncpy(config.image_url, "http://example.com/img", sizeof(config.image_url) - 1);
    CHECK(config_is_valid(), "config_is_valid: returns true when ssid and url are both set");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  now_epoch tests
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_now_epoch_returns_post_2020_value(void)
{
    /* The host clock is > 2020. now_epoch() should return actual time. */
    time_t t = now_epoch();
    CHECK(t > 1577836800LL,
          "now_epoch: returns a Unix timestamp later than 2020-01-01");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  refresh_due tests
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_refresh_due_when_not_scheduled(void)
{
    next_refresh_epoch = 0;
    CHECK(refresh_due(), "refresh_due: returns true when next_refresh_epoch == 0 (unscheduled)");
}

static void test_refresh_due_when_epoch_in_past(void)
{
    next_refresh_epoch = 1;  /* ancient past — always before real time */
    CHECK(refresh_due(), "refresh_due: returns true when epoch is in the past");
}

static void test_refresh_not_due_when_epoch_far_future(void)
{
    next_refresh_epoch = (int64_t)9999999999LL;  /* year 2286 */
    CHECK(!refresh_due(), "refresh_due: returns false when epoch is far in the future");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  schedule_retry_in tests
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_schedule_retry_sets_next_epoch_to_now_plus_seconds(void)
{
    next_refresh_epoch = 0;
    time_t before = time(NULL);
    schedule_retry_in(60, "test");
    time_t after  = time(NULL);

    /* next_refresh_epoch should be in [before+60, after+60] */
    CHECK(next_refresh_epoch >= (int64_t)before + 60 &&
          next_refresh_epoch <= (int64_t)after  + 60,
          "schedule_retry_in: sets next_refresh_epoch to now + seconds");
}

static void test_schedule_retry_clears_sleep_error_state(void)
{
    pre_sleep_server_epoch = 12345;
    last_sleep_err_known   = true;
    schedule_retry_in(60, "test");
    CHECK(pre_sleep_server_epoch == 0,
          "schedule_retry_in: clears pre_sleep_server_epoch");
    CHECK(!last_sleep_err_known,
          "schedule_retry_in: clears last_sleep_err_known");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  usb_host_present_stable — debounce state machine
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_usb_stable_no_usb_on_single_high_read(void)
{
    reset_usb_debounce();
    gpio_set_mock(PIN_USB_DETECT, 1);  /* HIGH = no USB */
    CHECK(!usb_host_present_stable(),
          "usb_stable: single HIGH read → no USB host");
}

static void test_usb_stable_no_usb_after_two_low_glitches(void)
{
    /* Two LOWs are not enough to flip; need USB_DEBOUNCE_SAMPLES = 3.
     * The CHECK itself is the second call, so opposite_streak reaches 2 — still
     * below threshold → state does not flip → returns false (no USB). */
    reset_usb_debounce();
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();  /* streak = 1 */
    gpio_set_mock(PIN_USB_DETECT, 0);
    CHECK(!usb_host_present_stable(),   /* streak = 2, no flip yet */
          "usb_stable: two LOW glitches do not trigger USB detection (need 3)");
}

static void test_usb_stable_usb_detected_after_three_lows(void)
{
    reset_usb_debounce();
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();  /* streak = 1 */
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();  /* streak = 2 */
    gpio_set_mock(PIN_USB_DETECT, 0);
    bool detected = usb_host_present_stable();                     /* streak = 3, flip */
    CHECK(detected, "usb_stable: three consecutive LOWs → USB host detected");
}

static void test_usb_stable_stays_detected_on_continued_low(void)
{
    reset_usb_debounce();
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();  /* now stable LOW */
    gpio_set_mock(PIN_USB_DETECT, 0);
    CHECK(usb_host_present_stable(),
          "usb_stable: subsequent LOW reads remain stable (USB still detected)");
}

static void test_usb_stable_glitch_high_does_not_immediately_undetect(void)
{
    /* Stabilise in USB-detected state (3 consecutive LOWs), then send one
     * HIGH glitch.  One sample is not enough to flip back. */
    reset_usb_debounce();
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();
    gpio_set_mock(PIN_USB_DETECT, 1);
    CHECK(usb_host_present_stable(),
          "usb_stable: single HIGH glitch while USB stable does not flip state");
}

static void test_usb_stable_glitch_resets_opposite_streak(void)
{
    /* Two LOWs followed by one HIGH resets the opposite streak.
     * After the HIGH, two more LOWs are still not enough to flip. */
    reset_usb_debounce();
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();  /* streak = 1 */
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();  /* streak = 2 */
    gpio_set_mock(PIN_USB_DETECT, 1); usb_host_present_stable();  /* streak reset to 0 */
    gpio_set_mock(PIN_USB_DETECT, 0); usb_host_present_stable();  /* streak = 1 */
    gpio_set_mock(PIN_USB_DETECT, 0);
    /* streak = 2, still below USB_DEBOUNCE_SAMPLES = 3 → not yet detected */
    CHECK(!usb_host_present_stable(),
          "usb_stable: a HIGH glitch mid-sequence resets the streak counter");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  button1_pressed_debounced — debounce state machine
 * ═══════════════════════════════════════════════════════════════════════ */

static void test_btn_single_low_does_not_fire(void)
{
    reset_btn_debounce();
    gpio_set_mock(PIN_BUTTON_1, 0);
    CHECK(!button1_pressed_debounced(),
          "btn_debounce: single LOW does not fire (need BUTTON_DEBOUNCE_SAMPLES=2)");
}

static void test_btn_fires_after_required_consecutive_lows(void)
{
    /* BUTTON_DEBOUNCE_SAMPLES = 2: two consecutive LOWs → true on the 2nd. */
    reset_btn_debounce();
    gpio_set_mock(PIN_BUTTON_1, 0); button1_pressed_debounced();   /* count = 1 */
    gpio_set_mock(PIN_BUTTON_1, 0);
    CHECK(button1_pressed_debounced(),
          "btn_debounce: fires on the Nth consecutive LOW (N=BUTTON_DEBOUNCE_SAMPLES)");
}

static void test_btn_does_not_fire_again_while_held(void)
{
    /* After firing, holding the button LOW should not produce a second event. */
    reset_btn_debounce();
    gpio_set_mock(PIN_BUTTON_1, 0); button1_pressed_debounced();
    gpio_set_mock(PIN_BUTTON_1, 0); button1_pressed_debounced();  /* fires here */
    gpio_set_mock(PIN_BUTTON_1, 0);
    CHECK(!button1_pressed_debounced(),
          "btn_debounce: does not fire a second time while button is held LOW");
}

static void test_btn_resets_after_release(void)
{
    /* Release (HIGH) clears the state, so the next press can fire again. */
    reset_btn_debounce();
    gpio_set_mock(PIN_BUTTON_1, 0); button1_pressed_debounced();
    gpio_set_mock(PIN_BUTTON_1, 0); button1_pressed_debounced();  /* fires */
    gpio_set_mock(PIN_BUTTON_1, 1); button1_pressed_debounced();  /* release */
    gpio_set_mock(PIN_BUTTON_1, 0); button1_pressed_debounced();  /* count = 1 */
    gpio_set_mock(PIN_BUTTON_1, 0);
    CHECK(button1_pressed_debounced(),
          "btn_debounce: resets after release so next press can fire again");
}

static void test_btn_glitch_high_clears_count(void)
{
    /* A single HIGH between two LOWs resets low_count to 0. */
    reset_btn_debounce();
    gpio_set_mock(PIN_BUTTON_1, 0); button1_pressed_debounced();  /* count = 1 */
    gpio_set_mock(PIN_BUTTON_1, 1); button1_pressed_debounced();  /* count = 0 */
    gpio_set_mock(PIN_BUTTON_1, 0);
    /* count = 1, below threshold → does not fire */
    CHECK(!button1_pressed_debounced(),
          "btn_debounce: a HIGH glitch between LOWs resets the counter");
}

/* ═══════════════════════════════════════════════════════════════════════
 *  Entry point
 * ═══════════════════════════════════════════════════════════════════════ */

int main(void)
{
    /* All mock GPIO pins start at 0 (LOW). Set defaults appropriate for the
     * firmware's expected hardware idle state. */
    memset(_mock_gpio, 0, sizeof(_mock_gpio));
    _mock_gpio[PIN_USB_DETECT] = 1;  /* no USB host */
    _mock_gpio[PIN_BUTTON_1]   = 1;  /* button released */
    _mock_gpio[PIN_EPAPER_BUSY]= 1;  /* display not busy */

    printf("=== test_logic ===\n\n");

    /* config */
    test_config_invalid_when_both_empty();
    test_config_invalid_when_only_ssid_set();
    test_config_invalid_when_only_url_set();
    test_config_valid_when_both_set();

    /* clock */
    test_now_epoch_returns_post_2020_value();

    /* schedule */
    test_refresh_due_when_not_scheduled();
    test_refresh_due_when_epoch_in_past();
    test_refresh_not_due_when_epoch_far_future();
    test_schedule_retry_sets_next_epoch_to_now_plus_seconds();
    test_schedule_retry_clears_sleep_error_state();

    /* USB debounce */
    test_usb_stable_no_usb_on_single_high_read();
    test_usb_stable_no_usb_after_two_low_glitches();
    test_usb_stable_usb_detected_after_three_lows();
    test_usb_stable_stays_detected_on_continued_low();
    test_usb_stable_glitch_high_does_not_immediately_undetect();
    test_usb_stable_glitch_resets_opposite_streak();

    /* Button debounce */
    test_btn_single_low_does_not_fire();
    test_btn_fires_after_required_consecutive_lows();
    test_btn_does_not_fire_again_while_held();
    test_btn_resets_after_release();
    test_btn_glitch_high_clears_count();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
