/*
 * Hokku 13.3" ACeP 6-color E-Paper Frame - Custom Firmware
 * UC8179C dual-panel controller, 1200x800, SPI interface
 *
 * Features:
 *   - WiFi image download from HTTP server
 *   - NTP time sync with RTC drift compensation
 *   - Scheduled refresh at 06:00, 12:00, 18:00
 *   - Deep sleep between refreshes (~8uA target)
 *   - Button wakeup (GPIO1, GPIO12)
 *   - Battery voltage monitoring
 *
 * Decoded from original firmware (ESP-IDF v5.2.2) IROM.bin disassembly.
 */

#include <string.h>
#include <time.h>
#include <sys/time.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_http_client.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "esp_timer.h"

static const char *TAG = "epaper";

/* ── Pin definitions ─────────────────────────────────────────────── */
#define PIN_EPAPER_MOSI     41
#define PIN_EPAPER_SCLK      9
#define PIN_EPAPER_CS        0
#define PIN_EPAPER_RST       6
#define PIN_EPAPER_BUSY      7
#define PIN_CTRL1           18
#define PIN_CTRL2            8
#define PIN_EPAPER_PWR_EN    3
#define PIN_SYS_POWER       17
#define PIN_BUTTON_1         1
#define PIN_PWR_BUTTON      12
#define PIN_BUTTON_2        40   /* "switch photo" / next image (active LOW) */
#define PIN_BUTTON_3        39
#define PIN_WORK_LED         2
#define PIN_WIFI_LED        38
#define PIN_BATT_ADC         5   /* ADC1_CH4 */
#define PIN_CHG_EN1          4   /* Charger enable (active LOW) */
#define PIN_CHG_EN2         13   /* Charger enable (active LOW) */
#define PIN_CHG_STATUS      14   /* Charger status input */

/* ── Display parameters ──────────────────────────────────────────── */
#define DISPLAY_W          1200
#define DISPLAY_H           800       /* full display: 1200x800, split across two panels */
#define PANEL_W             600       /* each panel shows 600 columns */
#define PANEL_SIZE         (DISPLAY_W * DISPLAY_H / 2)  /* 4bpp = 480000 per panel */
#define TOTAL_IMAGE_SIZE   (PANEL_SIZE * 2)              /* 960000 for full display */
#define SPI_CHUNK_SIZE     4800

#define ROW_BYTES       (DISPLAY_W / 2)   /* 600 bytes per row (4bpp) */
#define ROWS_PER_CHUNK  (SPI_CHUNK_SIZE / ROW_BYTES)  /* 8 rows per chunk */
#define NUM_CHUNKS      (DISPLAY_H / ROWS_PER_CHUNK)  /* 100 chunks per panel */

/* ── Network config ──────────────────────────────────────────────── */
/* WIFI_SSID, WIFI_PASS, IMAGE_URL — copy secrets.h.example to secrets.h */
#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Missing secrets.h — copy secrets.h.example to secrets.h and fill in your values"
#endif
#define WIFI_CONNECT_TIMEOUT_MS  15000
#define NTP_SERVER         "pool.ntp.org"
#define NTP_SYNC_TIMEOUT_MS      10000
#define HTTP_TIMEOUT_MS    30000

/* ── Schedule (from secrets.h) ───────────────────────────────────── */
#ifndef WAKE_HOURS_INIT
#define WAKE_HOURS_INIT    {6, 12, 18}
#endif
#ifndef TIMEZONE
#define TIMEZONE           "CST6CDT,M3.2.0,M11.1.0"
#endif
static const int WAKE_HOURS[] = WAKE_HOURS_INIT;
#define NUM_WAKE_HOURS     (sizeof(WAKE_HOURS) / sizeof(WAKE_HOURS[0]))

/* ── Battery ─────────────────────────────────────────────────────── */
#define BATT_LOW_MV        3400
#define BATT_CHARGE_MV     3300  /* below this: charge-only mode, skip WiFi/display */
#define BATT_DIVIDER_MULT  3.34f  /* calibrated: ADC ~1230mV at pin → 4.1V actual */

/* ── RTC memory (survives deep sleep) ────────────────────────────── */
RTC_DATA_ATTR static int      boot_count = 0;
RTC_DATA_ATTR static uint8_t  wifi_channel = 0;
RTC_DATA_ATTR static uint8_t  wifi_bssid[6] = {0};
RTC_DATA_ATTR static bool     has_wifi_cache = false;
RTC_DATA_ATTR static int64_t  last_ntp_sync_epoch = 0;  /* for weekly re-sync check */
RTC_DATA_ATTR static uint16_t last_battery_mv = 0;
RTC_DATA_ATTR static bool     was_sleeping = false;  /* detect USB reset after deep sleep */

/* ── Embedded image ──────────────────────────────────────────────── */
extern const uint8_t testimg_start[] asm("_binary_test_quadrants_bin_start");
extern const uint8_t testimg_end[]   asm("_binary_test_quadrants_bin_end");

