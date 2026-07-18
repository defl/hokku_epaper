/*
 * Seeed reTerminal E1004 (13.3" Spectra 6, T133A01 dual-chip panel) as a
 * hokku screen — ESP-IDF port.
 *
 * Ported from the Arduino/GxEPD2 client in clients/reterminal_e1004/ (PR #16,
 * from issue #14) to the same ESP-IDF framework huessen_epf1301 uses, so this
 * screen builds and is structured the same way as the rest of firmware/.
 *
 * Flow: boot -> WiFi -> GET /hokku/screen/ (X-Screen-Name, X-Screen-Model,
 * X-Battery-mV) -> read 960,000-byte packed panel buffer -> init panel ->
 * DMA both 480K halves out via DTM (0x10), one per chip-select -> refresh
 * (~30-40s) -> hibernate -> deep sleep for X-Sleep-Seconds, repeat (deep
 * sleep resets the chip, which re-runs app_main — same restart-based
 * lifecycle as the Arduino sketch, not huessen_epf1301's regime state
 * machine).
 *
 * The hokku wire format (see docs/screens/huessen_epf1301/hardware_facts.md
 * "Image Preparation" — this screen has no hardware_facts.md of its own yet):
 *   image is 1200(w) x 1600(h), split into two 600x1600 halves.
 *   bytes[0       .. 479999] = LEFT  half (1600 rows x 300 bytes, 2 px/byte)
 *   bytes[480000  .. 959999] = RIGHT half
 *   per byte: high nibble = even column, low nibble = odd column.
 *   nibble palette: 0x0 black, 0x1 white, 0x2 yellow, 0x3 red, 0x5 blue, 0x6 green
 *
 * That palette is IDENTICAL to the T133A01's native nibble encoding (see
 * docs/screens/huessen_epf1301/hardware_guesses.md "Display — Cross-reference:
 * Seeed T133A01 driver"). The Arduino version has to remap through a byte LUT
 * because it goes through GxEPD2's generic color-index framebuffer, which
 * re-encodes to native nibbles at SPI-out time. Since this port talks to the
 * panel directly with no GxEPD2 layer in between, that round-trip is
 * unnecessary — the downloaded bytes are DMA'd straight to DTM unchanged.
 *
 * Init sequence, register values, and the CS0-only-vs-both grouping are taken
 * from clients/reterminal_e1004/GxEPD2_T133A01_1200x1600.cpp (vendored from
 * Seeed_GxEPD2, itself sourced from Seeed's internal Seeed_GFX reference) —
 * translated from Arduino SPI/digitalWrite calls to ESP-IDF's spi_master
 * driver + gpio driver, following huessen_epf1301/main/main.c's patterns
 * (epaper_cmd_data / chunked DMA send / http_event_handler header capture).
 *
 * STATUS: written but UNBUILT and UNFLASHED — no ESP-IDF build or real
 * E1004 hardware was available while porting. The Arduino sketch this was
 * ported from IS verified working end-to-end on stock E1004 hardware (see
 * clients/reterminal_e1004/README.md); the register values, pin map, and
 * wire-format handling carry that verification, but the ESP-IDF-specific
 * plumbing (SPI chunking, ADC calibration, WiFi/HTTP glue) does not. See
 * README.md "Status" in this directory before flashing — this firmware also
 * does not implement A/B OTA, so per root AGENTS.md's firmware-flashing STOP
 * rules it must not be flashed without addressing that first, or explicit
 * human sign-off to flash without it.
 */

#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "nvs_flash.h"

#include "version.h"

static const char *TAG = "e1004";

/* ── Screen identity ─────────────────────────────────────────────── */
#define SCREEN_MODEL       "seeedstudio_e1004"

/* ── USER CONFIG — edit before flashing ──────────────────────────── */
/* No NVS config store in this port (matches the Arduino sketch it was
 * ported from) — compile-time only. huessen_epf1301-style runtime
 * provisioning would be a reasonable follow-up, not attempted here. */
