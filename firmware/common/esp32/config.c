#include "config.h"
#include "screen_ident.h"

#include <string.h>

#include "nvs_flash.h"
#include "esp_log.h"

config_t config = {0};

bool config_load(void)
{
    nvs_handle_t nvs;
    if (nvs_open("hokku", NVS_READONLY, &nvs) != ESP_OK) return false;
    nvs_get_u8(nvs, "cfg_ver",    &config.cfg_ver);
    nvs_get_u8(nvs, "wifi_order", &config.wifi_order);
    size_t len;
    len = sizeof(config.wifi_ssid[0]); nvs_get_str(nvs, "wifi_ssid1",  config.wifi_ssid[0], &len);
    len = sizeof(config.wifi_pass[0]); nvs_get_str(nvs, "wifi_pass1",  config.wifi_pass[0], &len);
    len = sizeof(config.wifi_ssid[1]); nvs_get_str(nvs, "wifi_ssid2",  config.wifi_ssid[1], &len);
    len = sizeof(config.wifi_pass[1]); nvs_get_str(nvs, "wifi_pass2",  config.wifi_pass[1], &len);
    len = sizeof(config.image_url);    nvs_get_str(nvs, "image_url",   config.image_url,    &len);
    len = sizeof(config.screen_name);  nvs_get_str(nvs, "screen_name", config.screen_name,  &len);
    nvs_close(nvs);
    return true;
}

bool config_version_ok(void)
{
    return config.cfg_ver == CONFIG_VERSION;
}

bool config_is_valid(void)
{
    return config_version_ok()
        && config.wifi_ssid[0][0] != '\0'
        && config.image_url[0] != '\0';
}

bool config_set_screen_name(const char *name)
{
    /* Same name first: the server sends it on every response. */
    if (name && strcmp(config.screen_name, name) == 0) return true;
    if (!hokku_screen_name_valid(name)) {
        ESP_LOGW("hokku", "rename refused: invalid name");
        return false;
    }

    nvs_handle_t nvs;
    if (nvs_open("hokku", NVS_READWRITE, &nvs) != ESP_OK) return false;
    bool ok = nvs_set_str(nvs, "screen_name", name) == ESP_OK && nvs_commit(nvs) == ESP_OK;
    nvs_close(nvs);
    if (!ok) {
        ESP_LOGE("hokku", "rename to '%s' failed to persist", name);
        return false;
    }
    ESP_LOGI("hokku", "renamed '%s' -> '%s'", config.screen_name, name);
    strncpy(config.screen_name, name, sizeof(config.screen_name) - 1);
    config.screen_name[sizeof(config.screen_name) - 1] = '\0';
    return true;
}
