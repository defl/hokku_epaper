#include "scheduler.h"
#include "state.h"
#include "nvs_cal.h"

#include "esp_timer.h"
#include "esp_log.h"

time_t now_epoch(void)
{
    time_t t = time(NULL);
    return (t < HOKKU_EPOCH_MIN) ? 0 : t;
}

bool refresh_due(void)
{
    return hokku_sched_due(&hokku_sched, (int64_t)now_epoch(), esp_timer_get_time());
}

hokku_fetch_outcome_t scheduler_apply_fetch(const hokku_fetch_result_t *res)
{
    int64_t mono = esp_timer_get_time();
    /* A timer wake esp_restart()s once before the fetch; that adds well under
     * a second to "awake since the wake", which is inside the sample noise. */
    hokku_sched_now_t now = {
        .now_epoch  = (int64_t)now_epoch(),
        .mono_us    = mono,
        .awake_s    = mono / 1000000LL,
        .timer_wake = (last_sleep_mode == LAST_SLEEP_MODE_TIMER_WAKE),
    };
    hokku_fetch_outcome_t o = hokku_sched_apply_fetch(&hokku_sched, res, &now);
    ESP_LOGI("hokku", "Fetch: status=%d -> %s, next in %d s (cal %d ppm, %u samples)",
             res ? res->http_status : 0, o.reason, (int)o.sleep_s,
             (int)hokku_sched.cal_ppm, (unsigned)hokku_sched.cal_samples);
    hokku_cal_save_if_changed();
    return o;
}

void scheduler_retry_in(int32_t secs)
{
    hokku_sched_retry_in(&hokku_sched, secs, (int64_t)now_epoch(), esp_timer_get_time());
}

int64_t scheduler_next_sleep_us(int64_t fallback_s)
{
    return hokku_sched_arm_sleep(&hokku_sched, (int64_t)now_epoch(), esp_timer_get_time(),
                                 fallback_s, 0) * 1000000LL;
}