static const char *WIFI_SSID       = "YOUR_WIFI_SSID";
static const char *WIFI_PASS       = "YOUR_WIFI_PASSWORD";
static const char *SERVER_HOST     = "192.168.1.100";
static const uint16_t SERVER_PORT  = 8080;
static const char *SCREEN_NAME     = "E1004";
#define DEFAULT_SLEEP_S     1800u   /* fallback if server sends no X-Sleep-Seconds */

/* ── Pin map (from clients/reterminal_e1004/README.md — verified against
 * Seeed's shipped example; the wiki's published RST pin is wrong) ──── */
#define PIN_EPAPER_SCLK      7
#define PIN_EPAPER_MISO      8
#define PIN_EPAPER_MOSI      9
#define PIN_EPAPER_CS0      10   /* chip 0 (left half), manually toggled — not SPI HW CS */
#define PIN_EPAPER_DC       11
#define PIN_EPAPER_CS1       2   /* chip 1 (right half) */
#define PIN_EPAPER_RST      38
#define PIN_EPAPER_BUSY     13
#define PIN_EPAPER_ENABLE   12
#define PIN_BATT_ADC         1   /* ADC1_CH0 */
#define PIN_BATT_ENABLE     21   /* drive HIGH to enable the divider */
#define BATT_DIVIDER        2.0f

/* ── Display parameters ──────────────────────────────────────────── */
#define HALF_W              600
#define HALF_H             1600
#define HALF_BYTES     480000u   /* 600 * 1600 / 2 */
#define TOTAL_IMAGE_SIZE 960000u
#define SPI_CHUNK_SIZE     4800  /* 480000 / 4800 = 100 chunks, matches huessen_epf1301 */
#define NUM_CHUNKS  (HALF_BYTES / SPI_CHUNK_SIZE)

/* ── Network / timeouts ───────────────────────────────────────────── */
#define WIFI_CONNECT_TIMEOUT_MS  20000
#define HTTP_TIMEOUT_MS          30000

/* ═══════════════════════════════════════════════════════════════════
 *  T133A01 register constants (from GxEPD2_T133A01_1200x1600.cpp,
 *  vendored from Seeed_GxEPD2 — see file header)
 * ═══════════════════════════════════════════════════════════════════ */
static const uint8_t PSR_V[]     = {0xDF, 0x69};
static const uint8_t PWR_V[]     = {0x0F, 0x00, 0x28, 0x2C, 0x28, 0x38};
static const uint8_t POF_V[]     = {0x00};
static const uint8_t DRF_V[]     = {0x01};
static const uint8_t CDI_V[]     = {0x37};
static const uint8_t TRES_V[]    = {0x04, 0xB0, 0x03, 0x20};
static const uint8_t CCSET_CUR[] = {0x01};
static const uint8_t PWS_V[]     = {0x22};
static const uint8_t DCDC_V[]    = {0x44, 0x54, 0x00};
static const uint8_t BTST_P_V[]  = {0xE0, 0x20};
static const uint8_t BTST_N_V[]  = {0xE0, 0x20};
static const uint8_t DSLP_V[]    = {0xA5};
static const uint8_t r74[]       = {0x00, 0x0C, 0x0C, 0xD9, 0xDD, 0xDD, 0x15, 0x15, 0x55};
static const uint8_t rf0[]       = {0x49, 0x55, 0x13, 0x5D, 0x05, 0x10};
static const uint8_t r60[]       = {0x03, 0x03};
static const uint8_t r86[]       = {0x10};
static const uint8_t rb6[]       = {0x07};
static const uint8_t rb7[]       = {0x01};
static const uint8_t rb0[]       = {0x01};
static const uint8_t rb1[]       = {0x02};

#define R00_PSR    0x00
#define R01_PWR    0x01
#define R02_POF    0x02
#define R04_PON    0x04
#define R05_BTST_N 0x05
#define R06_BTST_P 0x06
#define R07_DSLP   0x07
#define R10_DTM    0x10
#define R12_DRF    0x12
#define R50_CDI    0x50
#define R61_TRES   0x61
#define RA5_DCDC   0xA5
#define RE0_CCSET  0xE0
#define RE3_PWS    0xE3

