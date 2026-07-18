#include "scheduler.h"
#include "state.h"

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
