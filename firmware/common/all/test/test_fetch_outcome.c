// Unit tests for fetch_outcome (pure): what every screen does with the server's
// answer to an image fetch, including each reply the server actually sends.
#include "test_harness.h"

#include "../backoff.c"
#include "../fetch_outcome.c"

static hokku_fetch_outcome_t decide(int status, bool image_ok, int32_t sleep_s,
                                    unsigned prior)
{
    hokku_fetch_result_t r = { .http_status = status, .image_ok = image_ok,
                               .sleep_s = sleep_s, .ota_failed = false };
    return hokku_fetch_decide(&r, prior);
}

static void test_image(void)
{
    hokku_fetch_outcome_t o = decide(200, true, 3600, 4);
    CHECK(o.action == HOKKU_FETCH_DISPLAY, "200 + image: display");
    CHECK(o.sleep_s == 3600, "200 + image: sleep the header value");
    CHECK(o.failures == 0, "200 + image: outage streak cleared");
    CHECK(!o.first_failure, "200 + image: not a failure");

    o = decide(200, true, 0, 2);
    CHECK(o.action == HOKKU_FETCH_DISPLAY, "200 + image, no header: still displayed");
    CHECK(o.sleep_s == HOKKU_RETRY_BASE_S, "200 + image, no header: retry base");
    CHECK(o.failures == 0, "200 + image, no header: streak cleared");
}

static void test_broken_image(void)
{
    hokku_fetch_outcome_t o = decide(200, false, 3600, 0);
    CHECK(o.action == HOKKU_FETCH_BACKOFF, "200 + short body: backoff, not a sleep");
    CHECK(o.sleep_s == HOKKU_RETRY_BASE_S, "200 + short body: first backoff = base");
    CHECK(o.failures == 1 && o.first_failure, "200 + short body: starts a streak");
}

/* The server's three no-image replies (flask_app.py, screen endpoint). */
static void test_server_busy_503(void)
{
    hokku_fetch_outcome_t o = decide(503, false, 30, 3);
    CHECK(o.action == HOKKU_FETCH_KEEP, "503 converting: keep the picture");
    CHECK(o.sleep_s == 30, "503 converting: sleep the short busy retry");
    CHECK(o.failures == 0 && !o.first_failure, "503 converting: reachable, streak cleared");
}

static void test_label_filter_404(void)
{
    hokku_fetch_outcome_t o = decide(404, false, 21600, 1);
    CHECK(o.action == HOKKU_FETCH_KEEP, "404 no label match: keep the picture");
    CHECK(o.sleep_s == 21600, "404 no label match: sleep the normal interval");
    CHECK(o.failures == 0 && !o.first_failure, "404 no label match: no outage");
}

static void test_empty_library_404(void)
{
    hokku_fetch_outcome_t o = decide(404, false, 30, 0);
    CHECK(o.action == HOKKU_FETCH_KEEP, "404 empty library: keep the picture");
    CHECK(o.sleep_s == 30, "404 empty library: sleep the busy retry");
}

static void test_other_status_with_header(void)
{
    hokku_fetch_outcome_t o = decide(400, false, 30, 0);
    CHECK(o.action == HOKKU_FETCH_KEEP, "400 unknown model + header: keep, server answered");
}

static void test_no_header_is_outage(void)
{
    hokku_fetch_outcome_t o = decide(404, false, 0, 0);
    CHECK(o.action == HOKKU_FETCH_BACKOFF, "404 without header: outage");
    CHECK(o.first_failure, "404 without header: starts a streak");

    o = decide(500, false, 0, 2);
    CHECK(o.action == HOKKU_FETCH_BACKOFF, "500 without header: outage");
    CHECK(o.sleep_s == 4 * HOKKU_RETRY_BASE_S, "500 without header: third failure = 4x base");
    CHECK(o.failures == 3 && !o.first_failure, "500 without header: streak grows");
}

static void test_transport_failure(void)
{
    hokku_fetch_outcome_t o = decide(0, false, 0, 0);
    CHECK(o.action == HOKKU_FETCH_BACKOFF, "no response: backoff");
    CHECK(o.sleep_s == HOKKU_RETRY_BASE_S && o.failures == 1 && o.first_failure,
          "no response: first failure = base, streak 1");

    o = decide(-1, false, 0, 1);
    CHECK(o.action == HOKKU_FETCH_BACKOFF && o.sleep_s == 2 * HOKKU_RETRY_BASE_S,
          "negative status (SDK 'none'): backoff, doubled");

    o = decide(0, false, 0, 100);
    CHECK(o.sleep_s == HOKKU_RETRY_MAX_S, "long outage: capped");
    o = decide(0, false, 0, 255);
    CHECK(o.failures == 255, "streak saturates at 255");

    o = hokku_fetch_decide(NULL, 0);
    CHECK(o.action == HOKKU_FETCH_BACKOFF, "NULL result: backoff");
}

