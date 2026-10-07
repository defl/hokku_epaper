#include "net.h"
#include "log.h"    /* hokku_log_snapshot / hokku_log_reset / HOKKU_LOG_MAX_UPLOAD */
#include "config.h" /* config_set_screen_name */
#include "screen_ident.h"
#include "http_headers.h"

#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <time.h>
#include <sys/time.h>

#include "esp_http_client.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_wifi.h"
#include "esp_log.h"

typedef struct {
    uint8_t *buf;
    size_t   kept;      /* bytes copied into buf (never more than capacity) */
    size_t   received;  /* every body byte that arrived, kept or not */
    size_t   capacity;
    /* Response-header captures, populated from HTTP_EVENT_ON_HEADER and read
     * after perform(). (Capturing from the event stream is the only correct
     * way — esp_http_client_get_header() reads REQUEST headers, not response.) */
    char     sleep_seconds_hdr[32];
    char     server_epoch_hdr[32];
    /* X-Firmware-Update: <version> — present when the server wants this device
     * to OTA. The body (image) is ignored when set. */
    char     fw_update_hdr[48];
    /* X-Sleep-Cal-PPM / X-Sleep-Cal-N — the server's pinned drift mean and how
     * many measurements back it (a cold-start seed; see sleep_cal). */
    char     cal_ppm_hdr[16];
    char     cal_n_hdr[16];
    /* X-Screen-Name: the server's name for this screen (set in its web UI).
     * One byte beyond the max so an over-long value stays over-long (and is
     * refused) instead of being silently truncated into a valid one. */
    char     name_hdr[HOKKU_SCREEN_NAME_MAX + 2];
} http_download_ctx_t;

static void capture(char *dst, size_t dstlen, const char *value)
{
    strncpy(dst, value, dstlen - 1);
    dst[dstlen - 1] = '\0';
}

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    http_download_ctx_t *ctx = (http_download_ctx_t *)evt->user_data;
    if (!ctx) return ESP_OK;

    switch (evt->event_id) {
        case HTTP_EVENT_ON_CONNECTED:
            /* Reset on each new connection (handles redirects): without this, a
             * 308 redirect's body would count toward the image. Header captures
             * reset too so we only see the final response's values. */
            ctx->kept = 0;
            ctx->received = 0;
            ctx->sleep_seconds_hdr[0] = '\0';
            ctx->server_epoch_hdr[0]  = '\0';
            ctx->fw_update_hdr[0]     = '\0';
            ctx->cal_ppm_hdr[0]       = '\0';
            ctx->cal_n_hdr[0]         = '\0';
            ctx->name_hdr[0]          = '\0';
            break;
        case HTTP_EVENT_ON_HEADER:
            if (evt->header_key && evt->header_value) {
                const char *k = evt->header_key, *v = evt->header_value;
                if (strcasecmp(k, HOKKU_HDR_SLEEP_SECONDS) == 0)
                    capture(ctx->sleep_seconds_hdr, sizeof(ctx->sleep_seconds_hdr), v);
                else if (strcasecmp(k, HOKKU_HDR_SERVER_TIME) == 0)
                    capture(ctx->server_epoch_hdr, sizeof(ctx->server_epoch_hdr), v);
                else if (strcasecmp(k, HOKKU_HDR_FW_UPDATE) == 0)
                    capture(ctx->fw_update_hdr, sizeof(ctx->fw_update_hdr), v);
                else if (strcasecmp(k, HOKKU_HDR_SLEEP_CAL_PPM) == 0)
                    capture(ctx->cal_ppm_hdr, sizeof(ctx->cal_ppm_hdr), v);
                else if (strcasecmp(k, HOKKU_HDR_SLEEP_CAL_N) == 0)
                    capture(ctx->cal_n_hdr, sizeof(ctx->cal_n_hdr), v);
                else if (strcasecmp(k, HOKKU_HDR_SCREEN_NAME) == 0)
                    capture(ctx->name_hdr, sizeof(ctx->name_hdr), v);
            }
            break;
        case HTTP_EVENT_ON_DATA:
            if (evt->data_len > 0) {
                size_t n = (size_t)evt->data_len;
                ctx->received += n;
                if (ctx->kept + n <= ctx->capacity) {
                    memcpy(ctx->buf + ctx->kept, evt->data, n);
                    ctx->kept += n;
                }
            }
            break;
        default:
            break;
    }
    return ESP_OK;
}

void hokku_screen_mac_str(char *out, size_t len)
{
    if (!out || len == 0) return;
    uint8_t mac[6] = {0};
    if (esp_wifi_get_mac(WIFI_IF_STA, mac) != ESP_OK) {
        out[0] = '\0';
        return;
    }
    hokku_mac_format(mac, out, len);
}

/* A reply that carries the server time sets the system clock (RTC-backed, so it
 * survives deep sleep + esp_restart); the next X-Frame-State reports it. */
static void set_clock(int64_t epoch)
{
    struct timeval tv = { .tv_sec = (time_t)epoch, .tv_usec = 0 };
    settimeofday(&tv, NULL);
    ESP_LOGI("hokku", "clock set from the server: %lld", (long long)epoch);
}