/* ═══════════════════════════════════════════════════════════════════
 *  SPI / Panel Driver — both chip-selects are plain GPIOs, manually
 *  toggled around each transaction (matches the vendored driver's own
 *  approach: neither chip is wired through the SPI peripheral's
 *  hardware CS). spics_io_num is left unset (-1) in the device config.
 * ═══════════════════════════════════════════════════════════════════ */
static spi_device_handle_t spi_handle = NULL;

static void epaper_wait_busy(uint32_t timeout_ms)
{
    /* Strict port of Seeed_GFX CHECK_BUSY(): unconditional 10ms delay
     * before the first poll (the chip needs time to actually drive BUSY
     * after receiving a command), then poll every 10ms. BUSY idles HIGH
     * on this panel (ready = HIGH), same polarity as huessen_epf1301. */
    uint32_t waited = 0;
    do {
        vTaskDelay(pdMS_TO_TICKS(10));
        waited += 10;
    } while (gpio_get_level(PIN_EPAPER_BUSY) == 0 && waited < timeout_ms);
    if (waited >= timeout_ms) ESP_LOGW(TAG, "BUSY timeout after %" PRIu32 " ms", waited);
}

static void cs_both_low(void)  { gpio_set_level(PIN_EPAPER_CS1, 0); gpio_set_level(PIN_EPAPER_CS0, 0); }
static void cs_both_high(void) { gpio_set_level(PIN_EPAPER_CS0, 1); gpio_set_level(PIN_EPAPER_CS1, 1); }

/* Select chip 0 (left) only; chip 1 must already be / become deselected. */
static void cs0_only_low(void)  { gpio_set_level(PIN_EPAPER_CS1, 1); gpio_set_level(PIN_EPAPER_CS0, 0); }
static void cs0_only_high(void) { gpio_set_level(PIN_EPAPER_CS0, 1); }

/* Select chip 1 (right) only; chip 0 must already be / become deselected. */
static void cs1_only_low(void)  { gpio_set_level(PIN_EPAPER_CS0, 1); gpio_set_level(PIN_EPAPER_CS1, 0); }
static void cs1_only_high(void) { gpio_set_level(PIN_EPAPER_CS1, 1); }

/* Raw SPI write, no DC handling — caller sets DC first. Never reads (the
 * panel has no meaningful read path we use), so a plain tx-only transaction
 * is enough; no command_bits/hardware-CS features are used (see spi_init). */
static void epaper_write_bytes(const uint8_t *data, size_t len)
{
    if (len == 0) return;
    spi_transaction_t t = { .length = len * 8, .tx_buffer = data };
    esp_err_t ret = spi_device_polling_transmit(spi_handle, &t);
    if (ret != ESP_OK) ESP_LOGE(TAG, "SPI write FAILED: %s", esp_err_to_name(ret));
}

/* DC LOW selects command, DC HIGH selects data — a real GPIO on this board
 * (unlike huessen_epf1301, which has no DC line and instead relies on the
 * SPI peripheral's hardware command-phase). Toggled around each command
 * byte / data-bytes pair, matching the vendored Arduino driver's
 * digitalWrite(_dc, LOW)/transfer(cmd)/digitalWrite(_dc, HIGH) sequence.
 * CS is NOT touched here — callers (cmd_both/cmd_cs0) hold it low for the
 * whole cmd+data pair via cs_*_low()/cs_*_high(). */
static void epaper_cmd_data(uint8_t cmd, const uint8_t *data, size_t len)
{
    gpio_set_level(PIN_EPAPER_DC, 0);
    epaper_write_bytes(&cmd, 1);
    gpio_set_level(PIN_EPAPER_DC, 1);
    epaper_write_bytes(data, len);
}

/* Send cmd+data to both chips (CS0+CS1 both low for the transaction). */
static void cmd_both(uint8_t cmd, const uint8_t *data, size_t len)
{
    cs_both_low();
    epaper_cmd_data(cmd, data, len);
    cs_both_high();
}

