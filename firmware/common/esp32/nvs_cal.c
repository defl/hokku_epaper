#include "nvs_cal.h"
#include "state.h"

#include "nvs_flash.h"
#include "esp_log.h"

#define HOKKU_CAL_NS         "hokku_cal"
#define HOKKU_CAL_WRITE_PPM  200   /* only rewrite when cal_ppm moved more than this */

void hokku_cal_load(void)
{
    nvs_handle_t nvs;
    if (nvs_open(HOKKU_CAL_NS, NVS_READONLY, &nvs) != ESP_OK) {
        return;   /* namespace not created yet -> stay uncalibrated (0/0) */
    }
    int32_t  ppm = 0;
    uint16_t n   = 0;
    if (nvs_get_i32(nvs, "cal_ppm", &ppm) == ESP_OK &&
        nvs_get_u16(nvs, "cal_samp", &n) == ESP_OK) {
        cal_ppm     = ppm;
        cal_samples = n;
        ESP_LOGI("hokku", "cal loaded from NVS: %d ppm, %u samples", (int)ppm, n);
    }
    nvs_close(nvs);
}

void hokku_cal_save_if_changed(void)
{
    nvs_handle_t nvs;
    if (nvs_open(HOKKU_CAL_NS, NVS_READWRITE, &nvs) != ESP_OK) return;

    int32_t  stored_ppm = 0;
    uint16_t stored_n   = 0;
    bool have = (nvs_get_i32(nvs, "cal_ppm", &stored_ppm) == ESP_OK &&
                 nvs_get_u16(nvs, "cal_samp", &stored_n) == ESP_OK);

    int32_t d = cal_ppm - stored_ppm;
    if (d < 0) d = -d;

    /* Write on first-ever store, once a fresh device crosses into "calibrated",
     * or when the value drifted past the wear threshold. */
    bool changed = !have || (stored_n == 0 && cal_samples > 0) || d > HOKKU_CAL_WRITE_PPM;
    if (changed) {
        nvs_set_i32(nvs, "cal_ppm", cal_ppm);
        nvs_set_u16(nvs, "cal_samp", cal_samples);
        nvs_commit(nvs);
        ESP_LOGI("hokku", "cal saved to NVS: %d ppm, %u samples", (int)cal_ppm, cal_samples);
    }
    nvs_close(nvs);
}
