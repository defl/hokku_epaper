#pragma once
#include <stdint.h>

/* Controllable mock: set this before calling functions under test. */
static int64_t _mock_timer_us = 0;

static inline int64_t esp_timer_get_time(void) { return _mock_timer_us; }
