#include "frame_state.h"

#include <stdio.h>
#include <string.h>

void frame_state_build(char *buf, size_t buflen, const frame_state_t *fs)
{
    /* bat_mv is optional: a >=0 value emits ,"bat_mv":N right after uptime_s
     * (matching the huessen schema position); <0 omits it entirely (matching
     * the F7 "omit when unknown" behaviour). */
    char batfield[24];
    if (fs->bat_mv >= 0) {
        snprintf(batfield, sizeof(batfield), ",\"bat_mv\":%d", fs->bat_mv);
    } else {
        batfield[0] = '\0';
    }

    /* sleep_err_s: emitted as an int when known, else JSON null. */
    char sleep_err_buf[16];
    if (fs->sleep_err_known) {
        snprintf(sleep_err_buf, sizeof(sleep_err_buf), "%d", fs->sleep_err_s);
    } else {
        strcpy(sleep_err_buf, "null");
    }

    snprintf(buf, buflen,
        "{\"fw\":\"%s\",\"boot\":%u,\"wake\":\"%s\",\"regime\":\"%s\","
        "\"uptime_s\":%lld%s,\"usb\":\"%s\","
        "\"last_sleep\":\"%s\",\"rssi\":%d,\"heap_kb\":%u,"
        "\"spurious\":%u,\"cfg_ver\":%u,\"clk_now\":%lld,"
        "\"next_ep\":%lld,\"sleep_err_s\":%s,\"wifi_cached\":%s,"
        "\"ota\":1}",
        fs->fw, fs->boot, fs->wake, fs->regime,
        fs->uptime_s, batfield, fs->usb,
        fs->last_sleep, fs->rssi, fs->heap_kb,
        fs->spurious, fs->cfg_ver, fs->clk_now,
        fs->next_ep, sleep_err_buf,
        fs->wifi_cached ? "true" : "false");
}
