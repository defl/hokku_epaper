#pragma once
/* Test-only version defines for the common/esp32 host tests. The shared
 * config.h includes "version.h" and derives CONFIG_VERSION from
 * FW_CONFIG_VERSION; the firmwares generate this from their VERSION file, but
 * the common modules have no version of their own, so the tests supply fixed
 * values. */
#define FW_PROTOCOL_VERSION 1
#define FW_CONFIG_VERSION   1
#define FW_BUILD_N          0
#define FW_VERSION_STRING   "test"
#define FW_BUILD_TIMESTAMP  "test"