static void test_ota_failed(void)
{
    hokku_fetch_result_t r = { .http_status = 200, .image_ok = true,
                               .sleep_s = 3600, .ota_failed = true };
    hokku_fetch_outcome_t o = hokku_fetch_decide(&r, 0);
    CHECK(o.action == HOKKU_FETCH_BACKOFF, "failed OTA: backoff, image not shown");
    CHECK(o.failures == 1, "failed OTA: counts toward the streak");
}

static void test_sleep_parse(void)
{
    CHECK(hokku_sleep_seconds_parse("3600") == 3600, "parse: plain number");
    CHECK(hokku_sleep_seconds_parse(" 30\r\n") == 30, "parse: surrounding whitespace");
    CHECK(hokku_sleep_seconds_parse(NULL) == 0, "parse: absent");
    CHECK(hokku_sleep_seconds_parse("") == 0, "parse: empty");
    CHECK(hokku_sleep_seconds_parse("0") == 0, "parse: zero is invalid");
    CHECK(hokku_sleep_seconds_parse("-5") == 0, "parse: negative rejected");
    CHECK(hokku_sleep_seconds_parse("+5") == 0, "parse: sign rejected");
    CHECK(hokku_sleep_seconds_parse("12abc") == 0, "parse: trailing junk rejected");
    CHECK(hokku_sleep_seconds_parse("1 2") == 0, "parse: inner space rejected");
    CHECK(hokku_sleep_seconds_parse("abc") == 0, "parse: not a number");
    CHECK(hokku_sleep_seconds_parse("99999999999999999999") == HOKKU_SLEEP_MAX_S,
          "parse: huge value clamped, no overflow");
    CHECK(hokku_sleep_seconds_parse("2678400") == HOKKU_SLEEP_MAX_S, "parse: exactly the max");
}

static void test_image_size(void)
{
    CHECK(hokku_image_size_ok(192000, 192000), "size: exact body is the image");
    CHECK(!hokku_image_size_ok(191999, 192000), "size: one byte short is not");
    CHECK(!hokku_image_size_ok(192001, 192000), "size: one byte long is not");
    CHECK(!hokku_image_size_ok(0, 0), "size: nothing expected, nothing ok");
}

static void test_epoch_parse(void)
{
    CHECK(hokku_server_epoch_parse("1700000000") == 1700000000LL, "epoch: plain value");
    CHECK(hokku_server_epoch_parse(" 1700000000\r\n") == 1700000000LL, "epoch: whitespace");
    CHECK(hokku_server_epoch_parse(NULL) == 0, "epoch: absent");
    CHECK(hokku_server_epoch_parse("1500000000") == 0, "epoch: before 2020 rejected");
    CHECK(hokku_server_epoch_parse("-1700000000") == 0, "epoch: signed rejected");
    CHECK(hokku_server_epoch_parse("17e8") == 0, "epoch: junk rejected");
    CHECK(hokku_server_epoch_parse("4102444800") == 4102444800LL, "epoch: past 2038 kept (64-bit)");
    CHECK(hokku_server_epoch_parse("99999999999999999999999") > 0,
          "epoch: absurd value saturates instead of overflowing");
}

static void test_cal_seed_parse(void)
{
    int32_t ppm = 1, n = 1;
    CHECK(hokku_cal_seed_parse("-1234", "7", &ppm, &n) && ppm == -1234 && n == 7,
          "seed: negative ppm and a count");
    CHECK(hokku_cal_seed_parse(" 900 ", "0", &ppm, &n) && ppm == 900 && n == 0,
          "seed: whitespace, zero count");
    CHECK(!hokku_cal_seed_parse("900", NULL, &ppm, &n), "seed: count missing");
    CHECK(!hokku_cal_seed_parse("", "3", &ppm, &n), "seed: ppm empty");
    CHECK(!hokku_cal_seed_parse("12x", "3", &ppm, &n), "seed: ppm junk");
    CHECK(!hokku_cal_seed_parse("5", "-3", &ppm, &n), "seed: negative count");
    CHECK(hokku_cal_seed_parse("99999999999999", "3", &ppm, &n) && ppm == 1000000000,
          "seed: huge ppm saturates into int32");
}

int main(void)
{
    test_image();
    test_broken_image();
    test_server_busy_503();
    test_label_filter_404();
    test_empty_library_404();
    test_other_status_with_header();
    test_no_header_is_outage();
    test_transport_failure();
    test_ota_failed();
    test_sleep_parse();
    test_image_size();
    test_epoch_parse();
    test_cal_seed_parse();
    TEST_MAIN_END();
}
