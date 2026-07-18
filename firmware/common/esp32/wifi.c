#include "wifi.h"
#include "state.h"     /* wifi_channel / wifi_bssid / has_wifi_cache / last_wifi_index */
#include "config.h"    /* config.wifi_* */

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"

#define WIFI_CONNECT_TIMEOUT_MS  8000   /* per-network attempt; two networks = 16 s worst case */

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

bool last_wifi_used_cache = false;

static EventGroupHandle_t wifi_events;
static bool               wifi_inited = false;

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupSetBits(wifi_events, WIFI_FAIL_BIT);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        /* Set the bit BEFORE logging: fwrite in the log hook can block if the
         * USB CDC TX buffer is full, which would prevent the bit from ever being
         * set and make wifi_connect() time out even though we have an IP. */
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
        ESP_LOGI("hokku", "Got IP: " IPSTR, IP2STR(&e->ip_info.ip));
    }
}

static void wifi_init_once(void)
{
    if (wifi_inited) return;
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t h1, h2;
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, &h1);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, &h2);
    wifi_inited = true;
}

/* Check ESP-IDF error, log and return false on failure (non-fatal). Avoids
 * ESP_ERROR_CHECK panic-on-error — a transient WiFi driver hiccup would
 * otherwise crash the long-lived awake regime. */
#define WIFI_TRY(expr) do {                                                     \
    esp_err_t __err = (expr);                                                   \
    if (__err != ESP_OK) {                                                      \
        ESP_LOGW("hokku", "%s -> %s (continuing)", #expr, esp_err_to_name(__err)); \
        return false;                                                           \
    }                                                                           \
} while (0)

bool wifi_connect(void)
{
    /* Create-once and reuse. Previously allocated a fresh EventGroup on every
     * call, which leaked one per button-press in the first-boot window. */
    if (wifi_events == NULL) {
        wifi_events = xEventGroupCreate();
    } else {
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);
    }
    wifi_init_once();

    WIFI_TRY(esp_wifi_set_mode(WIFI_MODE_STA));
    WIFI_TRY(esp_wifi_start());

    /* Determine which network to try first based on the configured strategy.
     * WIFI_ORDER_LAST_FIRST: start with whichever network last succeeded.
     * WIFI_ORDER_PRIMARY_FIRST (default): always start with slot 0.
     * The BSSID cache is applied only when idx == last_wifi_index. */
    int first = (config.wifi_order == WIFI_ORDER_LAST_FIRST) ? (int)last_wifi_index : 0;
    for (int step = 0; step < 2; step++) {
        int idx = (first + step) % 2;
        if (config.wifi_ssid[idx][0] == '\0') continue;

        /* WIFI_AUTH_OPEN accepts any auth level the AP advertises. Previously
         * hard-coded WPA2_PSK which silently failed on WPA3-only APs.
         * pmf_cfg.capable=true is required for WPA3-SAE: without it the GTK
         * broadcast key is not set up and DHCP DISCOVER (broadcast) is silently
         * dropped by the AP even though L2 association succeeds. required=false
         * keeps WPA2-only AP compatibility. */
        wifi_config_t wifi_cfg = {
            .sta = {
                .threshold.authmode = WIFI_AUTH_OPEN,
                .pmf_cfg = { .capable = true, .required = false },
            },
        };
        /* strncpy with n == sizeof(dst) leaves the last byte unwritten for a
         * source of exactly that length — force NUL so the WiFi stack never
         * reads past the buffer. */
        strncpy((char *)wifi_cfg.sta.ssid, config.wifi_ssid[idx], sizeof(wifi_cfg.sta.ssid) - 1);
        wifi_cfg.sta.ssid[sizeof(wifi_cfg.sta.ssid) - 1] = '\0';
        strncpy((char *)wifi_cfg.sta.password, config.wifi_pass[idx], sizeof(wifi_cfg.sta.password) - 1);
        wifi_cfg.sta.password[sizeof(wifi_cfg.sta.password) - 1] = '\0';

        /* Use cached channel/BSSID only for the network that last succeeded */
        if (has_wifi_cache && wifi_channel > 0 && idx == last_wifi_index) {
            wifi_cfg.sta.channel = wifi_channel;
            memcpy(wifi_cfg.sta.bssid, wifi_bssid, 6);
            wifi_cfg.sta.bssid_set = true;
            ESP_LOGI("hokku", "WiFi fast reconnect ch=%d (net %d)", wifi_channel, idx);
        }

        WIFI_TRY(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);
        WIFI_TRY(esp_wifi_connect());

        EventBits_t bits = xEventGroupWaitBits(wifi_events,
            WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
            pdFALSE, pdFALSE, pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));

        if (bits & WIFI_CONNECTED_BIT) {
            wifi_ap_record_t ap;
            if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
                wifi_channel = ap.primary;
                memcpy(wifi_bssid, ap.bssid, 6);
                has_wifi_cache = true;
            }
            last_wifi_used_cache = wifi_cfg.sta.bssid_set;
            last_wifi_index = (uint8_t)idx;
            return true;
        }

        /* Cache miss: retry this network with a full scan before moving on */
        if (wifi_cfg.sta.bssid_set) {
            ESP_LOGW("hokku", "Fast reconnect failed for net %d, retrying with full scan...", idx);
            has_wifi_cache = false;
            esp_wifi_disconnect();

            wifi_cfg.sta.channel = 0;
            wifi_cfg.sta.bssid_set = false;
            WIFI_TRY(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
            xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);
            WIFI_TRY(esp_wifi_connect());

            bits = xEventGroupWaitBits(wifi_events,
                WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                pdFALSE, pdFALSE, pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));

            if (bits & WIFI_CONNECTED_BIT) {
                wifi_ap_record_t ap;
                if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
                    wifi_channel = ap.primary;
                    memcpy(wifi_bssid, ap.bssid, 6);
                    has_wifi_cache = true;
                }
                last_wifi_used_cache = false;
                last_wifi_index = (uint8_t)idx;
                return true;
            }
        }

        /* If there's another network to try, disconnect cleanly before it */
        int next_idx = (first + step + 1) % 2;
        if (step < 1 && config.wifi_ssid[next_idx][0] != '\0') {
            ESP_LOGW("hokku", "WiFi net %d failed, trying net %d...", idx, next_idx);
            esp_wifi_disconnect();
        }
    }

    ESP_LOGE("hokku", "WiFi connect failed");
    return false;
}

void wifi_shutdown(void)
{
    esp_wifi_disconnect();
    esp_wifi_stop();
}
