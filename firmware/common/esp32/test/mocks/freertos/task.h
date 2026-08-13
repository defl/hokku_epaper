#pragma once
#include "FreeRTOS.h"

typedef void *TaskHandle_t;
typedef void (*TaskFunction_t)(void *);

/* Tick counter. Real FreeRTOS advances this on its own; here a test drives it,
 * because code that computes a deadline as `now + timeout` needs `now` to move
 * for the timeout to ever fire. vTaskDelay advances it too, so a delay loop
 * makes progress rather than spinning forever against a frozen clock. */
static TickType_t _mock_tick;

static inline TickType_t xTaskGetTickCount(void) { return _mock_tick; }
static inline void vTaskDelay(TickType_t d) { _mock_tick += d; }
static inline BaseType_t xTaskCreate(TaskFunction_t f, const char *n, uint32_t s,
                                     void *p, UBaseType_t pri, TaskHandle_t *h) {
    (void)f; (void)n; (void)s; (void)p; (void)pri; (void)h; return pdTRUE;
}
static inline void vTaskDelete(TaskHandle_t h) { (void)h; }
