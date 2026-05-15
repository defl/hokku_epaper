#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#ifndef ESP_OK
typedef int esp_err_t;
#define ESP_OK ((esp_err_t)0)
#endif

typedef uint32_t nvs_handle_t;
typedef enum { NVS_READONLY = 0, NVS_READWRITE = 1 } nvs_open_mode_t;

#define ESP_ERR_NVS_NO_FREE_PAGES     0x1100
#define ESP_ERR_NVS_NEW_VERSION_FOUND 0x1101

/* Controllable mock state — set these before calling config_load() in tests.
 * Default: NVS namespace not found (open fails). */
static int     _mock_nvs_open_fail          = 1;
static uint8_t _mock_nvs_cfg_ver            = 0;
static uint8_t _mock_nvs_wifi_order         = 0;
static char    _mock_nvs_wifi_ssid[2][33]   = {{0}, {0}};
static char    _mock_nvs_wifi_pass[2][65]   = {{0}, {0}};
static char    _mock_nvs_image_url[257]     = {0};
static char    _mock_nvs_screen_name[65]    = {0};

static inline int nvs_flash_init(void)  { return 0; }
static inline int nvs_flash_erase(void) { return 0; }

static inline int nvs_open(const char *ns, nvs_open_mode_t m, nvs_handle_t *h) {
    (void)ns; (void)m;
    if (_mock_nvs_open_fail) return -1;
    *h = 1;
    return 0;
}

static inline int nvs_get_u8(nvs_handle_t h, const char *k, uint8_t *v) {
    (void)h;
    if (strcmp(k, "cfg_ver")    == 0) { *v = _mock_nvs_cfg_ver;    return 0; }
    if (strcmp(k, "wifi_order") == 0) { *v = _mock_nvs_wifi_order; return 0; }
    return -1;
}

static inline int nvs_get_str(nvs_handle_t h, const char *k, char *v, size_t *l) {
    (void)h;
    const char *src = NULL;
    if      (strcmp(k, "wifi_ssid1")  == 0) src = _mock_nvs_wifi_ssid[0];
    else if (strcmp(k, "wifi_pass1")  == 0) src = _mock_nvs_wifi_pass[0];
    else if (strcmp(k, "wifi_ssid2")  == 0) src = _mock_nvs_wifi_ssid[1];
    else if (strcmp(k, "wifi_pass2")  == 0) src = _mock_nvs_wifi_pass[1];
    else if (strcmp(k, "image_url")   == 0) src = _mock_nvs_image_url;
    else if (strcmp(k, "screen_name") == 0) src = _mock_nvs_screen_name;
    if (!src) return -1;
    strncpy(v, src, *l);
    if (*l > 0) v[*l - 1] = '\0';
    return 0;
}

static inline void nvs_close(nvs_handle_t h) { (void)h; }
