#pragma once
#include <stdint.h>
#include "hal_def.h"

typedef enum { ADC_CHANNEL_4 = 4 } ADC_Channel;
typedef enum { ADC_CONTI_CONV = 2 } ADC_WorkMode;
typedef enum { ADC_VREF_MODE_1 = 1 } ADC_VrefMode;

typedef struct {
    uint32_t     freq;
    uint8_t      delay;
    uint8_t      suspend_bypass;
    ADC_VrefMode vref_mode;
    ADC_WorkMode mode;
} ADC_InitParam;

/* ── Controllable mock state (hokku_battery_mv reads _mock_adc_raw) ──────── */
static uint32_t   _mock_adc_raw;
static HAL_Status  _mock_adc_init_result;
static HAL_Status  _mock_adc_conv_result;

static inline HAL_Status HAL_ADC_Init(ADC_InitParam *p) { (void)p; return _mock_adc_init_result; }
static inline HAL_Status HAL_ADC_Conv_Polling(ADC_Channel chan, uint32_t *data, uint32_t msec)
{ (void)chan; (void)msec; *data = _mock_adc_raw; return _mock_adc_conv_result; }
