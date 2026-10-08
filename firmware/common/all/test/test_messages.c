// Unit tests for messages (pure): which error every screen draws, and when.
#include <string.h>

#include "test_harness.h"

#include "../backoff.c"
#include "../fetch_outcome.c"
#include "../messages.c"

#define URL   "http://hokku.local:8080/hokku/screen/"
#define HINT  "Press the button to\ntry again now."

static bool draw(const hokku_fetch_result_t *r, unsigned prior, char *buf, size_t len)
{
    hokku_fetch_outcome_t o = hokku_fetch_decide(r, prior);
    return hokku_msg_fetch_error(buf, len, r, &o, URL, HINT);
}

static void test_first_outage_draws(void)
{
    char buf[HOKKU_MSG_MAX];
    hokku_fetch_result_t down = { .http_status = 0 };
    CHECK(draw(&down, 0, buf, sizeof(buf)), "error: the first outage draws");
    CHECK(strstr(buf, "Image download failed.") && strstr(buf, URL) && strstr(buf, HINT),
          "error: download failure names the server and how to retry");

    hokku_fetch_result_t wifi = { .http_status = 0, .wifi_failed = true };
    CHECK(draw(&wifi, 0, buf, sizeof(buf)), "error: a WiFi failure draws");
    CHECK(strstr(buf, "WiFi connect failed.") && strstr(buf, HINT) && !strstr(buf, URL),
          "error: WiFi failure says WiFi, not the server");
}

static void test_quiet_cases(void)
{
    char buf[HOKKU_MSG_MAX];
    hokku_fetch_result_t down = { .http_status = 0 };
    CHECK(!draw(&down, 1, buf, sizeof(buf)), "quiet: later retries of a streak stay silent");

    hokku_fetch_result_t keep = { .http_status = 404, .sleep_s = 21600 };
    CHECK(!draw(&keep, 0, buf, sizeof(buf)), "quiet: a keep reply never draws");

    hokku_fetch_result_t img = { .http_status = 200, .image_ok = true, .sleep_s = 60 };
    CHECK(!draw(&img, 3, buf, sizeof(buf)), "quiet: an image is not an error");

    hokku_fetch_result_t ota = { .http_status = 200, .image_ok = true, .ota_failed = true };
    CHECK(!draw(&ota, 0, buf, sizeof(buf)), "quiet: a failed OTA already drew its own");
}

static void test_fits_with_longest_url(void)
{
    char url[257];
    memset(url, 'u', sizeof(url) - 1);
    url[sizeof(url) - 1] = '\0';
    char buf[HOKKU_MSG_MAX];
    hokku_fetch_result_t down = { .http_status = 0 };
    hokku_fetch_outcome_t o = hokku_fetch_decide(&down, 0);
    hokku_msg_fetch_error(buf, sizeof(buf), &down, &o, url,
                          "Turn the screen off\nand on again to\ntry again now.");
    CHECK(strstr(buf, "try again now.") != NULL,
          "error: a 256-byte URL and the longest hint fit HOKKU_MSG_MAX");
}

static void test_config_and_ota_texts(void)
{
    char buf[HOKKU_MSG_MAX];
    hokku_msg_config_version(buf, sizeof(buf), 3, 2);
    CHECK(strstr(buf, "Expected: 3") && strstr(buf, "Found: 2"), "config: version mismatch");
    hokku_msg_ota_failed(buf, sizeof(buf), "download");
    CHECK(strstr(buf, "(download)") && strstr(buf, "Will retry later."), "ota: failure stage");
}

int main(void)
{
    test_first_outage_draws();
    test_quiet_cases();
    test_fits_with_longest_url();
    test_config_and_ota_texts();
    TEST_MAIN_END();
}
