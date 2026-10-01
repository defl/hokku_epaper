#include "ota.h"
#include "config.h"        /* current NVS config for the migration round-trip */
#include "json_util.h"     /* json_escape */
#include "firmware_url.h"  /* firmware_url_build */
#include "net.h"           /* HTTP_TIMEOUT_MS */

#include <string.h>
#include <stdlib.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_http_client.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "nvs_flash.h"
#include "esp_log.h"

#define PROGRESS(msg) do { if (progress) progress(msg); } while (0)

/* Build the X-Config-State header: the device's current NVS config as JSON so
 * the server can migrate it forward into the new firmware's schema. */
static void build_config_state_json(char *out, size_t outlen)
{
    char s1[2 * 33], p1[2 * 65], s2[2 * 33], p2[2 * 65], url[2 * 257], name[2 * 65];
    json_escape(s1,   sizeof(s1),   config.wifi_ssid[0]);
    json_escape(p1,   sizeof(p1),   config.wifi_pass[0]);
    json_escape(s2,   sizeof(s2),   config.wifi_ssid[1]);
    json_escape(p2,   sizeof(p2),   config.wifi_pass[1]);
    json_escape(url,  sizeof(url),  config.image_url);
    json_escape(name, sizeof(name), config.screen_name);
    snprintf(out, outlen,
        "{\"wifi_ssid1\":\"%s\",\"wifi_pass1\":\"%s\","
        "\"wifi_ssid2\":\"%s\",\"wifi_pass2\":\"%s\","
        "\"image_url\":\"%s\",\"screen_name\":\"%s\","
        "\"wifi_order\":%u,\"cfg_ver\":%u}",
        s1, p1, s2, p2, url, name,
        (unsigned)config.wifi_order, (unsigned)config.cfg_ver);
}

/* ── fetch migrated NVS config image into a RAM buffer ── */
typedef struct { uint8_t *buf; size_t len; size_t cap; bool ok; } ota_buf_ctx_t;

static esp_err_t ota_buf_event_handler(esp_http_client_event_t *evt)
{
    ota_buf_ctx_t *ctx = (ota_buf_ctx_t *)evt->user_data;
    if (!ctx) return ESP_OK;
    if (evt->event_id == HTTP_EVENT_ON_CONNECTED) {
        ctx->len = 0;  /* reset on (re)connect so a redirect doesn't accumulate */
    } else if (evt->event_id == HTTP_EVENT_ON_DATA && evt->data_len > 0) {
        if (ctx->len + (size_t)evt->data_len > ctx->cap) { ctx->ok = false; return ESP_FAIL; }
        memcpy(ctx->buf + ctx->len, evt->data, evt->data_len);
        ctx->len += evt->data_len;
    }
    return ESP_OK;
}

static bool ota_fetch_config(const char *base_url, const char *screen_name,
                             const char *screen_model, uint8_t **out, size_t *out_len)
{
    char url[257 + 32];
    firmware_url_build(url, sizeof(url), base_url, "firmware-config");
    char cfgstate[1280];
    build_config_state_json(cfgstate, sizeof(cfgstate));

    size_t cap = 64 * 1024;
    uint8_t *buf = malloc(cap);
    if (!buf) { ESP_LOGE("hokku", "OTA: config buffer OOM"); return false; }
    ota_buf_ctx_t ctx = { .buf = buf, .len = 0, .cap = cap, .ok = true };

    esp_http_client_config_t cfg = {
        .url = url, .event_handler = ota_buf_event_handler, .user_data = &ctx,
        .timeout_ms = HTTP_TIMEOUT_MS, .buffer_size = 4096,
        /* X-Config-State is up to 1280 bytes (cfgstate[]) — a SINGLE header far
         * over the 512-byte default TX buffer. If one header exceeds the TX
         * buffer, esp_http_client can't make progress and silently drops it and
         * every header after it. Must exceed the whole X-Config-State header. */
        .buffer_size_tx = 2048,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) { free(buf); return false; }
    if (screen_name && screen_name[0] != '\0')
        esp_http_client_set_header(client, "X-Screen-Name", screen_name);
    if (screen_model && screen_model[0] != '\0')
        esp_http_client_set_header(client, "X-Screen-Model", screen_model);
    char mac_str[18];
    hokku_screen_mac_str(mac_str, sizeof(mac_str));
    if (mac_str[0] != '\0')
        esp_http_client_set_header(client, "X-Screen-Mac", mac_str);
    esp_http_client_set_header(client, "X-Config-State", cfgstate);

    esp_err_t perr = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (perr != ESP_OK || status != 200 || !ctx.ok || ctx.len == 0) {
        ESP_LOGE("hokku", "OTA config fetch failed: err=%s status=%d ok=%d len=%u",
                 esp_err_to_name(perr), status, (int)ctx.ok, (unsigned)ctx.len);
        free(buf);
        return false;
    }
    ESP_LOGI("hokku", "OTA migrated config received: %u bytes", (unsigned)ctx.len);
    *out = buf;
    *out_len = ctx.len;
    return true;
}

