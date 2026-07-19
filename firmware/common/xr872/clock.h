// Software wall-clock for XR872 hokku screens, anchored to the server epoch
// (X-Server-Time-Epoch) so a screen can report clk_now without an RTC. Good
// enough for the always-on/loop cadence; hokku_clock_now() returns 0 until the
// first sync. Shared by all XR872/XR872AT screens.
#pragma once

#include <stdint.h>

void     hokku_clock_set(uint32_t server_epoch);  // anchor to the server's epoch
uint32_t hokku_clock_now(void);                    // current epoch estimate, or 0 if unsynced
