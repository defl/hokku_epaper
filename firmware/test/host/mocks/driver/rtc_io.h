#pragma once
#include <stdint.h>

static inline int  rtc_gpio_init(int pin)               { (void)pin; return 0; }
static inline int  rtc_gpio_pullup_en(int pin)          { (void)pin; return 0; }
static inline int  rtc_gpio_isolate(int pin)            { (void)pin; return 0; }
static inline int  rtc_gpio_hold_en(int pin)            { (void)pin; return 0; }
static inline int  rtc_gpio_hold_dis(int pin)           { (void)pin; return 0; }
static inline int  rtc_gpio_deinit(int pin)             { (void)pin; return 0; }
static inline int  rtc_gpio_set_level(int pin, uint32_t level) { (void)pin; (void)level; return 0; }
static inline int  rtc_gpio_is_valid_gpio(int pin)      { (void)pin; return 1; }
