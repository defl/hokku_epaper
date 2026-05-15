#pragma once
/* Stub sys/time.h for Windows/MSVC where it does not exist.
 * Provides struct timeval and settimeofday used by the firmware source. */

#include <time.h>

#ifndef _STRUCT_TIMEVAL
#define _STRUCT_TIMEVAL
struct timeval {
    time_t tv_sec;
    long   tv_usec;
};
#endif

static inline int settimeofday(const struct timeval *tv, const void *tz) {
    (void)tv; (void)tz; return 0;
}

/* POSIX gmtime_r — MSVC only has gmtime_s with reversed arg order. */
static inline struct tm *gmtime_r(const time_t *t, struct tm *result) {
    gmtime_s(result, t);  /* available on MSVC; on Linux real sys/time.h is used */
    return result;
}
