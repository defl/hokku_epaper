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

static inline int wlan_sta_ap_info(wlan_sta_ap_t *ap)
{
    ap->rssi = _mock_wlan_sta_ap_rssi;
    return _mock_wlan_sta_ap_info_result;
}
static inline int wlan_sta_config(uint8_t *ssid, uint8_t ssid_len, uint8_t *psk, uint32_t flag)
{ (void)ssid; (void)ssid_len; (void)psk; (void)flag; return _mock_wlan_sta_config_result; }
static inline int wlan_sta_enable(void) { return _mock_wlan_sta_enable_result; }
