/*
 * Runtime state an XR872 hokku screen keeps across hibernation and power loss:
 * the shared schedule + drift calibration (common/all/schedule.h), the boot
 * counter, and the "freshly OTA'd, not confirmed yet" marker.
 *
 * Hibernation is a full reboot on this SoC, so unlike the ESP32's RTC memory
 * nothing survives it in RAM: the state lives in its own FDCM area in flash,
 * next to the config blob. FDCM appends each write into the area and erases it
 * only when full, and hokku_state_save() writes only when the record changed.
 * One write per fetch cycle (the sleep start always changes) into an 8 KB area
 * that holds 127 of these 64-byte records is one erase per 127 cycles: at 48
 * cycles a day an erase every ~2.6 days, centuries inside the flash's ~100k
 * erase cycles (a screen stuck on 30 s retries all day: still ~10 years).
 *
 * A torn or foreign record fails its CRC and the screen starts from zero
 * state: uncalibrated (the server's seed restores it on the next fetch), no
 * outage streak, nothing pending.
 */
#ifndef HOKKU_XR872_STATE_H
#define HOKKU_XR872_STATE_H

#include <stdint.h>

#include "schedule.h"

#define HOKKU_STATE_MAGIC    0x48535354U   /* 'HSST' */
/* Bump on any layout change, including hokku_sched_t's (schedule.h). */
#define HOKKU_STATE_VERSION  1U

typedef struct {
    uint32_t      magic;
    uint32_t      version;
    hokku_sched_t sched;
    uint32_t      boot_count;
    /* 0 = nothing pending; else 1 + the slot (image seq) an OTA just wrote and
     * booted into, which has not reached the server yet (main.c). */
    uint8_t       ota_pending;
    uint8_t       reserved[3];
    uint32_t      crc;            /* CRC-32 of everything above */
} hokku_state_t;

/* Load the record from flash, or zero state when there is none / it is bad. */
void           hokku_state_load(void);
/* Live state. */
hokku_state_t *hokku_state_get(void);
/* Write the record when it differs from what is in flash. 0 on success or
 * nothing to do. */
int            hokku_state_save(void);

#endif /* HOKKU_XR872_STATE_H */