/* Send cmd+data to chip 0 only. */
static void cmd_cs0(uint8_t cmd, const uint8_t *data, size_t len)
{
    cs0_only_low();
    epaper_cmd_data(cmd, data, len);
    cs0_only_high();
}

static void epaper_reset(void)
{
    /* 20/20ms per Seeed's vendored driver, proven working on real E1004
     * hardware via the Arduino sketch. huessen_epf1301 needed 100/100ms
     * on its board after wedged-controller debugging (see that firmware's
     * epaper_reset comment) — if this panel ever shows similar
     * half-rendered-state symptoms, that's the first thing to try. */
    gpio_set_level(PIN_EPAPER_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(PIN_EPAPER_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(20));
    epaper_wait_busy(5000);
}

/* Full init sequence, ported 1:1 from GxEPD2_T133A01_1200x1600::_InitDisplay(). */
static void epaper_init_panel(void)
{
    epaper_reset();

    cmd_cs0(0x74, r74, sizeof(r74));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(0xF0, rf0, sizeof(rf0));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(R00_PSR, PSR_V, sizeof(PSR_V));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(RA5_DCDC, DCDC_V, sizeof(DCDC_V));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(R50_CDI, CDI_V, sizeof(CDI_V));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(0x60, r60, sizeof(r60));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(0x86, r86, sizeof(r86));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(RE3_PWS, PWS_V, sizeof(PWS_V));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(R61_TRES, TRES_V, sizeof(TRES_V));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(R01_PWR, PWR_V, sizeof(PWR_V));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(0xB6, rb6, sizeof(rb6));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(R06_BTST_P, BTST_P_V, sizeof(BTST_P_V));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(0xB7, rb7, sizeof(rb7));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(R05_BTST_N, BTST_N_V, sizeof(BTST_N_V));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(0xB0, rb0, sizeof(rb0));
    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(0xB1, rb1, sizeof(rb1));
    vTaskDelay(pdMS_TO_TICKS(10));
}

/* Send one 480K half to one chip via DTM (0x10), chunked at SPI_CHUNK_SIZE
 * like huessen_epf1301's epaper_send_panel — a single beginTransaction-style
 * DMA burst per chunk instead of the Arduino driver's per-byte SPI.transfer
 * loop. cs_low/cs_high select/deselect the target chip only. */
static void epaper_send_half(void (*cs_low)(void), void (*cs_high)(void), const uint8_t *data)
{
    static uint8_t buf[SPI_CHUNK_SIZE];
    cs_low();
    uint8_t cmd = R10_DTM;
    gpio_set_level(PIN_EPAPER_DC, 0);
    epaper_write_bytes(&cmd, 1);
    gpio_set_level(PIN_EPAPER_DC, 1);   /* stays HIGH for all data chunks below */
    for (int chunk = 0; chunk < NUM_CHUNKS; chunk++) {
        memcpy(buf, data + (size_t)chunk * SPI_CHUNK_SIZE, SPI_CHUNK_SIZE);
        epaper_write_bytes(buf, SPI_CHUNK_SIZE);
    }
    cs_high();
}

/* Full display cycle: send both halves, then PON -> DRF -> POF -> hibernate.
 * left/right are already in the panel's native nibble encoding — no LUT
 * pass needed (see file header). */
static void epaper_display_dual(const uint8_t *left, const uint8_t *right)
{
    epaper_init_panel();

    cmd_both(RE0_CCSET, CCSET_CUR, sizeof(CCSET_CUR));
    epaper_wait_busy(1000);
    vTaskDelay(pdMS_TO_TICKS(10));

    ESP_LOGI(TAG, "Sending 480K to chip0 (left)...");
    epaper_send_half(cs0_only_low, cs0_only_high, left);
    vTaskDelay(pdMS_TO_TICKS(10));
    ESP_LOGI(TAG, "Sending 480K to chip1 (right)...");
    epaper_send_half(cs1_only_low, cs1_only_high, right);

    cmd_both(R04_PON, NULL, 0);
    epaper_wait_busy(5000);
    vTaskDelay(pdMS_TO_TICKS(30));

    ESP_LOGI(TAG, "DRF sent, waiting for refresh (~30-40s)...");
    int64_t t0 = esp_timer_get_time();
    cmd_both(R12_DRF, DRF_V, sizeof(DRF_V));
    epaper_wait_busy(60000);
    ESP_LOGI(TAG, "DRF done (%lldms)", (esp_timer_get_time() - t0) / 1000);
    vTaskDelay(pdMS_TO_TICKS(30));

    cmd_both(R02_POF, POF_V, sizeof(POF_V));
    epaper_wait_busy(5000);
    vTaskDelay(pdMS_TO_TICKS(30));

    /* Hibernate (0x07 {0xA5}) — matches the Arduino sketch calling
     * epd.hibernate() after every refresh. */
    cmd_both(R07_DSLP, DSLP_V, sizeof(DSLP_V));
}

static void hw_gpio_init(void)
{
    gpio_config_t out_cfg = {
        .pin_bit_mask = (1ULL << PIN_EPAPER_DC) | (1ULL << PIN_EPAPER_RST) |
                        (1ULL << PIN_EPAPER_CS0) | (1ULL << PIN_EPAPER_CS1) |
                        (1ULL << PIN_EPAPER_ENABLE),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&out_cfg);
    /* Deselect both chips BEFORE spi_bus_initialize — CS LOW = selected,
     * so bus-init SCLK/MOSI glitches would otherwise be seen as commands
     * (same rationale as huessen_epf1301's hw_gpio_init). */
    gpio_set_level(PIN_EPAPER_CS0, 1);
    gpio_set_level(PIN_EPAPER_CS1, 1);
    gpio_set_level(PIN_EPAPER_DC, 1);
    gpio_set_level(PIN_EPAPER_RST, 1);
    gpio_set_level(PIN_EPAPER_ENABLE, 1);

    gpio_config_t busy_cfg = {
        .pin_bit_mask = (1ULL << PIN_EPAPER_BUSY),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
    };
    gpio_config(&busy_cfg);
}

static void spi_init(void)
{
    spi_bus_config_t buscfg = {
        .mosi_io_num = PIN_EPAPER_MOSI,
        .miso_io_num = PIN_EPAPER_MISO,
        .sclk_io_num = PIN_EPAPER_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = SPI_CHUNK_SIZE,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO));

    /* No command_bits / hardware-CS features used — every command byte and
     * every data byte goes out as a plain tx-only transaction, with DC and
     * both chip-selects toggled by GPIO around them (see epaper_cmd_data /
     * epaper_send_half). Plain full-duplex mode is fine since we never
     * read (MISO, GPIO8, is wired but unused). */
    spi_device_interface_config_t devcfg = {
        .mode = 0,
        .clock_speed_hz = 10 * 1000 * 1000,
        .spics_io_num = -1,   /* both chip-selects are manual GPIOs, see above */
        .queue_size = 4,
    };
    ESP_ERROR_CHECK(spi_bus_add_device(SPI2_HOST, &devcfg, &spi_handle));
}