bool hokku_http_fetch_image(uint8_t *buf, size_t expect_bytes,
                            const char *url, const char *screen_name,
                            const char *screen_model, const char *frame_state,
                            const char *fw_build, hokku_fetch_result_t *res,
                            char *fw_update, size_t fw_update_len)
{
    http_download_ctx_t ctx = { .buf = buf, .capacity = expect_bytes };

    res->http_status = 0;
    res->image_ok    = false;
    if (fw_update && fw_update_len) fw_update[0] = '\0';

    esp_http_client_config_t http_cfg = {
        .url = url,
        .event_handler = http_event_handler,
        .user_data = &ctx,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .buffer_size = 4096,
        /* TX (request) buffer. The default is DEFAULT_HTTP_BUF_SIZE = 512,
         * which our request headers exceed: X-Frame-State alone is up to the
         * caller's ~384-byte JSON, on top of X-Screen-Name/Model and the two
         * firmware headers. esp_http_client tolerates that by writing headers
         * across several passes, but it logs an ESP_LOGE each time and — if any
         * SINGLE header ever exceeds this buffer — silently drops that header
         * and every one after it. Size it well past our largest single header. */
        .buffer_size_tx = 1024,
    };
    esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
    if (!client) {
        ESP_LOGE("hokku", "esp_http_client_init failed (OOM?)");
        return false;
    }

    /* POST so the ring-buffer log can travel as the request body. */
    esp_http_client_set_method(client, HTTP_METHOD_POST);

    if (screen_name && screen_name[0] != '\0')
        esp_http_client_set_header(client, HOKKU_HDR_SCREEN_NAME, screen_name);
    if (screen_model && screen_model[0] != '\0')
        esp_http_client_set_header(client, HOKKU_HDR_SCREEN_MODEL, screen_model);
    /* X-Screen-Mac: the server's durable per-device key (name is only a label). */
    char mac_str[HOKKU_MAC_STR_LEN];
    hokku_screen_mac_str(mac_str, sizeof(mac_str));
    if (mac_str[0] != '\0')
        esp_http_client_set_header(client, HOKKU_HDR_SCREEN_MAC, mac_str);
    esp_http_client_set_header(client, HOKKU_HDR_FRAME_STATE, frame_state);

    const esp_app_desc_t *app = esp_app_get_description();
    const char *fw_ver = (app && app->version[0]) ? app->version : "unknown";
    esp_http_client_set_header(client, HOKKU_HDR_FW_VERSION, fw_ver);
    esp_http_client_set_header(client, HOKKU_HDR_FW_BUILD, fw_build);

    /* Attach the log ring (carry + active, joined) as the POST body. Size the
     * receiving buffer to the max joined length so nothing is truncated.
     * Allocated in PSRAM: it's up to 22 KB and this runs during the WiFi+HTTP
     * window, the peak internal-DRAM-pressure moment (WiFi/lwIP live in DRAM). */
    char *log_body = heap_caps_malloc(HOKKU_LOG_MAX_UPLOAD, MALLOC_CAP_SPIRAM);
    int   log_body_len = 0;
    if (log_body) {
        log_body_len = (int)hokku_log_snapshot(log_body, HOKKU_LOG_MAX_UPLOAD);
        if (log_body_len == 0) { free(log_body); log_body = NULL; }  /* free() is valid on heap_caps mem */
    }
    if (log_body) {
        esp_http_client_set_header(client, "Content-Type", "text/plain");
        esp_http_client_set_post_field(client, log_body, log_body_len);
    }

    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    free(log_body);

    if (err != ESP_OK && status < 100) {
        ESP_LOGE("hokku", "HTTP request failed: %s", esp_err_to_name(err));
        return false;
    }

    /* Response headers captured during perform() — copied into ctx, not
     * pointers into esp_http_client internals, so safe to read now. */
    res->http_status  = status;
    res->sleep_s      = hokku_sleep_seconds_parse(ctx.sleep_seconds_hdr[0] ? ctx.sleep_seconds_hdr : NULL);
    res->server_epoch = hokku_server_epoch_parse(ctx.server_epoch_hdr[0] ? ctx.server_epoch_hdr : NULL);
    if (!hokku_cal_seed_parse(ctx.cal_ppm_hdr, ctx.cal_n_hdr, &res->cal_seed_ppm, &res->cal_seed_n))
        res->cal_seed_n = 0;
    ESP_LOGI("hokku", "reply %d: sleep=%d epoch=%lld seed=%d ppm/%d", status, (int)res->sleep_s,
             (long long)res->server_epoch, (int)res->cal_seed_ppm, (int)res->cal_seed_n);

    if (res->server_epoch > 0)
        set_clock(res->server_epoch);

    if (status != 200)
        return false;

    /* The log reached the server with this 200: reset the ring so the next
     * cycle starts fresh rather than re-uploading the same content. */
    hokku_log_reset();

    /* The server owns the name of a screen it knows: adopt its name (a no-op
     * when unchanged) so the next request carries it. */
    if (ctx.name_hdr[0] != '\0')
        config_set_screen_name(ctx.name_hdr);

    /* OTA request (the server sends it only on a 200): the body is ignored. */
    if (ctx.fw_update_hdr[0] != '\0' && fw_update && fw_update_len) {
        capture(fw_update, fw_update_len, ctx.fw_update_hdr);
        ESP_LOGI("hokku", "X-Firmware-Update: %s (server requested OTA)", fw_update);
    }

    res->image_ok = hokku_image_size_ok(ctx.received, expect_bytes);
    if (!res->image_ok)
        ESP_LOGE("hokku", "image size %u, expected %u",
                 (unsigned)ctx.received, (unsigned)expect_bytes);
    return res->image_ok;
}
