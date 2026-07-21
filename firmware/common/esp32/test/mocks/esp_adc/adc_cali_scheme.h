#pragma once
#include "adc_oneshot.h"
#include "adc_cali.h"

typedef struct {
    adc_unit_t    unit_id;
    adc_atten_t   atten;
    adc_bitwidth_t bitwidth;
} adc_cali_curve_fitting_config_t;

static inline int adc_cali_create_scheme_curve_fitting(const adc_cali_curve_fitting_config_t *c,
                                                        adc_cali_handle_t *h) {
    (void)c; *h = (void *)1; return 0;
}
static inline int adc_cali_delete_scheme_curve_fitting(adc_cali_handle_t h) {
    (void)h; return 0;
}
