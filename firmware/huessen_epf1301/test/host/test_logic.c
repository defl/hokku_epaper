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
#include "mocks/driver/usb_serial_jtag.h"
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

#include "../../../common/esp32/text_render.c"  /* font table + draw_char + draw_string */
#include "../../../common/esp32/config.c"       /* NVS config struct + load/validate    */
#include "../../../common/esp32/state.c"  /* RTC-persistent state + validation    */
#include "../../../common/esp32/scheduler.c"  /* now_epoch / refresh_due / retry / drift cal */
#include "../../../common/esp32/nvs_cal.c" /* drift-calibration NVS persistence   */
#include "../../../common/esp32/log.c"    /* log ring + level gating              */
#include "../../../common/esp32/wifi.c"   /* WiFi connect + fast-reconnect cache  */
#include "../../../common/esp32/net.c"    /* HTTP image fetch + header capture    */
#include "../../../common/esp32/ota.c"    /* A/B OTA                             */
#include "../../../common/all/firmware_url.c" /* firmware endpoint derivation      */
#include "../../../common/all/backoff.c"      /* exponential retry backoff policy   */
#include "../../../common/all/frame_state.c"  /* X-Frame-State JSON builder         */
#include "../../../common/all/sleep_cal.c"    /* oscillator-drift calibration       */
#include "../../../common/all/json_util.c"    /* json_escape                        */
#include "../../../common/all/logbuf.c"       /* log buffer primitive (two-tier log)*/
#include "../../../common/all/frame_proto.c"   /* serial frame-upload protocol       */
#include "../../main/console.c"                  /* USB Serial/JTAG console + dispatch   */
#include "../../main/main.c"                     /* all firmware logic                   */

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

/* ═══════════════════════════════════════════════════════════════════════
 *  Logger (common/esp32/log.c) integration — a single RTC-resident ring, built
 *  on the shared logbuf primitive, written directly on every line so it
 *  survives sleep AND an unclean crash. Exercises init → hook-append →
 *  snapshot → reset, plus RTC survival across a simulated reboot (the ring is
 *  reconstructed from the persisted head/used). log_vprintf is the static hook,
 *  reachable here because `static` is neutralised.
 * ═══════════════════════════════════════════════════════════════════════ */
static int call_log(const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt);
    int n = log_vprintf(fmt, ap);
    va_end(ap);
    return n;
}

static void test_logger_ring_lifecycle(void)
{
    s_log_ring_head = 0;   /* fresh RTC ring, as after a clean POR boot */
    s_log_ring_used = 0;
    hokku_log_init();      /* reconstructs the ring + allocs format scratch */

    call_log("boot-line-A ");
    call_log("boot-line-B");
    /* Every line is written straight into the RTC ring AND its position is
     * persisted — so head/used already reflect the content (no spill needed). */
    CHECK(s_log_ring_used > 0, "logger: append persists ring position to RTC each line");

    char body[HOKKU_LOG_MAX_UPLOAD];
    size_t n = hokku_log_snapshot(body, sizeof(body));
    body[n] = '\0';
    CHECK(strcmp(body, "boot-line-A boot-line-B") == 0,
          "logger: snapshot returns the ring contents");

    /* Simulate a reboot WITHOUT a clean reset (i.e. a crash): the RAM logbuf_t
     * is lost, but s_log_ring storage + head/used survived in RTC. Re-init must
     * reconstruct the ring and recover the pre-crash logs. */
    hokku_log_init();
    call_log("+after-crash");
    n = hokku_log_snapshot(body, sizeof(body)); body[n] = '\0';
    CHECK(strcmp(body, "boot-line-A boot-line-B+after-crash") == 0,
          "logger: pre-crash logs survive an unclean reboot (RTC ring)");

    hokku_log_reset();     /* simulate a successful (HTTP 200) upload */
    n = hokku_log_snapshot(body, sizeof(body)); body[n] = '\0';
    CHECK(n == 0 && s_log_ring_used == 0, "logger: reset clears the ring after upload");
}

/* ── `frame` upload over the USB Serial/JTAG console ──────────────────────
 *
 * Two properties matter more than the happy path.
 *
 * The console "busy" flag gates the USB_AWAKE regime's restarts. A frame that
 * sets it without clearing it leaves a device that can never refresh or reboot
 * on schedule again, which on a wall-mounted screen looks like a dead unit.
 *
 * And a transfer that fails must leave the glass ALONE. This board buffers the
 * whole 960 KB in PSRAM precisely so the CRC can be checked before the panel is
 * touched; a half-written picture during colour measurement is worse than no
 * picture, because it is measurable and wrong rather than obviously absent. */

static void frame_test_reset(void)
{
    _mock_usb_avail = 0;
    _mock_usb_delivered = 0;
    _mock_usb_reads = 0;
    _mock_usb_tx_len = 0;
    _mock_usb_tx_dropped = 0;
    _mock_usb_install_result = ESP_OK;
    _mock_usb_install_calls = 0;
    s_busy = false;
    _mock_gpio[PIN_EPAPER_BUSY] = 1;   /* controller idle, so waits return at once */
}

