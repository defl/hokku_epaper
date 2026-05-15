#pragma once

#include <stdbool.h>
#include <stdint.h>

#define CONFIG_VERSION  1

typedef struct {
    uint8_t cfg_ver;
    char wifi_ssid[33];
    char wifi_pass[65];
    char image_url[257];
    char screen_name[65];
} config_t;

extern config_t config;

bool config_load(void);
bool config_is_valid(void);
