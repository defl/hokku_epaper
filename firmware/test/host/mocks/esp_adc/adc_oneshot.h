#pragma once
#include <stdint.h>

typedef void *adc_oneshot_unit_handle_t;
typedef enum { ADC_UNIT_1 = 0, ADC_UNIT_2 = 1 } adc_unit_t;
typedef enum { ADC_CHANNEL_0 = 0, ADC_CHANNEL_4 = 4 } adc_channel_t;
typedef enum { ADC_BITWIDTH_DEFAULT = 0, ADC_BITWIDTH_12 = 12 } adc_bitwidth_t;
typedef enum { ADC_ATTEN_DB_0 = 0, ADC_ATTEN_DB_6 = 2, ADC_ATTEN_DB_12 = 3 } adc_atten_t;

typedef struct { adc_unit_t unit_id; } adc_oneshot_unit_init_cfg_t;
typedef struct { adc_atten_t atten; adc_bitwidth_t bitwidth; } adc_oneshot_chan_cfg_t;

static inline int adc_oneshot_new_unit(const adc_oneshot_unit_init_cfg_t *c,
                                       adc_oneshot_unit_handle_t *h) {
    (void)c; *h = (void *)1; return 0;
}
static inline int adc_oneshot_config_channel(adc_oneshot_unit_handle_t h, adc_channel_t ch,
                                              const adc_oneshot_chan_cfg_t *c) {
    (void)h; (void)ch; (void)c; return 0;
}
static inline int adc_oneshot_read(adc_oneshot_unit_handle_t h, adc_channel_t ch, int *out) {
    (void)h; (void)ch; *out = 2000; return 0;
}
static inline int adc_oneshot_del_unit(adc_oneshot_unit_handle_t h) { (void)h; return 0; }
