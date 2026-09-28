#include "frame_state.h"

#include <stdio.h>
#include <string.h>

/* Render a 64-bit value without relying on printf's "%lld".
 *
 * The XR872/F7 firmware links newlib-nano (--specs=nano.specs), which is built
 * without _WANT_IO_LONG_LONG, so "%lld" is not understood. That doesn't just
 * mangle one field: once the length modifier misparses, the varargs go out of
 * alignment and EVERY later field is garbage, so the X-Frame-State header left
 * the device unusable and the server silently dropped it (no battery, no
 * Details, and a bogus "predates OTA" since the ota flag lives in this JSON).
 * The ESP32 boards link full newlib, which is why only the F7 was affected.
 *
 * Formatting by hand and emitting with %s keeps the full 64-bit range (a cast
 * to long would truncate epoch seconds in 2038) and is identical on every
 * platform. */
static void frame_state_i64(char *out, size_t outlen, long long v)
{
    char               digits[24];
    size_t             i = 0;
    size_t             n = 0;
    int                neg = (v < 0);
    unsigned long long u;

    /* Negate via unsigned so LLONG_MIN doesn't overflow. */
    u = neg ? (unsigned long long)(-(v + 1)) + 1ULL : (unsigned long long)v;

    do {
        digits[i++] = (char)('0' + (unsigned)(u % 10ULL));
        u /= 10ULL;
    } while (u != 0ULL && i < sizeof(digits));

    if (neg && n + 1 < outlen) {
        out[n++] = '-';
    }
    while (i > 0 && n + 1 < outlen) {
        out[n++] = digits[--i];
    }
    if (outlen > 0) {
        out[n] = '\0';
    }
}

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

    /* cal_ppm is optional: a calibrated device emits ,"cal_ppm":N after
     * sleep_err_s; an uncalibrated one omits it entirely so the server never
     * folds a placeholder 0 into the pinned long-term mean. */
    char calfield[24];
    if (fs->cal_known) {
        snprintf(calfield, sizeof(calfield), ",\"cal_ppm\":%d", fs->cal_ppm);
    } else {
        calfield[0] = '\0';
    }

    /* 64-bit fields are pre-rendered — see frame_state_i64 for why not "%lld". */
    char uptime_buf[24];
    char clk_buf[24];
    char next_ep_buf[24];
    frame_state_i64(uptime_buf, sizeof(uptime_buf), fs->uptime_s);
    frame_state_i64(clk_buf, sizeof(clk_buf), fs->clk_now);
    frame_state_i64(next_ep_buf, sizeof(next_ep_buf), fs->next_ep);

    snprintf(buf, buflen,
        "{\"fw\":\"%s\",\"boot\":%u,\"wake\":\"%s\",\"regime\":\"%s\","
        "\"uptime_s\":%s%s,\"usb\":\"%s\","
        "\"last_sleep\":\"%s\",\"rssi\":%d,\"heap_kb\":%u,"
        "\"spurious\":%u,\"cfg_ver\":%u,\"clk_now\":%s,"
        "\"next_ep\":%s,\"sleep_err_s\":%s%s,\"wifi_cached\":%s,"
        "\"ota\":1}",
        fs->fw, fs->boot, fs->wake, fs->regime,
        uptime_buf, batfield, fs->usb,
        fs->last_sleep, fs->rssi, fs->heap_kb,
        fs->spurious, fs->cfg_ver, clk_buf,
        next_ep_buf, sleep_err_buf, calfield,
        fs->wifi_cached ? "true" : "false");
}
