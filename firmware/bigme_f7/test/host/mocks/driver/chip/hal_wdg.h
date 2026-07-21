#pragma once
#include <stdint.h>
#include "hal_def.h"

#define WDG_EVT_TYPE_SHIFT 0
typedef enum { WDG_EVT_RESET_CPU = 0, WDG_EVT_RESET = 1, WDG_EVT_INTERRUPT = 2 } WDG_EventType;
#define WDG_TIMEOUT_SHIFT 4
typedef enum {
    WDG_TIMEOUT_500MS = 0, WDG_TIMEOUT_1SEC, WDG_TIMEOUT_2SEC, WDG_TIMEOUT_3SEC,
    WDG_TIMEOUT_4SEC, WDG_TIMEOUT_5SEC, WDG_TIMEOUT_6SEC, WDG_TIMEOUT_8SEC,
    WDG_TIMEOUT_10SEC, WDG_TIMEOUT_12SEC, WDG_TIMEOUT_14SEC, WDG_TIMEOUT_16SEC,
} WDG_Timeout;
#define WDG_DEFAULT_RESET_CYCLE 0xA

typedef struct {
    WDG_EventType event;
    WDG_Timeout   timeout;
    uint8_t       resetCycle;
} WDG_HwInitParam;

typedef struct {
    WDG_HwInitParam hw;
} WDG_InitParam;

/* ── Controllable/recording mock state ──────────────────────────────────── */
static int         _mock_wdg_init_called;
static int         _mock_wdg_start_called;
static int         _mock_wdg_stop_called;
static int         _mock_wdg_reboot_called;
static WDG_InitParam _mock_wdg_init_last;

static inline HAL_Status HAL_WDG_Init(const WDG_InitParam *param)
{ _mock_wdg_init_last = *param; _mock_wdg_init_called++; return HAL_OK; }
static inline void HAL_WDG_Start(void) { _mock_wdg_start_called++; }
static inline void HAL_WDG_Stop(void) { _mock_wdg_stop_called++; }
static inline void HAL_WDG_Reboot(void) { _mock_wdg_reboot_called++; }
