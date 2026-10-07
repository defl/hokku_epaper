#include "fetch_outcome.h"
#include "backoff.h"

#include <stddef.h>

#define HOKKU_FETCH_FAILURES_MAX 255u

static hokku_fetch_outcome_t fetch_backoff(unsigned prior_failures, const char *reason)
{
    hokku_fetch_outcome_t o;
    o.action        = HOKKU_FETCH_BACKOFF;
    o.sleep_s       = hokku_backoff_seconds(prior_failures, HOKKU_RETRY_BASE_S,
                                            HOKKU_RETRY_MAX_S);
    o.failures      = prior_failures < HOKKU_FETCH_FAILURES_MAX
                          ? prior_failures + 1 : HOKKU_FETCH_FAILURES_MAX;
    o.first_failure = (prior_failures == 0);
    o.reason        = reason;
    return o;
}

static hokku_fetch_outcome_t fetch_reached(hokku_fetch_action_t action, int32_t sleep_s,
                                           const char *reason)
{
    hokku_fetch_outcome_t o;
    o.action        = action;
    o.sleep_s       = sleep_s;
    o.failures      = 0;
    o.first_failure = false;
    o.reason        = reason;
    return o;
}

hokku_fetch_outcome_t hokku_fetch_decide(const hokku_fetch_result_t *r,
                                         unsigned prior_failures)
{
    if (r == NULL || r->http_status < 100)
        return fetch_backoff(prior_failures, "no response");
    if (r->ota_failed)
        return fetch_backoff(prior_failures, "ota failed");

    bool have_sleep = r->sleep_s > 0;

    if (r->http_status == 200) {
        if (!r->image_ok)
            return fetch_backoff(prior_failures, "incomplete image");
        if (have_sleep)
            return fetch_reached(HOKKU_FETCH_DISPLAY, r->sleep_s, "image");
        return fetch_reached(HOKKU_FETCH_DISPLAY, HOKKU_RETRY_BASE_S, "image without schedule");
    }

    if (have_sleep)
        return fetch_reached(HOKKU_FETCH_KEEP, r->sleep_s, "no image for this screen");
    return fetch_backoff(prior_failures, "error without schedule");
}

bool hokku_image_size_ok(size_t received, size_t expected)
{
    return expected > 0 && received == expected;
}

/* One plain decimal header value: optional surrounding whitespace, an optional
 * leading '-' when allow_neg, then digits and nothing else. The magnitude stops
 * growing once it passes cap, so a huge value saturates instead of overflowing.
 * Returns false for anything else. */
static bool parse_decimal(const char *value, bool allow_neg, int64_t cap, int64_t *out)
{
    if (value == NULL)
        return false;

    const char *p = value;
    while (*p == ' ' || *p == '\t')
        p++;
    bool neg = false;
    if (allow_neg && *p == '-') {
        neg = true;
        p++;
    }
    if (*p < '0' || *p > '9')
        return false;

    int64_t v = 0;
    while (*p >= '0' && *p <= '9') {
        if (v <= cap)
            v = v * 10 + (*p - '0');
        p++;
    }
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')
        p++;
    if (*p != '\0')
        return false;

    *out = neg ? -v : v;
    return true;
}

int32_t hokku_sleep_seconds_parse(const char *value)
{
    int64_t secs;
    if (!parse_decimal(value, false, HOKKU_SLEEP_MAX_S, &secs) || secs == 0)
        return 0;
    return secs > HOKKU_SLEEP_MAX_S ? HOKKU_SLEEP_MAX_S : (int32_t)secs;
}

int64_t hokku_server_epoch_parse(const char *value)
{
    int64_t epoch;
    /* Cap far past any real clock (year ~33658) yet far inside int64. */
    if (!parse_decimal(value, false, 1000000000000LL, &epoch) || epoch < HOKKU_EPOCH_MIN)
        return 0;
    return epoch;
}

bool hokku_cal_seed_parse(const char *ppm, const char *n,
                          int32_t *out_ppm, int32_t *out_n)
{
    int64_t p, k;
    if (!parse_decimal(ppm, true, 1000000000LL, &p) ||
        !parse_decimal(n, false, 1000000000LL, &k))
        return false;
    /* A saturated magnitude can reach 10 * cap + 9; clamp it into int32. */
    if (p > 1000000000LL) p = 1000000000LL;
    if (p < -1000000000LL) p = -1000000000LL;
    if (k > 1000000000LL) k = 1000000000LL;
    *out_ppm = (int32_t)p;
    *out_n   = (int32_t)k;
    return true;
}