/* ── Globals ─────────────────────────────────────────────────────── */
static spi_device_handle_t spi_handle;
static EventGroupHandle_t  wifi_events;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

/* ═══════════════════════════════════════════════════════════════════
 *  SPI / E-Paper Display Driver
 * ═══════════════════════════════════════════════════════════════════ */

static void ctrl_low(void)  { gpio_set_level(PIN_CTRL1, 0); gpio_set_level(PIN_CTRL2, 0); }
static void ctrl_high(void) { gpio_set_level(PIN_CTRL1, 1); gpio_set_level(PIN_CTRL2, 1); }

static void epaper_wait_busy(void)
{
    int timeout = 60000;  /* 60s — dual-panel refresh can take 30-40s */
    while (gpio_get_level(PIN_EPAPER_BUSY) == 0 && timeout > 0) {
        vTaskDelay(pdMS_TO_TICKS(10));
        timeout -= 10;
    }
    if (timeout <= 0) ESP_LOGW(TAG, "BUSY timeout!");
}

static void epaper_cmd(uint8_t cmd)
{
    spi_transaction_t t = { .cmd = cmd };
    esp_err_t ret = spi_device_polling_transmit(spi_handle, &t);
    if (ret != ESP_OK) ESP_LOGE(TAG, "SPI cmd 0x%02X FAILED: %s", cmd, esp_err_to_name(ret));
}

static void epaper_cmd_data(uint8_t cmd, const uint8_t *data, size_t len)
{
    spi_transaction_t t = { .cmd = cmd, .length = len * 8, .tx_buffer = data };
    esp_err_t ret = spi_device_polling_transmit(spi_handle, &t);
    if (ret != ESP_OK) ESP_LOGE(TAG, "SPI cmd_data 0x%02X FAILED: %s", cmd, esp_err_to_name(ret));
}

/* ── Hardware init ───────────────────────────────────────────────── */

static void hw_gpio_init(void)
{
    /* De-isolate all RTC GPIOs — they may be held/isolated from a previous
       deep sleep (original firmware or ours). Must use RTC functions first
       since gpio_reset_pin() alone doesn't clear RTC isolation. */
    /* De-isolate SYS_POWER first and immediately drive it HIGH to avoid brownout.
     * gpio_reset_pin() briefly sets output LOW, which would cut system power. */
    if (rtc_gpio_is_valid_gpio(PIN_SYS_POWER)) {
        rtc_gpio_hold_dis(PIN_SYS_POWER);
        rtc_gpio_deinit(PIN_SYS_POWER);
    }
    gpio_reset_pin(PIN_SYS_POWER);
    gpio_set_direction(PIN_SYS_POWER, GPIO_MODE_INPUT_OUTPUT);
    gpio_set_level(PIN_SYS_POWER, 1);  /* keep system powered */

    const int rtc_pins[] = {
        PIN_EPAPER_PWR_EN, PIN_EPAPER_RST, PIN_EPAPER_BUSY,
        PIN_CTRL1, PIN_CTRL2, PIN_EPAPER_CS, PIN_WORK_LED,
        PIN_EPAPER_SCLK, PIN_BATT_ADC,
    };
    for (int i = 0; i < (int)(sizeof(rtc_pins)/sizeof(rtc_pins[0])); i++) {
        if (rtc_gpio_is_valid_gpio(rtc_pins[i])) {
            rtc_gpio_hold_dis(rtc_pins[i]);
            rtc_gpio_deinit(rtc_pins[i]);
        }
        gpio_reset_pin(rtc_pins[i]);
    }

    gpio_config_t pwr_cfg = {
        .pin_bit_mask = (1ULL << PIN_SYS_POWER) | (1ULL << PIN_EPAPER_PWR_EN),
        .mode = GPIO_MODE_INPUT_OUTPUT,  /* INPUT_OUTPUT so we can read back */
    };
    gpio_config(&pwr_cfg);

    gpio_config_t ctrl_cfg = {
        .pin_bit_mask = (1ULL << PIN_EPAPER_RST) | (1ULL << PIN_CTRL1) |
                        (1ULL << PIN_CTRL2) | (1ULL << PIN_WORK_LED) |
                        (1ULL << PIN_WIFI_LED),
        .mode = GPIO_MODE_INPUT_OUTPUT,  /* INPUT_OUTPUT for readback debug */
    };
    gpio_config(&ctrl_cfg);

    /* CRITICAL: deselect display BEFORE SPI bus init — CTRL LOW = chip selected,
     * so any SCLK/MOSI glitches during spi_bus_initialize would be seen as commands */
    gpio_set_level(PIN_CTRL1, 1);
    gpio_set_level(PIN_CTRL2, 1);

    gpio_config_t busy_cfg = {
        .pin_bit_mask = (1ULL << PIN_EPAPER_BUSY),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
    };
    gpio_config(&busy_cfg);

    /* Charger enable pins — drive LOW to enable charging (active LOW) */
    gpio_config_t chg_out_cfg = {
        .pin_bit_mask = (1ULL << PIN_CHG_EN1) | (1ULL << PIN_CHG_EN2),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&chg_out_cfg);
    gpio_set_level(PIN_CHG_EN1, 0);
    gpio_set_level(PIN_CHG_EN2, 0);
    ESP_LOGI(TAG, "Charger enabled: GPIO4=0 GPIO13=0");

    /* Charger status input */
    gpio_config_t chg_in_cfg = {
        .pin_bit_mask = (1ULL << PIN_CHG_STATUS),
        .mode = GPIO_MODE_INPUT,
    };
    gpio_config(&chg_in_cfg);
}

