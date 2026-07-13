#pragma once
#include <stdint.h>

typedef uint32_t OS_Time_t;
typedef void *OS_Handle_t;
#define OS_INVALID_HANDLE ((OS_Handle_t)0)
#define OS_WAIT_FOREVER   0xffffffffU

typedef enum {
    OS_OK        = 0,
    OS_FAIL      = -1,
    OS_E_NOMEM   = -2,
    OS_E_PARAM   = -3,
    OS_E_TIMEOUT = -4,
    OS_E_ISR     = -5,
} OS_Status;

typedef enum {
    OS_PRIORITY_IDLE         = 0,
    OS_PRIORITY_LOW          = 1,
    OS_PRIORITY_BELOW_NORMAL = 2,
    OS_PRIORITY_NORMAL       = 3,
    OS_PRIORITY_ABOVE_NORMAL = 4,
    OS_PRIORITY_HIGH         = 5,
    OS_PRIORITY_REAL_TIME    = 6,
} OS_Priority;
#define OS_THREAD_PRIO_APP OS_PRIORITY_NORMAL

typedef struct OS_Thread { OS_Handle_t handle; } OS_Thread_t;
typedef struct OS_Mutex  { OS_Handle_t handle; } OS_Mutex_t;
typedef void (*OS_ThreadEntry_t)(void *arg);

/* ── Controllable mock state ──────────────────────────────────────────── */
static uint32_t _mock_os_time_s = 1000;  /* value OS_GetTime() returns */
static int      _mock_thread_created;    /* count of OS_ThreadCreate calls */

static inline OS_Status OS_ThreadCreate(OS_Thread_t *thread, const char *name,
                                         OS_ThreadEntry_t entry, void *arg,
                                         OS_Priority priority, uint32_t stackSize)
{
    (void)name; (void)entry; (void)arg; (void)priority; (void)stackSize;
    thread->handle = (OS_Handle_t)1;
    _mock_thread_created++;
    return OS_OK;
}
static inline int OS_ThreadIsValid(OS_Thread_t *thread) { return thread->handle != OS_INVALID_HANDLE; }

static inline OS_Status OS_MutexCreate(OS_Mutex_t *mutex) { mutex->handle = (OS_Handle_t)1; return OS_OK; }
static inline int OS_MutexIsValid(OS_Mutex_t *mutex) { return mutex->handle != OS_INVALID_HANDLE; }
static inline OS_Status OS_MutexLock(OS_Mutex_t *mutex, OS_Time_t waitMS) { (void)mutex; (void)waitMS; return OS_OK; }
static inline OS_Status OS_MutexUnlock(OS_Mutex_t *mutex) { (void)mutex; return OS_OK; }

static inline void OS_MSleep(OS_Time_t msec) { (void)msec; }
static inline OS_Time_t OS_GetTime(void) { return _mock_os_time_s; }
