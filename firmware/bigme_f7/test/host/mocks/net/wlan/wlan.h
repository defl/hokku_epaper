#pragma once
#include <stdint.h>

enum wlan_mode { WLAN_MODE_STA = 0, WLAN_MODE_HOSTAP, WLAN_MODE_MONITOR, WLAN_MODE_NUM };

#define WLAN_STA_CONF_FLAG_MFP  (1U << 1)
#define WLAN_STA_CONF_FLAG_SAE  (1U << 2)
#define WLAN_STA_CONF_FLAG_WPA3 (WLAN_STA_CONF_FLAG_MFP | WLAN_STA_CONF_FLAG_SAE)

typedef struct wlan_sta_ap {
    int rssi; /* real field is "unit is 0.5db"; main.c casts through int8_t */
} wlan_sta_ap_t;

/* ── Controllable mock state ──────────────────────────────────────────── */
static int _mock_wlan_sta_ap_info_result; /* 0 = success */
static int _mock_wlan_sta_ap_rssi;
static int _mock_wlan_sta_config_result;
static int _mock_wlan_sta_enable_result;

/* Call trace of the station control functions, in order, so tests can assert
 * the sequence (e.g. disable -> config -> enable), not just that each ran. */
enum { MOCK_WLAN_DISABLE = 1, MOCK_WLAN_CONFIG, MOCK_WLAN_ENABLE };
#define MOCK_WLAN_CALLS_MAX 8
static int _mock_wlan_calls[MOCK_WLAN_CALLS_MAX];
static int _mock_wlan_call_count;
static uint8_t _mock_wlan_config_ssid[33];

static inline void _mock_wlan_record(int call)
{
    if (_mock_wlan_call_count < MOCK_WLAN_CALLS_MAX)
        _mock_wlan_calls[_mock_wlan_call_count] = call;
    _mock_wlan_call_count++;
}

static inline int wlan_sta_ap_info(wlan_sta_ap_t *ap)
{
    ap->rssi = _mock_wlan_sta_ap_rssi;
    return _mock_wlan_sta_ap_info_result;
}
static inline int wlan_sta_config(uint8_t *ssid, uint8_t ssid_len, uint8_t *psk, uint32_t flag)
{
    (void)psk; (void)flag;
    _mock_wlan_record(MOCK_WLAN_CONFIG);
    for (int i = 0; i < 33; i++)
        _mock_wlan_config_ssid[i] = (i < ssid_len && i < 32) ? ssid[i] : 0;
    return _mock_wlan_sta_config_result;
}
static inline int wlan_sta_enable(void) { _mock_wlan_record(MOCK_WLAN_ENABLE); return _mock_wlan_sta_enable_result; }
static inline int wlan_sta_disable(void) { _mock_wlan_record(MOCK_WLAN_DISABLE); return 0; }
