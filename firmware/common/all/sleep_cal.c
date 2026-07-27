#include "sleep_cal.h"

int32_t sleep_cal_clamp_ppm(int32_t cal_ppm)
{
    if (cal_ppm >  SLEEP_CAL_CLAMP_PPM) return  SLEEP_CAL_CLAMP_PPM;
    if (cal_ppm < -SLEEP_CAL_CLAMP_PPM) return -SLEEP_CAL_CLAMP_PPM;
    return cal_ppm;
}

sleep_cal_result_t sleep_cal_update(int32_t cal_ppm, uint16_t sample_count,
                                    int64_t actual_slept_s, int32_t armed_s)
{
    sleep_cal_result_t r = { .cal_ppm = cal_ppm, .updated = false };

    /* Reject short sleeps (noise) and non-sensical inputs. */
    if (armed_s < SLEEP_CAL_MIN_SAMPLE_S || actual_slept_s <= 0) {
        return r;
    }

    /* Reject a missed/doubled cycle: ratio actual/armed outside [0.5, 2.0].
     * Compared as cross-multiplied integers to avoid division. */
    if (actual_slept_s * 2 < (int64_t)armed_s ||
        actual_slept_s > (int64_t)armed_s * 2) {
        return r;
    }

    /* Observed correction in ppm: (actual - armed) / armed * 1e6. */
    int32_t obs_ppm = (int32_t)(((actual_slept_s - (int64_t)armed_s) * 1000000LL)
                                / (int64_t)armed_s);
    obs_ppm = sleep_cal_clamp_ppm(obs_ppm);

    /* EMA weight 1/den. Early samples (small count) use a larger weight so a
     * fresh device converges quickly; steady state settles at 1/ALPHA_DEN. */
    int32_t den = (int32_t)sample_count + 1;
    if (den > SLEEP_CAL_ALPHA_DEN) den = SLEEP_CAL_ALPHA_DEN;

    int32_t next = cal_ppm + (obs_ppm - cal_ppm) / den;

    r.cal_ppm = sleep_cal_clamp_ppm(next);
    r.updated = true;
    return r;
}

int64_t sleep_cal_apply_us(int32_t cal_ppm, int64_t desired_wall_s)
{
    if (desired_wall_s <= 0) return 1000000LL;

    cal_ppm = sleep_cal_clamp_ppm(cal_ppm);   /* keeps denominator well clear of 0 */

    /* armed = desired / (1 + cal/1e6) = desired * 1e6 / (1e6 + cal). Divide
     * before the final scale to keep the intermediate within int64 range. */
    int64_t armed_s = (desired_wall_s * 1000000LL) / (1000000LL + cal_ppm);
    if (armed_s < 1) armed_s = 1;
    return armed_s * 1000000LL;
}

bool sleep_cal_should_adopt(bool locally_calibrated, int server_n)
{
    return !locally_calibrated && server_n >= SLEEP_CAL_MIN_SEED_N;
}
