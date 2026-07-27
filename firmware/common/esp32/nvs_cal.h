// Non-volatile persistence for the deep-sleep drift calibration.
//
// The learned correction (cal_ppm/cal_samples, see state.h) lives in RTC memory
// across deep sleep and esp_restart(), but RTC is lost on true power removal. This
// module mirrors it to a SEPARATE read-write NVS namespace ("hokku_cal") so a
// battery swap doesn't discard days of learning.
//
// It is deliberately NOT the provisioned "hokku" config namespace: that one is
// read-only to the app and is raw-rewritten wholesale by OTA config migration
// (ota.c), which would wipe a value stored there. A separate namespace also means
// the config schema version (cfg_ver) never has to change for calibration.
#pragma once

/* Load the persisted calibration from NVS into the RTC globals cal_ppm/cal_samples.
 * Call once on a cold POR (see hokku_state_validate), AFTER nvs_flash_init(). A
 * missing namespace/keys leaves the globals at 0/0 (uncalibrated). */
void hokku_cal_load(void);

/* Persist the current RTC cal_ppm/cal_samples to NVS, but only when cal_ppm has
 * moved meaningfully since the last stored value (flash-wear guard), or when
 * nothing is stored yet. Safe to call every cycle. */
void hokku_cal_save_if_changed(void);