static void spi_init(void)
{
    spi_bus_config_t buscfg = {
        .mosi_io_num = PIN_EPAPER_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = PIN_EPAPER_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = SPI_CHUNK_SIZE,
    };
    esp_err_t ret = spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO);
    ESP_LOGI(TAG, "SPI bus init: %s", esp_err_to_name(ret));
    ESP_ERROR_CHECK(ret);

    spi_device_interface_config_t devcfg = {
        .command_bits = 8,
        .mode = 0,
        .clock_speed_hz = 8 * 1000 * 1000,
        .spics_io_num = PIN_EPAPER_CS,
        .flags = SPI_DEVICE_3WIRE | SPI_DEVICE_HALFDUPLEX,
        .queue_size = 10,
    };
    ret = spi_bus_add_device(SPI2_HOST, &devcfg, &spi_handle);
    ESP_LOGI(TAG, "SPI device add: %s", esp_err_to_name(ret));
    ESP_ERROR_CHECK(ret);
}

static void epaper_reset(void)
{
    gpio_set_level(PIN_EPAPER_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(PIN_EPAPER_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(PIN_EPAPER_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(200));
}

/* ── Init sequence (18 commands from IROM disassembly) ───────────── */

static void epaper_init_panel(void)
{
    static const uint8_t cmd_74[] = {0xC0,0x1C,0x1C,0xCC,0xCC,0xCC,0x15,0x15,0x55};
    static const uint8_t cmd_F0[] = {0x49,0x55,0x13,0x5D,0x05,0x10};
    static const uint8_t cmd_00[] = {0xDF,0x69};
    static const uint8_t cmd_50[] = {0xF7};
    static const uint8_t cmd_60[] = {0x03,0x03};
    static const uint8_t cmd_86[] = {0x10};
    static const uint8_t cmd_E3[] = {0x22};
    static const uint8_t cmd_E0[] = {0x01};
    static const uint8_t cmd_61[] = {0x04,0xB0,0x03,0x20};
    static const uint8_t cmd_01[] = {0x0F,0x00,0x28,0x2C,0x28,0x38};
    static const uint8_t cmd_B6[] = {0x07};
    static const uint8_t cmd_06[] = {0xE8,0x28};

    static const uint8_t cmd_B7[] = {0x01};
    static const uint8_t cmd_05[] = {0xE8,0x28};
    static const uint8_t cmd_B0[] = {0x01};
    static const uint8_t cmd_B1[] = {0x02};
    static const uint8_t cmd_A4[] = {0x83,0x00,0x02,0x00,0x00,0x00,0x00,0x00,0x00};
    static const uint8_t cmd_76[] = {0x00,0x00,0x00,0x00,0x04,0x00,0x00,0x00,0x83};

    struct { uint8_t cmd; const uint8_t *data; size_t len; } group1[] = {
        {0x74, cmd_74, sizeof(cmd_74)}, {0xF0, cmd_F0, sizeof(cmd_F0)},
        {0x00, cmd_00, sizeof(cmd_00)}, {0x50, cmd_50, sizeof(cmd_50)},
        {0x60, cmd_60, sizeof(cmd_60)}, {0x86, cmd_86, sizeof(cmd_86)},
        {0xE3, cmd_E3, sizeof(cmd_E3)}, {0xE0, cmd_E0, sizeof(cmd_E0)},
        {0x61, cmd_61, sizeof(cmd_61)}, {0x01, cmd_01, sizeof(cmd_01)},
        {0xB6, cmd_B6, sizeof(cmd_B6)}, {0x06, cmd_06, sizeof(cmd_06)},
    };
    struct { uint8_t cmd; const uint8_t *data; size_t len; } group2[] = {
        {0xB7, cmd_B7, sizeof(cmd_B7)}, {0x05, cmd_05, sizeof(cmd_05)},
        {0xB0, cmd_B0, sizeof(cmd_B0)}, {0xB1, cmd_B1, sizeof(cmd_B1)},
        {0xA4, cmd_A4, sizeof(cmd_A4)}, {0x76, cmd_76, sizeof(cmd_76)},
    };

    for (int i = 0; i < (int)(sizeof(group1)/sizeof(group1[0])); i++) {
        ctrl_low();
        epaper_cmd_data(group1[i].cmd, group1[i].data, group1[i].len);
        ctrl_high();
    }
    for (int i = 0; i < (int)(sizeof(group2)/sizeof(group2[0])); i++) {
        gpio_set_level(PIN_CTRL1, 0);
        epaper_cmd_data(group2[i].cmd, group2[i].data, group2[i].len);
        ctrl_high();
    }
    /* Send Group 2 to CTRL2 as well so both panels are fully initialized */
    for (int i = 0; i < (int)(sizeof(group2)/sizeof(group2[0])); i++) {
        gpio_set_level(PIN_CTRL2, 0);
        epaper_cmd_data(group2[i].cmd, group2[i].data, group2[i].len);
        ctrl_high();
    }
}

/* ── Full display update ─────────────────────────────────────────── */

/* Send 480K to a specific panel via DTM (0x10). ctrl_pin selects the panel. */
static void epaper_send_panel(int ctrl_pin, const uint8_t *image)
{
    gpio_set_level(ctrl_pin, 0);
    static uint8_t buf[SPI_CHUNK_SIZE];

    for (int chunk = 0; chunk < NUM_CHUNKS; chunk++) {
        int offset = chunk * SPI_CHUNK_SIZE;
        memcpy(buf, image + offset, SPI_CHUNK_SIZE);

        if (chunk == 0) {
            spi_transaction_t t = { .cmd = 0x10, .length = SPI_CHUNK_SIZE * 8, .tx_buffer = buf };
            spi_device_polling_transmit(spi_handle, &t);
        } else {
            spi_transaction_ext_t t = {
                .base = { .flags = SPI_TRANS_VARIABLE_CMD, .length = SPI_CHUNK_SIZE * 8, .tx_buffer = buf },
                .command_bits = 0,
            };
            spi_device_polling_transmit(spi_handle, (spi_transaction_t *)&t);
        }
    }
    gpio_set_level(PIN_CTRL1, 1);
    gpio_set_level(PIN_CTRL2, 1);
}

/* Send 480K per panel and refresh. ctrl1_data and ctrl2_data are each 480K. */
static void epaper_display_dual(const uint8_t *ctrl1_data, const uint8_t *ctrl2_data)
{
    epaper_reset();
    epaper_init_panel();

    ESP_LOGI(TAG, "SYS=%d PWR_EN=%d RST=%d BUSY=%d",
             gpio_get_level(PIN_SYS_POWER), gpio_get_level(PIN_EPAPER_PWR_EN),
             gpio_get_level(PIN_EPAPER_RST), gpio_get_level(PIN_EPAPER_BUSY));

    ESP_LOGI(TAG, "Sending 480K to CTRL1 (panel 0)...");
    epaper_send_panel(PIN_CTRL1, ctrl1_data);
    ESP_LOGI(TAG, "Sending 480K to CTRL2 (panel 1)...");
    epaper_send_panel(PIN_CTRL2, ctrl2_data);
    ESP_LOGI(TAG, "BUSY after data: %d", gpio_get_level(PIN_EPAPER_BUSY));

    /* PON — release CTRL before BUSY wait */
    ctrl_low();
    epaper_cmd(0x04);
    ctrl_high();
    epaper_wait_busy();
    ESP_LOGI(TAG, "PON done");

    /* DRF — 30ms pre-delay, release CTRL before BUSY wait */
    ctrl_low();
    vTaskDelay(pdMS_TO_TICKS(30));
    static const uint8_t drf[] = {0x00};
    epaper_cmd_data(0x12, drf, 1);
    ctrl_high();
    ESP_LOGI(TAG, "DRF sent, waiting for refresh (~19s)...");
    epaper_wait_busy();
    ESP_LOGI(TAG, "DRF done");

    /* POF */
    ctrl_low();
    static const uint8_t pof[] = {0x00};
    epaper_cmd_data(0x02, pof, 1);
    ctrl_high();
    epaper_wait_busy();

    ESP_LOGI(TAG, "Display done");
}

/* ═══════════════════════════════════════════════════════════════════
 *  WiFi
 * ═══════════════════════════════════════════════════════════════════ */

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupSetBits(wifi_events, WIFI_FAIL_BIT);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
    }
}