/* ═══════════════════════════════════════════════════════════════════
 *  WiFi (single network — no fast-reconnect cache, matches the Arduino
 *  sketch's scope; huessen_epf1301's dual-network cached-BSSID logic
 *  was not ported)
 * ═══════════════════════════════════════════════════════════════════ */
static EventGroupHandle_t wifi_events = NULL;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupSetBits(wifi_events, WIFI_FAIL_BIT);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&e->ip_info.ip));
    }
}

static bool wifi_connect(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    wifi_events = xEventGroupCreate();
    esp_event_handler_instance_t h1, h2;
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, &h1);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, &h2);

    wifi_config_t wifi_cfg = {
        .sta = {
            .threshold.authmode = WIFI_AUTH_OPEN,
            .pmf_cfg = { .capable = true, .required = false },
        },
    };
    strncpy((char *)wifi_cfg.sta.ssid, WIFI_SSID, sizeof(wifi_cfg.sta.ssid) - 1);
    strncpy((char *)wifi_cfg.sta.password, WIFI_PASS, sizeof(wifi_cfg.sta.password) - 1);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "Connecting to %s ...", WIFI_SSID);
    esp_wifi_connect();

    EventBits_t bits = xEventGroupWaitBits(wifi_events,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT, pdFALSE, pdFALSE,
        pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));

    if (bits & WIFI_CONNECTED_BIT) return true;
    ESP_LOGE(TAG, "WiFi connect failed");
    return false;
}

