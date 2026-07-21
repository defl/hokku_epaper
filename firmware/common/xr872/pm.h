// Deep-sleep / hibernation for XR872 hokku screens. On battery a screen
// hibernates between refreshes and wakes via the HAL wake-timer into a full
// reboot. Shared by all XR872/XR872AT screens.
#pragma once

#include <stdint.h>

#define HOKKU_HIBERNATE_MIN_S   5U       /* floor: never hibernate < 5 s */
#define HOKKU_HIBERNATE_MAX_S   60000U   /* ceiling: HAL wake timer tops out ~18.6 h */

/* Clamp sleep_s to [MIN,MAX], arm the wake timer, and enter hibernation. Wakes
 * into a full reboot; does not return on success (falls back to a clean reboot
 * if pm_enter_mode somehow returns). */
void hokku_hibernate(uint32_t sleep_s);