static bool wifi_inited = false;

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

static bool wifi_connect(void)
{
    wifi_events = xEventGroupCreate();
    wifi_init_once();

    wifi_config_t wifi_cfg = {
        .sta = {
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    strncpy((char *)wifi_cfg.sta.ssid, WIFI_SSID, sizeof(wifi_cfg.sta.ssid));
    strncpy((char *)wifi_cfg.sta.password, WIFI_PASS, sizeof(wifi_cfg.sta.password));

    /* Use cached channel/BSSID for fast reconnect */
    if (has_wifi_cache && wifi_channel > 0) {
        wifi_cfg.sta.channel = wifi_channel;
        memcpy(wifi_cfg.sta.bssid, wifi_bssid, 6);
        wifi_cfg.sta.bssid_set = true;
        ESP_LOGI(TAG, "WiFi fast reconnect ch=%d", wifi_channel);
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_connect());

    EventBits_t bits = xEventGroupWaitBits(wifi_events,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE, pdFALSE, pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));

    if (bits & WIFI_CONNECTED_BIT) {
        /* Cache AP info for fast reconnect next time */
        wifi_ap_record_t ap;
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
            wifi_channel = ap.primary;
            memcpy(wifi_bssid, ap.bssid, 6);
            has_wifi_cache = true;
        }
        return true;
    }

    /* Fast reconnect failed, retry without cache */
    if (has_wifi_cache) {
        ESP_LOGW(TAG, "Fast reconnect failed, trying full scan...");
        has_wifi_cache = false;
        esp_wifi_disconnect();

        wifi_cfg.sta.channel = 0;
        wifi_cfg.sta.bssid_set = false;
        esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);
        esp_wifi_connect();

        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);
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
            return true;
        }
    }

    ESP_LOGE(TAG, "WiFi connect failed");
    return false;
}

