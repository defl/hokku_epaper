// When a screen fetches next, and how it corrects its sleep timer for
// oscillator drift. Shared by every hokku screen firmware: the same reply puts
// every board on the same wake time, and every board learns its drift the same
// way. Pure C (see README.md): the caller passes in its clocks and keeps the
// hokku_sched_t wherever its platform keeps state across a sleep (RTC memory on
// the ESP32 boards, a flash record on the Bigme F7).
//
// Lifecycle of one cycle:
//   1. hokku_sched_apply_fetch()  after each fetch: decide (fetch_outcome.h),
//      learn drift from the sleep that just ended, adopt the server's seed,
//      and set the next fetch time.
//   2. hokku_sched_arm_sleep()    at the sleep site: the timer to arm, with the
//      learned drift taken out; records what was armed and when.
//   For a screen that stays awake instead: hokku_sched_due() /
//   hokku_sched_remaining_s() against the running clock (no drift correction).
//
// The next fetch is an absolute server-clock time whenever the reply carried
// X-Server-Time-Epoch: next = server time + X-Sleep-Seconds (or the outcome's
// sleep). That keeps every screen on the server's schedule however long the
// fetch, the panel refresh or the boot took. Without a server time it is
// relative to the device clock, or to a monotonic tick when the clock is unset.
//
// Drift is measured against the server clock, never the device's own: the
// sleep started at sleep_start_epoch (device clock at arming, which a server
// reply set) and ended at wake = reply server time - seconds awake since the
// wake. A device clock that ran through the sleep on the drifting oscillator
// would only measure itself. One sample per sleep; see sleep_cal.h.
//
// LAYOUT: this struct is persisted raw (ESP32 RTC memory, the F7's flash state
// record). Changing it must also bump RTC_MAGIC (common/esp32/state.h) and
// HOKKU_STATE_VERSION (common/xr872/state.h) so stale bytes are not misread.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "fetch_outcome.h"

/* Sleep when nothing is scheduled at all (no server contact yet). */
#define HOKKU_FALLBACK_SLEEP_S  (3 * 3600)

typedef struct {
    /* Next fetch:
     *   0        not scheduled: due now (first boot, no server contact yet)
     *   > 0      absolute epoch seconds
     *   < 0      -(monotonic deadline, microseconds): the clock was unset */
    int64_t  next_epoch;
    /* Device epoch when the last timer sleep was armed; 0 = that sleep is not
     * a drift sample (clock unset, or not a timed sleep). Consumed by the next
     * hokku_sched_apply_fetch(). */
    int64_t  sleep_start_epoch;
    int32_t  armed_s;        /* timer armed for that sleep, seconds */
    int32_t  sleep_s;        /* the last schedule's interval, seconds */
    int32_t  sleep_err_s;    /* wake minus the scheduled time, last sample */
    int32_t  cal_ppm;        /* learned drift correction (sleep_cal.h) */
    uint16_t cal_samples;    /* accepted samples; 0 = not calibrated yet */
    uint8_t  failures;       /* outage streak (fetch_outcome.h) */
    uint8_t  sleep_err_known;
} hokku_sched_t;

/* Clocks at the moment of a call. */
typedef struct {
    int64_t now_epoch;   /* device clock, epoch seconds; 0 = unset */
    int64_t mono_us;     /* monotonic microseconds (tick-deadline fallback) */
    int64_t awake_s;     /* seconds since this boot woke */
    bool    timer_wake;  /* this boot is the wake of an armed timer sleep */
} hokku_sched_now_t;

/* Zero state: unscheduled, uncalibrated, no outage. */
void hokku_sched_init(hokku_sched_t *s);

/* Act on one fetch: run hokku_fetch_decide() with the stored outage streak,
 * store the new streak, and schedule the next fetch (see above). On a timer
 * wake whose reply carried the server time, fold the sleep that just ended into
 * the drift calibration. When the server was reached (DISPLAY or KEEP) and this
 * screen is not calibrated yet, adopt the server's seed. Drawing is the
 * caller's job. */
hokku_fetch_outcome_t hokku_sched_apply_fetch(hokku_sched_t *s,
                                              const hokku_fetch_result_t *r,
                                              const hokku_sched_now_t *now);

/* Schedule the next fetch secs from now, on the device clock (or a monotonic
 * tick when the clock is unset). For a screen that cannot fetch at all (its
 * config is unusable) and must try again later. */
void hokku_sched_retry_in(hokku_sched_t *s, int32_t secs, int64_t now_epoch, int64_t mono_us);

/* Whether the next fetch is due (always true when unscheduled). A scheduled
 * epoch with the clock unset is not due. */
bool hokku_sched_due(const hokku_sched_t *s, int64_t now_epoch, int64_t mono_us);

/* Wall seconds until the next fetch (>= 1), or fallback_s when unscheduled. */
int64_t hokku_sched_remaining_s(const hokku_sched_t *s, int64_t now_epoch,
                                int64_t mono_us, int64_t fallback_s);

/* Seconds to arm a sleep timer for now (>= 1): the remaining wall time with the
 * learned drift taken out, capped at max_s (0 = no cap; a board's timer
 * limit). Records armed_s and, when the device clock is set and the next fetch
 * is an epoch, sleep_start_epoch, so the wake can measure drift. */
int64_t hokku_sched_arm_sleep(hokku_sched_t *s, int64_t now_epoch, int64_t mono_us,
                              int64_t fallback_s, int64_t max_s);

/* Best estimate of the epoch at which the armed timer sleep ended (start +
 * armed, corrected by the learned drift), or 0 when the last sleep is not
 * known. Lets a board without a clock that runs through sleep restore one at a
 * timer wake. Not used for measuring drift. */
int64_t hokku_sched_wake_estimate(const hokku_sched_t *s);
