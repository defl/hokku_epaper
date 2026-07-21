// Unit tests for firmware_url_build (pure). Covers both the /hokku/-anchor path
// and the raw fallback, plus buffer-bound safety.
#include <string.h>

#include "test_harness.h"
#include "../firmware_url.c"

static const char *build(const char *base, const char *leaf)
{
    static char out[256];
    firmware_url_build(out, sizeof(out), base, leaf);
    return out;
}

static void test_normal_screen_endpoint(void)
{
    CHECK(strcmp(build("http://host:8080/hokku/screen/", "firmware.bin"),
                 "http://host:8080/hokku/firmware.bin") == 0,
          "firmware_url: derives sibling from .../hokku/screen/");
}

static void test_model_query_leaf(void)
{
    CHECK(strcmp(build("http://host/hokku/screen/", "firmware.bin?model=seeedstudio_e1004"),
                 "http://host/hokku/firmware.bin?model=seeedstudio_e1004") == 0,
          "firmware_url: leaf may carry a query string (model tag)");
}

static void test_config_leaf(void)
{
    CHECK(strcmp(build("http://host/hokku/screen/", "firmware-config"),
                 "http://host/hokku/firmware-config") == 0,
          "firmware_url: firmware-config leaf");
}

static void test_no_trailing_slash(void)
{
    CHECK(strcmp(build("http://host/hokku/screen", "firmware.bin"),
                 "http://host/hokku/firmware.bin") == 0,
          "firmware_url: works without a trailing slash on the base");
}

static void test_fallback_when_no_hokku(void)
{
    /* Matches the F7 test's documented behaviour: unrecognised URL -> raw base. */
    CHECK(strcmp(build("http://unusual-host/x/y", "firmware.bin"),
                 "http://unusual-host/x/y") == 0,
          "firmware_url: falls back to raw base when /hokku/ is absent");
}

static void test_bound_safety(void)
{
    char out[10];
    firmware_url_build(out, sizeof(out), "http://host/hokku/screen/", "firmware.bin");
    CHECK(strlen(out) < sizeof(out), "firmware_url: never overflows a small out buffer");
}

int main(void)
{
    test_normal_screen_endpoint();
    test_model_query_leaf();
    test_config_leaf();
    test_no_trailing_slash();
    test_fallback_when_no_hokku();
    test_bound_safety();
    TEST_MAIN_END();
}
