#pragma once
#include <stdint.h>

typedef void *adc_cali_handle_t;

static inline int adc_cali_raw_to_voltage(adc_cali_handle_t h, int raw, int *mv) {
    (void)h; *mv = (raw * 2200) / 4095; return 0;
}
