#include "schedule.h"
#include "sleep_cal.h"

#include <string.h>

void hokku_sched_init(hokku_sched_t *s)
{
    memset(s, 0, sizeof(*s));
}

/* Fold the sleep that just ended into the calibration. wake_epoch is the server
 * time at which this boot woke. One sample per sleep: the start is consumed by
 * the caller whatever happens here. */
static void observe_sleep(hokku_sched_t *s, int64_t wake_epoch)
{
    if (s->next_epoch > 0) {
        int64_t err = wake_epoch - s->next_epoch;
        if (err > INT32_MAX) err = INT32_MAX;
        if (err < INT32_MIN) err = INT32_MIN;
        s->sleep_err_s = (int32_t)err;
        s->sleep_err_known = 1;
    }
    if (s->armed_s > 0) {
        sleep_cal_result_t c = sleep_cal_update(s->cal_ppm, s->cal_samples,
                                                wake_epoch - s->sleep_start_epoch,
                                                s->armed_s);
        if (c.updated) {
            s->cal_ppm = c.cal_ppm;
            if (s->cal_samples < UINT16_MAX)
                s->cal_samples++;
        }
    }
}

void hokku_sched_retry_in(hokku_sched_t *s, int32_t secs, int64_t now_epoch, int64_t mono_us)
{
    if (now_epoch > 0)
        s->next_epoch = now_epoch + secs;
    else
        s->next_epoch = -(mono_us + (int64_t)secs * 1000000LL);
}

hokku_fetch_outcome_t hokku_sched_apply_fetch(hokku_sched_t *s,
                                              const hokku_fetch_result_t *r,
                                              const hokku_sched_now_t *now)
{
    hokku_fetch_outcome_t o = hokku_fetch_decide(r, s->failures);
    s->failures = (uint8_t)o.failures;

    int64_t server_epoch = r ? r->server_epoch : 0;

    /* The sleep that just ended, measured on the server clock. */
    if (now->timer_wake && server_epoch > 0 && s->sleep_start_epoch > 0)
        observe_sleep(s, server_epoch - now->awake_s);
    s->sleep_start_epoch = 0;

    bool reached = o.action != HOKKU_FETCH_BACKOFF;

    /* Cold start: a screen with no calibration of its own takes the server's
     * long-term mean for it, when enough measurements back it. */
    if (reached && r->cal_seed_n > 0 &&
        sleep_cal_should_adopt(s->cal_samples > 0, r->cal_seed_n)) {
        s->cal_ppm = sleep_cal_clamp_ppm(r->cal_seed_ppm);
        s->cal_samples = 1;   /* calibrated; real samples refine it */
    }

    s->sleep_s = o.sleep_s;
    if (reached && server_epoch > 0)
        s->next_epoch = server_epoch + o.sleep_s;   /* the server's clock */
    else
        hokku_sched_retry_in(s, o.sleep_s, now->now_epoch, now->mono_us);
    return o;
}

bool hokku_sched_due(const hokku_sched_t *s, int64_t now_epoch, int64_t mono_us)
{
    if (s->next_epoch == 0)
        return true;
    if (s->next_epoch < 0)
        return mono_us >= -s->next_epoch;
    return now_epoch > 0 && now_epoch >= s->next_epoch;
}

int64_t hokku_sched_remaining_s(const hokku_sched_t *s, int64_t now_epoch,
                                int64_t mono_us, int64_t fallback_s)
{
    int64_t secs;
    if (s->next_epoch > 0)
        secs = now_epoch > 0 ? s->next_epoch - now_epoch : s->sleep_s;
    else if (s->next_epoch < 0)
        secs = (-s->next_epoch - mono_us) / 1000000LL;
    else
        secs = fallback_s;
    return secs < 1 ? 1 : secs;
}

int64_t hokku_sched_arm_sleep(hokku_sched_t *s, int64_t now_epoch, int64_t mono_us,
                              int64_t fallback_s, int64_t max_s)
{
    int64_t secs = hokku_sched_remaining_s(s, now_epoch, mono_us, fallback_s);

    /* Only a sleep toward an epoch with the clock set is a drift sample, and
     * only that one is pre-distorted: a tick deadline or the fallback are short
     * or unanchored. */
    bool sample = s->next_epoch > 0 && now_epoch > 0;
    int64_t armed = sample ? sleep_cal_apply_us(s->cal_ppm, secs) / 1000000LL : secs;
    if (max_s > 0 && armed > max_s)
        armed = max_s;
    if (armed < 1)
        armed = 1;

    s->armed_s = sample ? (int32_t)armed : 0;
    s->sleep_start_epoch = sample ? now_epoch : 0;
    return armed;
}

int64_t hokku_sched_wake_estimate(const hokku_sched_t *s)
{
    if (s->sleep_start_epoch <= 0 || s->armed_s <= 0)
        return 0;
    int32_t ppm = sleep_cal_clamp_ppm(s->cal_ppm);
    return s->sleep_start_epoch +
           ((int64_t)s->armed_s * (1000000LL + ppm)) / 1000000LL;
}
