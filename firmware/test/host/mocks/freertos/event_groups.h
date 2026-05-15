#pragma once
#include "FreeRTOS.h"

typedef uint32_t EventBits_t;
typedef void    *EventGroupHandle_t;

static inline EventGroupHandle_t xEventGroupCreate(void) { return (void *)1; }
static inline EventBits_t xEventGroupSetBits(EventGroupHandle_t g, EventBits_t b) {
    (void)g; return b;
}
static inline EventBits_t xEventGroupClearBits(EventGroupHandle_t g, EventBits_t b) {
    (void)g; return b;
}
static inline EventBits_t xEventGroupWaitBits(EventGroupHandle_t g, EventBits_t bits,
                                               BaseType_t clear, BaseType_t all,
                                               TickType_t t) {
    (void)g; (void)bits; (void)clear; (void)all; (void)t; return 0;
}