/* Did the device write this control line? The capture is raw bytes, not a C
 * string, so search rather than strstr. */
static int wire_contains(const char *needle)
{
    size_t n = strlen(needle);
    uint32_t i;

    if (n > _mock_usb_tx_len)
        return 0;
    for (i = 0; i + n <= _mock_usb_tx_len; i++) {
        if (memcmp(_mock_usb_tx + i, needle, n) == 0)
            return 1;
    }
    return 0;
}

static uint32_t wire_count_byte(uint8_t b)
{
    uint32_t i, c = 0;
    for (i = 0; i < _mock_usb_tx_len; i++)
        if (_mock_usb_tx[i] == b)
            c++;
    return c;
}

static void test_frame_rejects_when_already_busy(void)
{
    frame_test_reset();
    s_busy = true;                      /* a transfer is already in flight */
    _mock_usb_avail = TOTAL_IMAGE_SIZE;

    CHECK(hokku_frame_receive() != 0, "frame: refused while another is in progress");
    CHECK(_mock_usb_reads == 0, "frame: does not touch the wire when refused");
    CHECK(!wire_contains(FRAME_PROTO_READY), "frame: no READY when refused");
}

static void test_frame_leaves_panel_untouched_when_host_dies(void)
{
    frame_test_reset();
    _mock_usb_avail = FRAME_PROTO_CHUNK_BYTES + 7;   /* one chunk, then silence */

    CHECK(hokku_frame_receive() != 0, "frame: truncated transfer reports failure");
    CHECK(!wire_contains(FRAME_PROTO_DONE), "frame: no DONE on a short read");
    CHECK(!wire_contains(FRAME_PROTO_REFRESHED),
          "frame: panel untouched when the host vanishes mid-transfer");
    CHECK(!hokku_console_busy(), "frame: busy flag released on the failure path");
}

static void test_frame_complete_transfer_acks_and_refreshes(void)
{
    uint32_t chunks = frame_proto_chunk_count(TOTAL_IMAGE_SIZE, FRAME_PROTO_CHUNK_BYTES);

    frame_test_reset();
    _mock_usb_avail = TOTAL_IMAGE_SIZE;

    CHECK(hokku_frame_receive() == 0, "frame: complete transfer succeeds");
    CHECK(_mock_usb_delivered == TOTAL_IMAGE_SIZE, "frame: consumes the whole image");
    CHECK(wire_count_byte(FRAME_PROTO_ACK) >= chunks, "frame: one ACK per chunk");
    CHECK(wire_contains(FRAME_PROTO_READY), "frame: announced READY");
    CHECK(wire_contains(FRAME_PROTO_DONE), "frame: reported DONE with a CRC");
    CHECK(wire_contains(FRAME_PROTO_REFRESHED), "frame: reported REFRESHED");
    CHECK(!hokku_console_busy(), "frame: busy flag released on the success path");
}

static void test_console_ping_identifies_the_board(void)
{
    char line[] = "ping";

    frame_test_reset();
    handle_line(line);
    CHECK(wire_contains("PONG"), "console: ping answers PONG");
    CHECK(wire_contains("huessen_epf1301"), "console: ping names the model");
}

static void test_console_rejects_unknown_command(void)
{
    char line[] = "framez";

    frame_test_reset();
    handle_line(line);
    CHECK(wire_contains("ERR"), "console: unknown command rejected");
    CHECK(_mock_usb_reads == 0, "console: unknown command starts no transfer");
}

int main(void)
{
    /* Unbuffered: when a test crashes the harness rather than failing a CHECK,
     * a block-buffered stdout discards every PASS line printed so far and the
     * run looks like it produced nothing at all. The last line printed is the
     * cheapest possible pointer at where it died. */
    setvbuf(stdout, NULL, _IONBF, 0);

    /* All mock GPIO pins start at 0 (LOW). Set defaults appropriate for the
     * firmware's expected hardware idle state. */
    memset(_mock_gpio, 0, sizeof(_mock_gpio));
    _mock_gpio[PIN_USB_DETECT] = 1;  /* no USB host */
    _mock_gpio[PIN_BUTTON_1]   = 1;  /* button released */
    _mock_gpio[PIN_EPAPER_BUSY]= 1;  /* display not busy */

    printf("=== test_logic ===\n\n");

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

    /* Logger (single RTC ring) */
    test_logger_ring_lifecycle();

    /* Serial `frame` upload */
    test_frame_rejects_when_already_busy();
    test_frame_leaves_panel_untouched_when_host_dies();
    test_frame_complete_transfer_acks_and_refreshes();
    test_console_ping_identifies_the_board();
    test_console_rejects_unknown_command();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
