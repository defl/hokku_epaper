#pragma once
#include <stdint.h>

typedef enum {
    ESP_SLEEP_WAKEUP_UNDEFINED = 0,
    ESP_SLEEP_WAKEUP_EXT1      = 3,
    ESP_SLEEP_WAKEUP_TIMER     = 4,
    ESP_SLEEP_WAKEUP_GPIO      = 7,
} esp_sleep_source_t;

typedef esp_sleep_source_t esp_sleep_wakeup_cause_t;

typedef enum { ESP_EXT1_WAKEUP_ANY_LOW = 0 } esp_sleep_ext1_wakeup_mode_t;

static inline int  esp_sleep_enable_timer_wakeup(uint64_t t) { (void)t; return 0; }
static inline int  esp_sleep_enable_ext1_wakeup(uint64_t m, esp_sleep_ext1_wakeup_mode_t md) {
    (void)m; (void)md; return 0;
}
static inline void esp_deep_sleep_start(void) {}
static inline esp_sleep_source_t esp_sleep_get_wakeup_cause(void) {
    return ESP_SLEEP_WAKEUP_UNDEFINED;
}
static inline uint64_t esp_sleep_get_ext1_wakeup_status(void) { return 0; }
