#pragma once
#include <stdint.h>

#define PM_WAKEUP_SRC_WKTIMER (1U << 10)

/* ── Controllable/recording mock state ──────────────────────────────────── */
static uint32_t _mock_wakeup_event;      /* what HAL_Wakeup_GetEvent() returns */
static uint32_t _mock_wakeup_timer_sec;  /* last value passed to SetTimer_Sec */

static inline uint32_t HAL_Wakeup_GetEvent(void) { return _mock_wakeup_event; }
/* Real macro is HAL_Wakeup_SetTimer((sec) * HAL_GetLFClock()); the mock just
 * records the seconds argument directly — hokku_hibernate's clamping logic
 * (5..60000) is what's under test, not the underlying timer-tick conversion. */
static inline void HAL_Wakeup_SetTimer_Sec(uint32_t sec) { _mock_wakeup_timer_sec = sec; }
