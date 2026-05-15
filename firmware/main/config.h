#pragma once

#include <stdbool.h>
#include <stdint.h>

#define CONFIG_VERSION  2

/* WiFi connection order strategy, stored as wifi_order in NVS */
#define WIFI_ORDER_PRIMARY_FIRST  0  /* always try slot 0 first */
#define WIFI_ORDER_LAST_FIRST     1  /* try whichever network last succeeded first */

typedef struct {
    uint8_t cfg_ver;
    uint8_t wifi_order;             /* WIFI_ORDER_* */
    char wifi_ssid[2][33];          /* [0]=primary (required), [1]=secondary (optional) */
    char wifi_pass[2][65];
    char image_url[257];
    char screen_name[65];
} config_t;

extern config_t config;

bool config_load(void);
bool config_version_ok(void);
bool config_is_valid(void);
