#pragma once
#include "FreeRTOS.h"

typedef void *TaskHandle_t;
typedef void (*TaskFunction_t)(void *);

static inline void vTaskDelay(TickType_t d) { (void)d; }
static inline BaseType_t xTaskCreate(TaskFunction_t f, const char *n, uint32_t s,
                                     void *p, UBaseType_t pri, TaskHandle_t *h) {
    (void)f; (void)n; (void)s; (void)p; (void)pri; (void)h; return pdTRUE;
}
static inline void vTaskDelete(TaskHandle_t h) { (void)h; }
