// Unit tests for frame_state_build (pure). Locks the X-Frame-State schema so
// all firmwares that feed it a frame_state_t emit identical wire output.
#include <string.h>

#include "test_harness.h"
#include "../frame_state.c"

static frame_state_t base_fs(void)
{
    frame_state_t fs = {
        .fw = "1.2.9", .boot = 7, .wake = "timer", .regime = "battery_idle",
        .uptime_s = 42, .bat_mv = 3980, .usb = "none", .last_sleep = "timer_wake",
        .rssi = -58, .heap_kb = 210, .spurious = 0, .cfg_ver = 3,
        .clk_now = 1700000000LL, .next_ep = 1700003600LL,
        .sleep_err_known = 1, .sleep_err_s = -3,
        .cal_known = 1, .cal_ppm = 1234, .wifi_cached = 1,
    };
    return fs;
}

static void test_full_object_exact(void)
{
    char buf[512];
    frame_state_t fs = base_fs();
    frame_state_build(buf, sizeof(buf), &fs);
    const char *expect =
        "{\"fw\":\"1.2.9\",\"boot\":7,\"wake\":\"timer\",\"regime\":\"battery_idle\","
        "\"uptime_s\":42,\"bat_mv\":3980,\"usb\":\"none\","
        "\"last_sleep\":\"timer_wake\",\"rssi\":-58,\"heap_kb\":210,"
        "\"spurious\":0,\"cfg_ver\":3,\"clk_now\":1700000000,"
        "\"next_ep\":1700003600,\"sleep_err_s\":-3,\"cal_ppm\":1234,\"wifi_cached\":true,"
        "\"ota\":1}";
    CHECK(strcmp(buf, expect) == 0, "frame_state: full object matches the locked schema exactly");
}

static void test_bat_mv_omitted_when_negative(void)
{
    char buf[512];
    frame_state_t fs = base_fs();
    fs.bat_mv = -1;
    frame_state_build(buf, sizeof(buf), &fs);
    CHECK(strstr(buf, "bat_mv") == NULL, "frame_state: bat_mv omitted when < 0 (unknown battery)");
    CHECK(strstr(buf, "\"uptime_s\":42,\"usb\":") != NULL,
          "frame_state: uptime_s is followed directly by usb when bat_mv omitted");
}

static void test_bat_mv_zero_is_emitted(void)
{
    char buf[512];
    frame_state_t fs = base_fs();
    fs.bat_mv = 0;                     /* 0 is a real reading, not 'unknown' */
    frame_state_build(buf, sizeof(buf), &fs);
    CHECK(strstr(buf, "\"bat_mv\":0") != NULL, "frame_state: bat_mv:0 is emitted (0 != unknown)");
}

static void test_sleep_err_null_when_unknown(void)
{
    char buf[512];
    frame_state_t fs = base_fs();
    fs.sleep_err_known = 0;
    frame_state_build(buf, sizeof(buf), &fs);
    CHECK(strstr(buf, "\"sleep_err_s\":null") != NULL,
          "frame_state: sleep_err_s is JSON null when unknown");
}

static void test_cal_ppm_omitted_when_unknown(void)
{
    char buf[512];
    frame_state_t fs = base_fs();
    fs.cal_known = 0;
    frame_state_build(buf, sizeof(buf), &fs);
    CHECK(strstr(buf, "cal_ppm") == NULL,
          "frame_state: cal_ppm omitted when uncalibrated");
    CHECK(strstr(buf, "\"sleep_err_s\":-3,\"wifi_cached\":") != NULL,
          "frame_state: sleep_err_s is followed directly by wifi_cached when cal_ppm omitted");
}

static void test_cal_ppm_negative(void)
{
    char buf[512];
    frame_state_t fs = base_fs();
    fs.cal_ppm = -8000;                /* fast oscillator */
    frame_state_build(buf, sizeof(buf), &fs);
    CHECK(strstr(buf, "\"cal_ppm\":-8000") != NULL,
          "frame_state: negative cal_ppm renders with sign");
}

static void test_wifi_cached_bool(void)
{
    char buf[512];
    frame_state_t fs = base_fs();
    fs.wifi_cached = 0;
    frame_state_build(buf, sizeof(buf), &fs);
    CHECK(strstr(buf, "\"wifi_cached\":false") != NULL, "frame_state: wifi_cached renders as a JSON bool");
}

static void test_always_ota_capable(void)
{
    char buf[512];
    frame_state_t fs = base_fs();
    frame_state_build(buf, sizeof(buf), &fs);
    CHECK(strstr(buf, "\"ota\":1}") != NULL, "frame_state: always advertises ota:1");
}

static void test_epoch_beyond_int32(void)
{
    /* clk_now/next_ep/uptime_s are 64-bit (long long / %lld). Use values past
     * INT32_MAX (2147483647) — epoch seconds exceed it after 2038 — so a future
     * regression narrowing them to int/%d would corrupt the output here. On a
     * 32-bit target that also garbles every following vararg field. */
    char buf[512];
    frame_state_t fs = base_fs();
    fs.uptime_s = 5000000000LL;     /* > 2^32 */
    fs.clk_now  = 4102444800LL;     /* 2100-01-01 */
    fs.next_ep  = 4102448400LL;
    frame_state_build(buf, sizeof(buf), &fs);
    CHECK(strstr(buf, "\"uptime_s\":5000000000") != NULL,
          "frame_state: uptime_s renders a value > 2^32 (64-bit width)");
    CHECK(strstr(buf, "\"clk_now\":4102444800") != NULL,
          "frame_state: clk_now renders a post-2038 epoch (64-bit width)");
    CHECK(strstr(buf, "\"next_ep\":4102448400") != NULL,
          "frame_state: next_ep renders a post-2038 epoch (64-bit width)");
}

int main(void)
{
    test_full_object_exact();
    test_bat_mv_omitted_when_negative();
    test_bat_mv_zero_is_emitted();
    test_sleep_err_null_when_unknown();
    test_cal_ppm_omitted_when_unknown();
    test_cal_ppm_negative();
    test_wifi_cached_bool();
    test_always_ota_capable();
    test_epoch_beyond_int32();
    TEST_MAIN_END();
}
