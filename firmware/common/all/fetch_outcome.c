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

int32_t hokku_sleep_seconds_parse(const char *value)
{
    if (value == NULL)
        return 0;

    const char *p = value;
    while (*p == ' ' || *p == '\t')
        p++;
    if (*p < '0' || *p > '9')
        return 0;

    int32_t secs = 0;
    while (*p >= '0' && *p <= '9') {
        if (secs < HOKKU_SLEEP_MAX_S)      /* stop growing once clamped: no overflow */
            secs = secs * 10 + (*p - '0');
        p++;
    }
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')
        p++;
    if (*p != '\0')
        return 0;

    return secs > HOKKU_SLEEP_MAX_S ? HOKKU_SLEEP_MAX_S : secs;
}
