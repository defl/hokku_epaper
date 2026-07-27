#include "state.h"

#include <string.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"   /* RTC_NOINIT_ATTR */

RTC_NOINIT_ATTR uint32_t rtc_magic;
RTC_NOINIT_ATTR uint32_t boot_count;

RTC_NOINIT_ATTR uint8_t  wifi_channel;
RTC_NOINIT_ATTR uint8_t  wifi_bssid[6];
RTC_NOINIT_ATTR bool     has_wifi_cache;
RTC_NOINIT_ATTR uint8_t  last_wifi_index;

RTC_NOINIT_ATTR uint16_t last_battery_mv;

RTC_NOINIT_ATTR int32_t  last_sleep_seconds;
RTC_NOINIT_ATTR int64_t  next_refresh_epoch;
RTC_NOINIT_ATTR int64_t  pre_sleep_server_epoch;
RTC_NOINIT_ATTR int32_t  last_sleep_err_s;
RTC_NOINIT_ATTR bool     last_sleep_err_known;

RTC_NOINIT_ATTR int32_t  cal_ppm;
RTC_NOINIT_ATTR uint16_t cal_samples;
RTC_NOINIT_ATTR int32_t  last_armed_sleep_s;

RTC_NOINIT_ATTR uint8_t  consecutive_spurious_resets;
RTC_NOINIT_ATTR uint8_t  consecutive_refresh_failures;
RTC_NOINIT_ATTR uint8_t  last_sleep_mode;
RTC_NOINIT_ATTR uint8_t  pending_action;

RTC_NOINIT_ATTR char     s_log_ring[LOG_RING_SIZE];
RTC_NOINIT_ATTR uint16_t s_log_ring_head;
RTC_NOINIT_ATTR uint16_t s_log_ring_used;

const char *current_regime = "boot";

bool hokku_state_validate(void)
{
    if (rtc_magic != RTC_MAGIC) {
        rtc_magic = 0;
        boot_count = 0;
        wifi_channel = 0;
        memset(wifi_bssid, 0, sizeof(wifi_bssid));
        has_wifi_cache = false;
        last_wifi_index = 0;
        last_battery_mv = 0;
        last_sleep_seconds = 0;
        next_refresh_epoch = 0;
        pre_sleep_server_epoch = 0;
        last_sleep_err_s = 0;
        last_sleep_err_known = false;
        cal_ppm = 0;
        cal_samples = 0;
        last_armed_sleep_s = 0;
        consecutive_spurious_resets = 0;
        consecutive_refresh_failures = 0;
        last_sleep_mode = LAST_SLEEP_MODE_NONE;
        pending_action = ACTION_NONE;
        s_log_ring_head = 0;
        s_log_ring_used = 0;
        struct timeval tv = {0, 0};
        settimeofday(&tv, NULL);
        rtc_magic = RTC_MAGIC;  /* validate for the rest of this boot chain */
        return true;            /* cold POR: caller hydrates cal_ppm from NVS */
    }
    rtc_magic = RTC_MAGIC;  /* validate for the rest of this boot chain */
    return false;
}