/* ── stream the app image into the inactive OTA slot ── */
typedef struct { esp_ota_handle_t handle; size_t written; bool ok; } ota_write_ctx_t;

static esp_err_t ota_app_event_handler(esp_http_client_event_t *evt)
{
    ota_write_ctx_t *ctx = (ota_write_ctx_t *)evt->user_data;
    if (!ctx) return ESP_OK;
    if (evt->event_id == HTTP_EVENT_ON_DATA && ctx->ok && evt->data_len > 0) {
        esp_err_t e = esp_ota_write(ctx->handle, evt->data, evt->data_len);
        if (e != ESP_OK) {
            ESP_LOGE("hokku", "esp_ota_write failed: %s", esp_err_to_name(e));
            ctx->ok = false;
            return e;
        }
        ctx->written += evt->data_len;
    }
    return ESP_OK;
}

/* Download + write + validate the app image into the next OTA partition.
 * Does NOT set the boot partition — that happens after the NVS flash. */
static bool ota_write_app(const char *base_url, const char *screen_name,
                          const char *screen_model)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *update  = esp_ota_get_next_update_partition(NULL);
    if (!update) { ESP_LOGE("hokku", "OTA: no next update partition"); return false; }
    ESP_LOGI("hokku", "OTA target: %s @ 0x%lx (running: %s)",
             update->label, (unsigned long)update->address,
             running ? running->label : "?");

    esp_ota_handle_t handle = 0;
    esp_err_t e = esp_ota_begin(update, OTA_SIZE_UNKNOWN, &handle);
    if (e != ESP_OK) { ESP_LOGE("hokku", "esp_ota_begin: %s", esp_err_to_name(e)); return false; }

    char url[257 + 32];
    firmware_url_build(url, sizeof(url), base_url, "firmware.bin");
    ota_write_ctx_t ctx = { .handle = handle, .written = 0, .ok = true };
    esp_http_client_config_t cfg = {
        .url = url, .event_handler = ota_app_event_handler, .user_data = &ctx,
        .timeout_ms = HTTP_TIMEOUT_MS, .buffer_size = 4096,
        /* Raise TX buffer past the 512-byte default for our request headers —
         * see the note on the config-fetch client above. */
        .buffer_size_tx = 1024,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) { esp_ota_abort(handle); return false; }
    if (screen_name && screen_name[0] != '\0')
        esp_http_client_set_header(client, "X-Screen-Name", screen_name);
    if (screen_model && screen_model[0] != '\0')
        esp_http_client_set_header(client, "X-Screen-Model", screen_model);
    esp_err_t perr = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (perr != ESP_OK || status != 200 || !ctx.ok) {
        ESP_LOGE("hokku", "OTA app download failed: err=%s status=%d ok=%d written=%u",
                 esp_err_to_name(perr), status, (int)ctx.ok, (unsigned)ctx.written);
        esp_ota_abort(handle);
        return false;
    }
    e = esp_ota_end(handle);  /* validates the image (magic, checksum, signature) */
    if (e != ESP_OK) { ESP_LOGE("hokku", "esp_ota_end (validate): %s", esp_err_to_name(e)); return false; }
    ESP_LOGI("hokku", "OTA app written + validated: %u bytes", (unsigned)ctx.written);
    return true;
}

/* Rewrite the NVS partition with a server-provided partition image (same bytes
 * the USB setup path flashes). NVS is released first; we restart right after. */