/* ═══════════════════════════════════════════════════════════════════
 *  HTTP — fetch the image, matching huessen_epf1301's download_image
 *  header-capture pattern but as a plain GET (no log-body upload; this
 *  port carries no ring-buffer logger, matching the Arduino sketch).
 * ═══════════════════════════════════════════════════════════════════ */
typedef struct {
    uint8_t *buf;
    size_t   received;
    size_t   capacity;
    char     sleep_seconds_hdr[32];
} http_ctx_t;

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    http_ctx_t *ctx = (http_ctx_t *)evt->user_data;
    if (!ctx) return ESP_OK;
    switch (evt->event_id) {
        case HTTP_EVENT_ON_CONNECTED:
            ctx->received = 0;
            ctx->sleep_seconds_hdr[0] = '\0';
            break;
        case HTTP_EVENT_ON_HEADER:
            if (evt->header_key && evt->header_value &&
                strcasecmp(evt->header_key, "X-Sleep-Seconds") == 0) {
                strncpy(ctx->sleep_seconds_hdr, evt->header_value, sizeof(ctx->sleep_seconds_hdr) - 1);
                ctx->sleep_seconds_hdr[sizeof(ctx->sleep_seconds_hdr) - 1] = '\0';
            }
            break;
        case HTTP_EVENT_ON_DATA:
            if (ctx->received + evt->data_len <= ctx->capacity) {
                memcpy(ctx->buf + ctx->received, evt->data, evt->data_len);
                ctx->received += evt->data_len;
            }
            break;
        default:
            break;
    }
    return ESP_OK;
}

