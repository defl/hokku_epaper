#pragma once
#include <stdint.h>
#include "net/wlan/wlan.h"

#define SYSINFO_SSID_LEN_MAX (32)
#define SYSINFO_PSK_LEN_MAX  (65)

struct sysinfo_wlan_sta_param {
    uint8_t ssid[SYSINFO_SSID_LEN_MAX];
    uint8_t ssid_len;
    uint8_t psk[SYSINFO_PSK_LEN_MAX];
};

struct sysinfo {
    enum wlan_mode                wlan_mode;
    struct sysinfo_wlan_sta_param wlan_sta_param;
};

/* ── Controllable mock state ──────────────────────────────────────────── */
static struct sysinfo _mock_sysinfo_state;
static int            _mock_sysinfo_get_null; /* simulate "sysinfo unavailable" */
static int            _mock_sysinfo_save_result;
static int            _mock_sysinfo_save_call_count;

static inline struct sysinfo *sysinfo_get(void)
{ return _mock_sysinfo_get_null ? (struct sysinfo *)0 : &_mock_sysinfo_state; }
static inline int sysinfo_save(void) { _mock_sysinfo_save_call_count++; return _mock_sysinfo_save_result; }
