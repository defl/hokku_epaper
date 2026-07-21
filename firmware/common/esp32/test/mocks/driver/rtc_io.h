#pragma once
#include <stdint.h>

typedef enum {
    RTC_GPIO_MODE_INPUT_ONLY,
    RTC_GPIO_MODE_OUTPUT_ONLY,
    RTC_GPIO_MODE_INPUT_OUTPUT,
    RTC_GPIO_MODE_DISABLED,
} rtc_gpio_mode_t;

static inline int  rtc_gpio_init(int pin)               { (void)pin; return 0; }
static inline int  rtc_gpio_set_direction(int pin, rtc_gpio_mode_t m) { (void)pin; (void)m; return 0; }
static inline int  rtc_gpio_pulldown_dis(int pin)       { (void)pin; return 0; }
static inline int  rtc_gpio_pullup_en(int pin)          { (void)pin; return 0; }
static inline int  rtc_gpio_isolate(int pin)            { (void)pin; return 0; }
static inline int  rtc_gpio_hold_en(int pin)            { (void)pin; return 0; }
static inline int  rtc_gpio_hold_dis(int pin)           { (void)pin; return 0; }
static inline int  rtc_gpio_deinit(int pin)             { (void)pin; return 0; }
static inline int  rtc_gpio_set_level(int pin, uint32_t level) { (void)pin; (void)level; return 0; }
static inline int  rtc_gpio_is_valid_gpio(int pin)      { (void)pin; return 1; }