static int read_battery_mv(void)
{
    gpio_config_t en_cfg = { .pin_bit_mask = (1ULL << PIN_BATT_ENABLE), .mode = GPIO_MODE_OUTPUT };
    gpio_config(&en_cfg);
    gpio_set_level(PIN_BATT_ENABLE, 1);
    vTaskDelay(pdMS_TO_TICKS(50));

    adc_oneshot_unit_handle_t handle;
    adc_oneshot_unit_init_cfg_t init = { .unit_id = ADC_UNIT_1 };
    if (adc_oneshot_new_unit(&init, &handle) != ESP_OK) {
        gpio_set_level(PIN_BATT_ENABLE, 0);
        return 0;
    }
    /* DB_12: full ~3.3V range. Battery sense (E-series documented values;
     * E1004 assumed same divider — VERIFY via serial, matches the caveat
     * already in clients/reterminal_e1004/reterminal_e1004.ino). With a
     * 2x divider a 4.2V battery reads ~2.1V at the pin, comfortably inside
     * range with margin for a higher-than-expected divider ratio. */
    adc_oneshot_chan_cfg_t chan = { .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT };
    adc_oneshot_config_channel(handle, ADC_CHANNEL_0, &chan);

    adc_cali_handle_t cali = NULL;
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = ADC_UNIT_1, .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    bool calibrated = (adc_cali_create_scheme_curve_fitting(&cali_cfg, &cali) == ESP_OK);

    int raw_sum = 0, good_reads = 0;
    for (int i = 0; i < 16; i++) {
        int raw = 0;
        if (adc_oneshot_read(handle, ADC_CHANNEL_0, &raw) == ESP_OK) { raw_sum += raw; good_reads++; }
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    gpio_set_level(PIN_BATT_ENABLE, 0);

    if (good_reads == 0) {
        if (cali) adc_cali_delete_scheme_curve_fitting(cali);
        adc_oneshot_del_unit(handle);
        return 0;
    }
    int raw_avg = raw_sum / good_reads;
    int pin_mv = 0;
    if (calibrated) {
        adc_cali_raw_to_voltage(cali, raw_avg, &pin_mv);
        adc_cali_delete_scheme_curve_fitting(cali);
    } else {
        pin_mv = (raw_avg * 3300) / 4095;
    }
    adc_oneshot_del_unit(handle);

    int batt_mv = (int)(pin_mv * BATT_DIVIDER);
    ESP_LOGI(TAG, "battery: pin=%d mV -> battery=%d mV", pin_mv, batt_mv);
    if (batt_mv < 2500 || batt_mv > 4500) return 0;   /* sanity gate */
    return batt_mv;
}

/* Fetch + display one image. Returns seconds to sleep before the next call. */
static uint32_t do_update(void)
{
    uint32_t sleep_s = DEFAULT_SLEEP_S;

    uint8_t *img = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
    if (!img) {
        ESP_LOGE(TAG, "Failed to allocate 960KB image buffer from PSRAM");
        return 300;
    }

    http_ctx_t ctx = { .buf = img, .received = 0, .capacity = TOTAL_IMAGE_SIZE };
    char url[96];
    snprintf(url, sizeof(url), "http://%s:%u/hokku/screen/", SERVER_HOST, (unsigned)SERVER_PORT);

    esp_http_client_config_t http_cfg = {
        .url = url,
        .event_handler = http_event_handler,
        .user_data = &ctx,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .buffer_size = 4096,
    };
    esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
    if (!client) {
        ESP_LOGE(TAG, "esp_http_client_init failed");
        heap_caps_free(img);
        return 300;
    }

    esp_http_client_set_header(client, "X-Screen-Name", SCREEN_NAME);
    esp_http_client_set_header(client, "X-Screen-Model", SCREEN_MODEL);
    int batt_mv = read_battery_mv();
    if (batt_mv > 0) {
        char batt_hdr[16];
        snprintf(batt_hdr, sizeof(batt_hdr), "%d", batt_mv);
        esp_http_client_set_header(client, "X-Battery-mV", batt_hdr);
    }

    ESP_LOGI(TAG, "GET %s", url);
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);

    if (ctx.sleep_seconds_hdr[0] != '\0') {
        int s = atoi(ctx.sleep_seconds_hdr);
        if (s > 0) sleep_s = (uint32_t)s;
        ESP_LOGI(TAG, "X-Sleep-Seconds: %lu", (unsigned long)sleep_s);
    }
    esp_http_client_cleanup(client);

    if (err != ESP_OK || status != 200) {
        ESP_LOGW(TAG, "no image this cycle: err=%s status=%d", esp_err_to_name(err), status);
        heap_caps_free(img);
        return sleep_s;
    }
    if (ctx.received != TOTAL_IMAGE_SIZE) {
        ESP_LOGW(TAG, "short read: got %u / %u bytes", (unsigned)ctx.received, (unsigned)TOTAL_IMAGE_SIZE);
        heap_caps_free(img);
        return sleep_s;
    }

    /* img is already panel-native (see file header) — DMA straight out. */
    spi_init();
    epaper_display_dual(img, img + HALF_BYTES);
    spi_bus_remove_device(spi_handle);
    spi_bus_free(SPI2_HOST);
    spi_handle = NULL;

    heap_caps_free(img);
    return sleep_s;
}

static void enter_deep_sleep(uint32_t sleep_s)
{
    ESP_LOGI(TAG, "Deep sleep for %" PRIu32 " s", sleep_s);
    esp_wifi_stop();
    esp_sleep_enable_timer_wakeup((uint64_t)sleep_s * 1000000ULL);
    esp_deep_sleep_start();   /* resets and re-runs app_main() on wake */
}

void app_main(void)
{
    ESP_LOGI(TAG, "boot, firmware %s (%s)", FW_VERSION_STRING, FW_BUILD_TIMESTAMP);

    hw_gpio_init();

    uint32_t sleep_s = DEFAULT_SLEEP_S;
    if (wifi_connect()) {
        sleep_s = do_update();
    } else {
        sleep_s = 60;   /* retry soon on WiFi failure */
    }

    enter_deep_sleep(sleep_s);
}
