#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

typedef enum { WIFI_MODE_NULL = 0, WIFI_MODE_STA } wifi_mode_t;
typedef enum { WIFI_AUTH_OPEN = 0, WIFI_AUTH_WPA2_PSK = 3 } wifi_auth_mode_t;
typedef int wifi_interface_t;
#define WIFI_IF_STA ((wifi_interface_t)0)

typedef struct {
    struct {
        uint8_t ssid[32];
        uint8_t password[64];
        uint8_t bssid[6];
        bool    bssid_set;
        uint8_t channel;
        struct  { wifi_auth_mode_t authmode; } threshold;
    } sta;
} wifi_config_t;

typedef struct {
    int8_t  rssi;
    uint8_t ssid[33];
    uint8_t bssid[6];
    uint8_t primary;
} wifi_ap_record_t;

typedef struct { int _placeholder; } wifi_init_config_t;
#define WIFI_INIT_CONFIG_DEFAULT() {0}

static inline int esp_wifi_init(const wifi_init_config_t *c)           { (void)c; return 0; }
static inline int esp_wifi_set_mode(wifi_mode_t m)                     { (void)m; return 0; }
static inline int esp_wifi_set_config(wifi_interface_t i, wifi_config_t *c) {
    (void)i; (void)c; return 0;
}
static inline int esp_wifi_start(void)      { return 0; }
static inline int esp_wifi_connect(void)    { return 0; }
static inline int esp_wifi_disconnect(void) { return 0; }
static inline int esp_wifi_stop(void)       { return 0; }
static inline int esp_wifi_deinit(void)     { return 0; }
static inline int esp_wifi_sta_get_ap_info(wifi_ap_record_t *ap) { (void)ap; return -1; }
static inline int esp_wifi_get_mac(wifi_interface_t i, uint8_t *mac) {
    (void)i; (void)mac; return 0;
}