static void wifi_shutdown(void)
{
    esp_wifi_disconnect();
    esp_wifi_stop();
}

/* ═══════════════════════════════════════════════════════════════════
 *  NTP Time Sync
 * ═══════════════════════════════════════════════════════════════════ */

static bool ntp_sync(void)
{
    setenv("TZ", TIMEZONE, 1);
    tzset();

    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(NTP_SERVER);
    esp_netif_sntp_init(&config);

    int retry = 0;
    const int max_retry = NTP_SYNC_TIMEOUT_MS / 100;
    while (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(100)) != ESP_OK && retry < max_retry) {
        retry++;
    }

    esp_netif_sntp_deinit();

    if (retry >= max_retry) {
        ESP_LOGE(TAG, "NTP sync timeout");
        return false;
    }

    time_t now;
    time(&now);
    struct tm t;
    localtime_r(&now, &t);
    ESP_LOGI(TAG, "NTP synced: %04d-%02d-%02d %02d:%02d:%02d",
             t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
             t.tm_hour, t.tm_min, t.tm_sec);

    /* Persist to NVS so it survives esp_restart() */
    nvs_handle_t nvs;
    if (nvs_open("hokku", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_i64(nvs, "ntp_epoch", (int64_t)now);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
    last_ntp_sync_epoch = now;
    return true;
}

static bool need_ntp_resync(void)
{
    /* Load from NVS on first check (RTC memory doesn't survive esp_restart) */
    if (last_ntp_sync_epoch == 0) {
        nvs_handle_t nvs;
        if (nvs_open("hokku", NVS_READONLY, &nvs) == ESP_OK) {
            nvs_get_i64(nvs, "ntp_epoch", &last_ntp_sync_epoch);
            nvs_close(nvs);
        }
    }
    if (last_ntp_sync_epoch == 0) return true;
    time_t now;
    time(&now);
    /* Re-sync if >96 hours since last sync */
    return (now - last_ntp_sync_epoch) > (96 * 3600);
}

/* ═══════════════════════════════════════════════════════════════════
 *  HTTP Image Download
 * ═══════════════════════════════════════════════════════════════════ */

typedef struct {
    uint8_t *buf;
    size_t   received;
    size_t   capacity;
} http_download_ctx_t;

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    http_download_ctx_t *ctx = (http_download_ctx_t *)evt->user_data;
    if (evt->event_id == HTTP_EVENT_ON_DATA) {
        if (ctx->received + evt->data_len <= ctx->capacity) {
            memcpy(ctx->buf + ctx->received, evt->data, evt->data_len);
            ctx->received += evt->data_len;
        }
    }
    return ESP_OK;
}

