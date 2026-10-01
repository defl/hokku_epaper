#include "scheduler.h"
#include "state.h"
#include "sleep_cal.h"

#include "esp_timer.h"
#include "esp_log.h"

time_t now_epoch(void)
{
    time_t t = time(NULL);
    return (t < 1577836800) ? 0 : t;
}

bool refresh_due(void)
{
    if (next_refresh_epoch == 0) return true;
    if (next_refresh_epoch < 0)
        return esp_timer_get_time() >= -next_refresh_epoch;
    time_t now = now_epoch();
    if (now == 0) return false;           /* epoch-based delay set, but clock not yet synced */
    return now >= next_refresh_epoch;
}

void schedule_retry_in(int seconds, const char *reason)
{
    time_t now = now_epoch();
    if (now > 0) {
        next_refresh_epoch = (int64_t)now + seconds;
    } else {
        /* No epoch clock yet. Encode a tick-based deadline as a negative value
         * so refresh_due() can honour the delay without the clock.
         * esp_timer_get_time() is monotonic across esp_restart(). Real epochs
         * are ~1.7e9; tick deadlines for any plausible uptime fit in ~1e12 µs. */
        next_refresh_epoch = -(esp_timer_get_time() + (int64_t)seconds * 1000000LL);
    }
    /* No fresh pre_sleep_server_epoch either — clear it so the next boot
     * doesn't log a bogus sleep_err_s. */
    pre_sleep_server_epoch = 0;
    last_sleep_err_known = false;
    ESP_LOGW("hokku", "Refresh retry in %d s (%s)", seconds, reason);
}

void save_pre_sleep_epoch(int64_t server_epoch, int64_t local_time_at_download_us)
{
    if (server_epoch <= 0) {
        pre_sleep_server_epoch = 0;
        last_sleep_err_known = false;
        return;
    }
    int64_t delta_s = (esp_timer_get_time() - local_time_at_download_us) / 1000000LL;
    pre_sleep_server_epoch = server_epoch + delta_s;
}

bool scheduler_set_after_refresh(int64_t server_epoch, int32_t sleep_seconds,
                                 int64_t local_time_at_download_us)
{
    if (server_epoch <= 0 || sleep_seconds <= 0) return false;
    last_sleep_seconds = sleep_seconds;
    next_refresh_epoch = server_epoch + sleep_seconds;   /* absolute, drift-free */
    save_pre_sleep_epoch(server_epoch, local_time_at_download_us);
    return true;
}

void scheduler_observe_sleep(void)
{
    if (last_sleep_mode != LAST_SLEEP_MODE_TIMER_WAKE) return;
    if (pre_sleep_server_epoch <= 0 || last_sleep_seconds <= 0) return;

    time_t now = now_epoch();
    if (now <= 0) return;   /* clock not synced this boot — can't measure */

    int64_t actual = (int64_t)now - pre_sleep_server_epoch;

    /* Diagnostic: how far off the scheduled slot we landed (intended duration).
     * Trends toward 0 as calibration converges — the on-device health meter. */
    int64_t err = actual - last_sleep_seconds;
    if (err > INT32_MAX) err = INT32_MAX;
    if (err < INT32_MIN) err = INT32_MIN;
    last_sleep_err_s = (int32_t)err;
    last_sleep_err_known = true;

    /* Calibration: the oscillator ratio is actual vs what we actually ARMED
     * (last_armed_sleep_s), not the intended duration. Skipped for retry/fallback
     * sleeps, which leave last_armed_sleep_s == 0. */
    if (last_armed_sleep_s > 0) {
        sleep_cal_result_t r =
            sleep_cal_update(cal_ppm, cal_samples, actual, last_armed_sleep_s);
        if (r.updated) {
            cal_ppm = r.cal_ppm;
            if (cal_samples < 0xFFFF) cal_samples++;
        }
    }
}

int64_t scheduler_next_sleep_us(int64_t fallback_us)
{
    if (next_refresh_epoch > 0) {
        time_t now = now_epoch();
        int64_t secs = (now > 0) ? (next_refresh_epoch - (int64_t)now)
                                 : (int64_t)last_sleep_seconds;
        if (secs < 1) secs = 1;
        /* Pre-distort the wall interval by the learned drift so the actual sleep
         * lands on the slot. Record what we armed for the next wake's measurement. */
        int64_t armed_us = sleep_cal_apply_us(cal_ppm, secs);
        last_armed_sleep_s = (int32_t)(armed_us / 1000000LL);
        return armed_us;
    }
    if (next_refresh_epoch < 0) {
        /* Tick-based retry deadline (clock was unset when scheduled). Short and
         * not a clean drift sample — do not calibrate. */
        int64_t remain_us = -next_refresh_epoch - esp_timer_get_time();
        if (remain_us < 1000000LL) remain_us = 1000000LL;
        last_armed_sleep_s = 0;
        return remain_us;
    }
    last_armed_sleep_s = 0;
    return fallback_us;
}

bool scheduler_adopt_cal_seed(int32_t seed_ppm, int server_n)
{
    bool locally_calibrated = (cal_samples > 0);
    if (!sleep_cal_should_adopt(locally_calibrated, server_n)) return false;
    cal_ppm = sleep_cal_clamp_ppm(seed_ppm);
    cal_samples = 1;   /* mark calibrated; a real measurement will refine it */
    return true;
}