static bool ota_flash_nvs(const uint8_t *img, size_t len)
{
    const esp_partition_t *nvs_part =
        esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_NVS, NULL);
    if (!nvs_part) { ESP_LOGE("hokku", "OTA: NVS partition not found"); return false; }
    if (len == 0 || len > nvs_part->size) {
        ESP_LOGE("hokku", "OTA: NVS image size %u invalid (partition %u)",
                 (unsigned)len, (unsigned)nvs_part->size);
        return false;
    }
    nvs_flash_deinit();  /* release before raw rewrite; ignore error if not init'd */
    esp_err_t e = esp_partition_erase_range(nvs_part, 0, nvs_part->size);
    if (e != ESP_OK) { ESP_LOGE("hokku", "OTA: NVS erase: %s", esp_err_to_name(e)); return false; }
    e = esp_partition_write(nvs_part, 0, img, len);
    if (e != ESP_OK) { ESP_LOGE("hokku", "OTA: NVS write: %s", esp_err_to_name(e)); return false; }

    uint8_t *check = malloc(len);
    if (check) {
        bool match = (esp_partition_read(nvs_part, 0, check, len) == ESP_OK)
                     && (memcmp(check, img, len) == 0);
        free(check);
        if (!match) { ESP_LOGE("hokku", "OTA: NVS verify mismatch"); return false; }
    }
    ESP_LOGI("hokku", "OTA: NVS partition rewritten (%u bytes)", (unsigned)len);
    return true;
}

bool perform_ota(const char *target_version, const char *base_url,
                 const char *screen_name, const char *screen_model,
                 ota_progress_fn progress)
{
    ESP_LOGI("hokku", "OTA starting -> %s", target_version);
    PROGRESS("Updating firmware...\n\nDo not unplug.\nThe screen will\nrestart itself.");

    /* 1. Migrated NVS config (held in RAM; flashed only after the app image is
     *    downloaded + validated, so a failed download never touches NVS). */
    uint8_t *nvs_img = NULL;
    size_t   nvs_len = 0;
    if (!ota_fetch_config(base_url, screen_name, screen_model, &nvs_img, &nvs_len)) {
        PROGRESS("Firmware update\nfailed.\n\n(config migration)\n\nWill retry later.");
        return false;
    }

    /* 2. App image -> inactive slot (validated by esp_ota_end). */
    if (!ota_write_app(base_url, screen_name, screen_model)) {
        free(nvs_img);
        PROGRESS("Firmware update\nfailed.\n\n(download)\n\nWill retry later.");
        return false;
    }

    /* 3. Rewrite NVS with the migrated config. */
    if (!ota_flash_nvs(nvs_img, nvs_len)) {
        free(nvs_img);
        PROGRESS("Firmware update\nfailed.\n\n(config write)\n\nWill retry later.");
        return false;
    }
    free(nvs_img);

    /* 4. Flip the boot partition and restart into the new slot. */
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    esp_err_t e = esp_ota_set_boot_partition(next);
    if (e != ESP_OK) {
        ESP_LOGE("hokku", "esp_ota_set_boot_partition: %s", esp_err_to_name(e));
        PROGRESS("Firmware update\nfailed.\n\n(commit)\n\nWill retry later.");
        return false;
    }
    ESP_LOGI("hokku", "OTA complete — rebooting into %s", next ? next->label : "new slot");
    PROGRESS("Update complete.\n\nRestarting...");
    vTaskDelay(pdMS_TO_TICKS(800));
    esp_restart();
    return true;  /* unreachable */
}

void ota_mark_valid_if_pending(void)
{
#ifdef CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t st;
    if (running && esp_ota_get_state_partition(running, &st) == ESP_OK
        && st == ESP_OTA_IMG_PENDING_VERIFY) {
        esp_err_t e = esp_ota_mark_app_valid_cancel_rollback();
        ESP_LOGI("hokku", "OTA pending-verify confirmed -> mark valid: %s", esp_err_to_name(e));
    }
#endif
}

bool ota_is_pending_verify(void)
{
#ifdef CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t st;
    return running && esp_ota_get_state_partition(running, &st) == ESP_OK
        && st == ESP_OTA_IMG_PENDING_VERIFY;
#else
    return false;
#endif
}