static uint8_t *download_image(void)
{
    uint8_t *buf = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
    if (!buf) {
        ESP_LOGE(TAG, "Failed to allocate image buffer from PSRAM");
        return NULL;
    }

    http_download_ctx_t ctx = { .buf = buf, .received = 0, .capacity = TOTAL_IMAGE_SIZE };

    esp_http_client_config_t config = {
        .url = IMAGE_URL,
        .event_handler = http_event_handler,
        .user_data = &ctx,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .buffer_size = 4096,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (err != ESP_OK || status != 200) {
        ESP_LOGE(TAG, "HTTP download failed: err=%s status=%d", esp_err_to_name(err), status);
        heap_caps_free(buf);
        return NULL;
    }

    if (ctx.received != TOTAL_IMAGE_SIZE) {
        ESP_LOGE(TAG, "Image size mismatch: got %d, expected %d", (int)ctx.received, TOTAL_IMAGE_SIZE);
        heap_caps_free(buf);
        return NULL;
    }

    ESP_LOGI(TAG, "Downloaded %d bytes", (int)ctx.received);
    return buf;
}

/* Display a full-resolution 1200x1600 4bpp image (960K) on both panels.
 * Match original FW: first 480K → CTRL1 (GPIO18), second 480K → CTRL2 (GPIO8). */
static void split_and_display(const uint8_t *img)
{
    epaper_display_dual(img, img + PANEL_SIZE);
}

/* ═══════════════════════════════════════════════════════════════════
 *  Schedule & Sleep
 * ═══════════════════════════════════════════════════════════════════ */

static int64_t time_until_next_wake_us(void)
{
    time_t now;
    time(&now);
    struct tm t;
    localtime_r(&now, &t);

    int now_secs = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec;
    int next_secs = -1;

    for (int i = 0; i < (int)NUM_WAKE_HOURS; i++) {
        int wake_secs = WAKE_HOURS[i] * 3600;
        if (wake_secs > now_secs) {
            next_secs = wake_secs;
            break;
        }
    }

    /* No more wake times today -> first wake tomorrow */
    if (next_secs < 0) {
        next_secs = WAKE_HOURS[0] * 3600 + 86400;
    }

    int sleep_secs = next_secs - now_secs;
    ESP_LOGI(TAG, "Next wake in %d:%02d:%02d",
             sleep_secs / 3600, (sleep_secs % 3600) / 60, sleep_secs % 60);

    return (int64_t)sleep_secs * 1000000LL;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Battery Monitoring
 * ═══════════════════════════════════════════════════════════════════ */

static int read_battery_mv(void)
{
    adc_oneshot_unit_handle_t handle;
    adc_oneshot_unit_init_cfg_t init = { .unit_id = ADC_UNIT_1 };
    if (adc_oneshot_new_unit(&init, &handle) != ESP_OK) return 0;

    /* Original FW uses ADC_ATTEN_DB_6 (~0-2200mV range), NOT DB_12 */
    adc_oneshot_chan_cfg_t chan = { .atten = ADC_ATTEN_DB_6, .bitwidth = ADC_BITWIDTH_DEFAULT };
    adc_oneshot_config_channel(handle, ADC_CHANNEL_4, &chan);

    adc_cali_handle_t cali = NULL;
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = ADC_UNIT_1, .atten = ADC_ATTEN_DB_6, .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    bool calibrated = (adc_cali_create_scheme_curve_fitting(&cali_cfg, &cali) == ESP_OK);

    /* Original FW: 50 samples per round, 10 rounds with 50-tick delays.
     * Simplified: 50 samples with short delays for good averaging. */
    int raw_sum = 0;
    for (int i = 0; i < 50; i++) {
        int raw;
        adc_oneshot_read(handle, ADC_CHANNEL_4, &raw);
        raw_sum += raw;
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    int raw_avg = raw_sum / 50;

    int mv = 0;
    if (calibrated) {
        adc_cali_raw_to_voltage(cali, raw_avg, &mv);
        adc_cali_delete_scheme_curve_fitting(cali);
    } else {
        mv = (raw_avg * 2200) / 4095;  /* DB_6 range ~2200mV */
    }

    /* Original FW multiplier: 4.476562 (voltage divider ratio ~4.48:1) */
    int battery_mv = (int)(mv * BATT_DIVIDER_MULT);

    ESP_LOGI("BATT", "ADC raw_avg=%d, calibrated_mv=%d, battery=%d mV",
             raw_avg, mv, battery_mv);

    adc_oneshot_del_unit(handle);
    return battery_mv;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Charger Monitor Task — blinks WORK_LED at 1Hz while charging
 * ═══════════════════════════════════════════════════════════════════ */

static TaskHandle_t chg_monitor_task_handle = NULL;
static bool chg_monitor_fast = false;  /* true = 2Hz (charge-only mode) */

static void chg_monitor_task(void *arg)
{
    bool led_on = false;
    while (1) {
        int charging = (gpio_get_level(PIN_CHG_STATUS) == 0);
        if (charging) {
            led_on = !led_on;
            gpio_set_level(PIN_WORK_LED, led_on ? 1 : 0);
        } else {
            gpio_set_level(PIN_WORK_LED, 1);  /* solid on when not charging */
            led_on = true;
        }
        int delay_ms = chg_monitor_fast ? 250 : 500;  /* 2Hz or 1Hz */
        vTaskDelay(pdMS_TO_TICKS(delay_ms));
    }
}

static void chg_monitor_start(void)
{
    if (!chg_monitor_task_handle) {
        xTaskCreate(chg_monitor_task, "chg_mon", 2048, NULL, 1, &chg_monitor_task_handle);
    }
}

static void chg_monitor_stop(void)
{
    if (chg_monitor_task_handle) {
        vTaskDelete(chg_monitor_task_handle);
        chg_monitor_task_handle = NULL;
    }
}

/* ═══════════════════════════════════════════════════════════════════
 *  Deep Sleep
 * ═══════════════════════════════════════════════════════════════════ */

static void enter_deep_sleep(int64_t sleep_us)
{
    chg_monitor_stop();

    /* Turn off SYS_POWER */
    gpio_set_level(PIN_SYS_POWER, 0);

    /* Turn off LEDs */
    gpio_set_level(PIN_WORK_LED, 0);

    /* Shut down SPI bus */
    spi_bus_remove_device(spi_handle);
    spi_bus_free(SPI2_HOST);

    /* Configure timer wakeup */
    if (sleep_us > 0) {
        esp_sleep_enable_timer_wakeup((uint64_t)sleep_us);
    }

    /* Configure button wakeup: GPIO1 + GPIO12 (active LOW) */
    /* Both are RTC-capable (GPIO 0-21) */
    esp_sleep_enable_ext1_wakeup(
        (1ULL << PIN_BUTTON_1) | (1ULL << PIN_PWR_BUTTON),
        ESP_EXT1_WAKEUP_ANY_LOW
    );

    /* Isolate unused RTC GPIOs to minimize leakage */
    const int isolate_pins[] = {
        PIN_EPAPER_CS, PIN_WORK_LED, PIN_EPAPER_PWR_EN,
        PIN_BATT_ADC, PIN_EPAPER_RST, PIN_EPAPER_BUSY,
        PIN_CTRL2, PIN_EPAPER_SCLK, PIN_SYS_POWER, PIN_CTRL1,
    };
    for (int i = 0; i < (int)(sizeof(isolate_pins)/sizeof(isolate_pins[0])); i++) {
        rtc_gpio_isolate(isolate_pins[i]);
    }

    /* Enable RTC pullups on wakeup buttons */
    rtc_gpio_init(PIN_BUTTON_1);
    rtc_gpio_pullup_en(PIN_BUTTON_1);
    rtc_gpio_init(PIN_PWR_BUTTON);
    rtc_gpio_pullup_en(PIN_PWR_BUTTON);

    ESP_LOGI(TAG, "Entering deep sleep for %.1f hours",
             sleep_us / 3600000000.0);

    was_sleeping = true;
    esp_deep_sleep_start();
    /* Never returns */
}

/* ═══════════════════════════════════════════════════════════════════
 *  Main
 * ═══════════════════════════════════════════════════════════════════ */

void app_main(void)
{
    boot_count++;
    int64_t boot_time = esp_timer_get_time();

    /* Wait for USB-Serial/JTAG to be ready */
    vTaskDelay(pdMS_TO_TICKS(5000));

    /* Determine wakeup cause */
    esp_sleep_wakeup_cause_t wakeup = esp_sleep_get_wakeup_cause();
    bool is_scheduled_wake = (wakeup == ESP_SLEEP_WAKEUP_TIMER || wakeup == ESP_SLEEP_WAKEUP_EXT1);

    /* Detect USB reset after deep sleep: wakeup is UNDEFINED but RTC flag says
       we were sleeping. Deep sleep disconnects USB-Serial/JTAG on ESP32-S3,
       which causes the host to reset the chip — appearing as a fresh boot. */
    bool is_usb_reset_after_sleep = (!is_scheduled_wake && was_sleeping);
    was_sleeping = false;  /* clear for this boot cycle */

    bool is_true_first_boot = (!is_scheduled_wake && !is_usb_reset_after_sleep);

    ESP_LOGI(TAG, "Boot #%d, wakeup=%d%s", boot_count, wakeup,
             is_true_first_boot ? " (first boot)" :
             is_usb_reset_after_sleep ? " (USB reset after sleep)" :
             (wakeup == ESP_SLEEP_WAKEUP_TIMER ? " (timer)" : " (button)"));

    /* Hardware init */
    hw_gpio_init();
    gpio_set_level(PIN_SYS_POWER, 1);
    gpio_set_level(PIN_EPAPER_PWR_EN, 1);
    gpio_set_level(PIN_WORK_LED, 1);
    gpio_set_level(PIN_WIFI_LED, 0);

    /* Start charger monitor (blinks LED while charging) */
    chg_monitor_start();

    vTaskDelay(pdMS_TO_TICKS(500));  /* let display controller fully power up */
    spi_init();

    /* Read battery */
    last_battery_mv = read_battery_mv();
    ESP_LOGI(TAG, "Battery: %d mV", last_battery_mv);

    /* Init NVS (required for WiFi and NTP persistence) */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    /* USB reset after deep sleep: image is already on display, skip refresh.
       Just wait 30s (for reflashing) then go back to sleep. */
    if (is_usb_reset_after_sleep) {
        ESP_LOGI(TAG, "USB reset after deep sleep — image already on display.");
        /* Set timezone for schedule calculation */
        setenv("TZ", TIMEZONE, 1);
        tzset();
        ESP_LOGI(TAG, "Waiting 30s for reflash window...");
        vTaskDelay(pdMS_TO_TICKS(30000));
        int64_t sleep_us = time_until_next_wake_us();
        enter_deep_sleep(sleep_us);
        /* Never returns */
    }

    /* Normal boot path: WiFi → NTP → download → display */
    uint8_t *img = NULL;
    if (wifi_connect()) {
        gpio_set_level(PIN_WIFI_LED, 1);

        /* NTP sync if needed (first boot or every 96h) */
        if (need_ntp_resync()) {
            ESP_LOGI(TAG, "NTP resync needed");
            ntp_sync();
        } else {
            /* Restore timezone even without NTP sync */
            setenv("TZ", TIMEZONE, 1);
            tzset();
        }

        img = download_image();
        wifi_shutdown();
        gpio_set_level(PIN_WIFI_LED, 0);
    }

    /* Fall back to embedded image on first boot only */
    if (!img && is_true_first_boot) {
        ESP_LOGW(TAG, "Download failed, using embedded test image");
        img = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
        if (img) memcpy(img, testimg_start, TOTAL_IMAGE_SIZE);
    }

    if (img) {
        ESP_LOGI(TAG, "Displaying image...");
        split_and_display(img);
        free(img);
        ESP_LOGI(TAG, "Image displayed.");
    } else if (!is_true_first_boot) {
        ESP_LOGW(TAG, "Download failed, keeping current image on display.");
    }

    if (is_scheduled_wake) {
        /* Woke from deep sleep (timer or button) — wait 30s then sleep again */
        int64_t elapsed_us = esp_timer_get_time() - boot_time;
        int64_t remaining_us = 30000000LL - elapsed_us;
        if (remaining_us > 0) {
            ESP_LOGI(TAG, "Staying awake for %d more seconds (30s boot window)...",
                     (int)(remaining_us / 1000000));
            vTaskDelay(pdMS_TO_TICKS(remaining_us / 1000));
        }

        int64_t sleep_us = time_until_next_wake_us();
        enter_deep_sleep(sleep_us);
        /* Never returns */
    }

    /* ── True first boot: button polling for 60s, then auto deep sleep ── */

    ESP_LOGI(TAG, "First boot: 60s awake window. Press BUTTON_2 (GPIO%d) for next image.",
             PIN_BUTTON_2);

    /* Configure buttons as inputs */
    gpio_config_t btn1_cfg = {
        .pin_bit_mask = (1ULL << PIN_BUTTON_1),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&btn1_cfg);

    /* GPIO40: no internal pull — the PCB has an external pull-up */
    gpio_config_t btn2_cfg = {
        .pin_bit_mask = (1ULL << PIN_BUTTON_2),
        .mode = GPIO_MODE_INPUT,
    };
    gpio_config(&btn2_cfg);

    int64_t deadline_us = boot_time + 60000000LL;  /* 60s from boot */

    while (esp_timer_get_time() < deadline_us) {
        /* Check BUTTON_1 (GPIO1) — enter deep sleep immediately */
        if (gpio_get_level(PIN_BUTTON_1) == 0) {
            vTaskDelay(pdMS_TO_TICKS(50));
            if (gpio_get_level(PIN_BUTTON_1) == 0) {
                ESP_LOGI(TAG, "Button 1 pressed — entering deep sleep.");
                while (gpio_get_level(PIN_BUTTON_1) == 0)
                    vTaskDelay(pdMS_TO_TICKS(50));
                int64_t sleep_us = time_until_next_wake_us();
                enter_deep_sleep(sleep_us);
            }
        }

        /* Check BUTTON_2 (GPIO40) — next image */
        if (gpio_get_level(PIN_BUTTON_2) == 0) {
            vTaskDelay(pdMS_TO_TICKS(50));
            if (gpio_get_level(PIN_BUTTON_2) == 0) {
                ESP_LOGI(TAG, "Button pressed! Fetching next image...");
                while (gpio_get_level(PIN_BUTTON_2) == 0)
                    vTaskDelay(pdMS_TO_TICKS(50));

                uint8_t *next = NULL;
                if (wifi_connect()) {
                    gpio_set_level(PIN_WIFI_LED, 1);
                    next = download_image();
                    wifi_shutdown();
                    gpio_set_level(PIN_WIFI_LED, 0);
                }
                if (next) {
                    split_and_display(next);
                    free(next);
                    ESP_LOGI(TAG, "Done. Press button for next image.");
                } else {
                    ESP_LOGW(TAG, "Download failed, keeping current image.");
                }
                /* Reset deadline — give another 60s after manual refresh */
                deadline_us = esp_timer_get_time() + 60000000LL;
            }
        }

        vTaskDelay(pdMS_TO_TICKS(50));
    }

    ESP_LOGI(TAG, "60s timeout — entering deep sleep schedule.");
    int64_t sleep_us = time_until_next_wake_us();
    enter_deep_sleep(sleep_us);
    /* Never returns */
}
