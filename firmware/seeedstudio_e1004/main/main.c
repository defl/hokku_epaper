/*
 * Seeed reTerminal E1004 (13.3" Spectra 6, T133A01 dual-chip panel) as a
 * hokku screen — ESP-IDF firmware.
 *
 * This is a thin BOARD LAYER over the shared appliance code in
 * firmware/common/{all,esp32}/ — the same modules huessen_epf1301 uses. Only the
 * board-specific parts live here: the T133A01 dual-chip-select panel driver,
 * the battery ADC (GPIO1 + enable GPIO21), the GPIO/SPI init, and the top-level
 * deep-sleep state machine. WiFi, HTTP image fetch, A/B OTA, the diagnostic log
 * ring, config (NVS), scheduling, and the X-Frame-State telemetry are all shared.
 *
 * Lifecycle (deep-sleep appliance; the E1004 has no USB-host-detect GPIO — see
 * docs/screens/seeedstudio_e1004/hardware_guesses.md — so there is no
 * USB-awake regime like huessen's): boot -> validate RTC state -> load config ->
 * if a refresh is due (or first boot / button wake): WiFi -> fetch (or OTA) ->
 * display -> schedule -> deep sleep until the next refresh, waking early on a
 * button press (GPIO3/4/5). Deep sleep resets the chip, so each wake re-runs
 * app_main.
 *
 * Wire format + palette (identical to huessen; the downloaded bytes are already
 * the T133A01's native nibble encoding, so they DMA straight to the panel with
 * no colour remap — see docs/screens/seeedstudio_e1004/hardware_facts.md and the
 * cross-reference in huessen's hardware_guesses.md). Panel init sequence +
 * register values are from Seeed's own GxEPD2_T133A01 driver.
 *
 * STATUS: builds in CI (ESP-IDF), host-tested, but UNFLASHED on real E1004
 * hardware. The panel register values/pinout carry the Arduino sketch's
 * real-hardware verification (clients/reterminal_e1004); the ESP-IDF plumbing
 * and the battery divider ratio are not yet hardware-confirmed. A/B OTA + the
 * early recovery via button-wake satisfy the root AGENTS.md flashing STOP rules,
 * but confirm the battery divider and do a scope/serial bring-up before relying
 * on a fleet deployment.
 */

#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <inttypes.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "esp_app_desc.h"
#include "nvs_flash.h"

/* Shared modules (see firmware/common/) */
#include "version.h"
#include "config.h"
#include "text_render.h"
#include "state.h"
#include "scheduler.h"
#include "log.h"
#include "wifi.h"
#include "net.h"
#include "ota.h"
#include "frame_state.h"
#include "backoff.h"       /* shared exponential-retry-backoff policy (SoC-agnostic) */

static const char *TAG = "e1004";

/* ── Screen identity ─────────────────────────────────────────────── */
#define SCREEN_MODEL       "seeedstudio_e1004"

/* ── Pin map (from docs/screens/seeedstudio_e1004/hardware_facts.md) ── */
#define PIN_EPAPER_SCLK      7
#define PIN_EPAPER_MISO      8
#define PIN_EPAPER_MOSI      9
#define PIN_EPAPER_CS0      10   /* chip 0 (left half), manual GPIO CS */
#define PIN_EPAPER_DC       11
#define PIN_EPAPER_CS1       2   /* chip 1 (right half) */
#define PIN_EPAPER_RST      38
#define PIN_EPAPER_BUSY     13
#define PIN_EPAPER_ENABLE   12   /* panel power enable (drive HIGH to power) */
#define PIN_BATT_ADC         1   /* ADC1_CH0 */
#define PIN_BATT_ENABLE     21   /* drive HIGH to enable the divider */
#define BATT_DIVIDER        2.0f
/* Buttons (active-LOW, RTC-wake-capable). GPIO4 is the stock wake pin; we wake
 * on any of the three so any press triggers an early refresh. */
#define PIN_BUTTON_GREEN     3
#define PIN_BUTTON_A         4
#define PIN_BUTTON_B         5
#define BUTTON_WAKE_MASK   ((1ULL << PIN_BUTTON_GREEN) | (1ULL << PIN_BUTTON_A) | (1ULL << PIN_BUTTON_B))

/* ── Display parameters ──────────────────────────────────────────── */
#define HALF_W              600
#define HALF_H             1600
#define HALF_BYTES     480000u   /* 600 * 1600 / 2 (4bpp) */
#define TOTAL_IMAGE_SIZE 960000u
#define COLOR_WHITE_BYTE   0x11  /* two white nibbles (white = 0x1) */
#define SPI_CHUNK_SIZE     4800
#define NUM_CHUNKS  (HALF_BYTES / SPI_CHUNK_SIZE)

/* ── Regime timings ──────────────────────────────────────────────── */
#define REFRESH_RETRY_SECONDS   60
#define REFRESH_RETRY_MAX_SECONDS  3600   /* backoff cap when the server stays unreachable: 1 h */
#define SLEEP_FALLBACK_S        (3 * 3600)

/* ═══════════════════════════════════════════════════════════════════
 *  T133A01 register constants (Seeed_GxEPD2 GxEPD2_T133A01_1200x1600.cpp)
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
 *  SPI / Panel Driver — both chip-selects are plain GPIOs, toggled around
 *  each transaction (the panel isn't wired through the SPI peripheral's
 *  hardware CS). DC is a real GPIO: LOW=command, HIGH=data.
 * ═══════════════════════════════════════════════════════════════════ */
static spi_device_handle_t spi_handle = NULL;

static void epaper_wait_busy(uint32_t timeout_ms)
{
    /* Seeed CHECK_BUSY(): unconditional 10ms delay before the first poll, then
     * poll every 10ms. BUSY idles HIGH (ready = HIGH). */
    uint32_t waited = 0;
    do {
        vTaskDelay(pdMS_TO_TICKS(10));
        waited += 10;
    } while (gpio_get_level(PIN_EPAPER_BUSY) == 0 && waited < timeout_ms);
    if (waited >= timeout_ms) ESP_LOGW(TAG, "BUSY timeout after %" PRIu32 " ms", waited);
}

static void cs_both_low(void)  { gpio_set_level(PIN_EPAPER_CS1, 0); gpio_set_level(PIN_EPAPER_CS0, 0); }
static void cs_both_high(void) { gpio_set_level(PIN_EPAPER_CS0, 1); gpio_set_level(PIN_EPAPER_CS1, 1); }
static void cs0_only_low(void)  { gpio_set_level(PIN_EPAPER_CS1, 1); gpio_set_level(PIN_EPAPER_CS0, 0); }
static void cs0_only_high(void) { gpio_set_level(PIN_EPAPER_CS0, 1); }
static void cs1_only_low(void)  { gpio_set_level(PIN_EPAPER_CS0, 1); gpio_set_level(PIN_EPAPER_CS1, 0); }
static void cs1_only_high(void) { gpio_set_level(PIN_EPAPER_CS1, 1); }

static void epaper_write_bytes(const uint8_t *data, size_t len)
{
    if (len == 0) return;
    spi_transaction_t t = { .length = len * 8, .tx_buffer = data };
    esp_err_t ret = spi_device_polling_transmit(spi_handle, &t);
    if (ret != ESP_OK) ESP_LOGE(TAG, "SPI write FAILED: %s", esp_err_to_name(ret));
}

/* DC LOW selects command, DC HIGH selects data. CS is held by the caller. */
static void epaper_cmd_data(uint8_t cmd, const uint8_t *data, size_t len)
{
    gpio_set_level(PIN_EPAPER_DC, 0);
    epaper_write_bytes(&cmd, 1);
    gpio_set_level(PIN_EPAPER_DC, 1);
    epaper_write_bytes(data, len);
}

static void cmd_both(uint8_t cmd, const uint8_t *data, size_t len)
{
    cs_both_low();
    epaper_cmd_data(cmd, data, len);
    cs_both_high();
}

static void cmd_cs0(uint8_t cmd, const uint8_t *data, size_t len)
{
    cs0_only_low();
    epaper_cmd_data(cmd, data, len);
    cs0_only_high();
}

static void epaper_reset(void)
{
    gpio_set_level(PIN_EPAPER_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(PIN_EPAPER_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(20));
    epaper_wait_busy(5000);
}

/* Init sequence ported 1:1 from GxEPD2_T133A01_1200x1600::_InitDisplay(). */
static void epaper_init_panel(void)
{
    epaper_reset();
    cmd_cs0(0x74, r74, sizeof(r74));            vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(0xF0, rf0, sizeof(rf0));           vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(R00_PSR, PSR_V, sizeof(PSR_V));    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(RA5_DCDC, DCDC_V, sizeof(DCDC_V));  vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(R50_CDI, CDI_V, sizeof(CDI_V));    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(0x60, r60, sizeof(r60));           vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(0x86, r86, sizeof(r86));           vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(RE3_PWS, PWS_V, sizeof(PWS_V));    vTaskDelay(pdMS_TO_TICKS(10));
    cmd_both(R61_TRES, TRES_V, sizeof(TRES_V)); vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(R01_PWR, PWR_V, sizeof(PWR_V));     vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(0xB6, rb6, sizeof(rb6));            vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(R06_BTST_P, BTST_P_V, sizeof(BTST_P_V)); vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(0xB7, rb7, sizeof(rb7));            vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(R05_BTST_N, BTST_N_V, sizeof(BTST_N_V)); vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(0xB0, rb0, sizeof(rb0));            vTaskDelay(pdMS_TO_TICKS(10));
    cmd_cs0(0xB1, rb1, sizeof(rb1));            vTaskDelay(pdMS_TO_TICKS(10));
}

/* Send one 480K half to one chip via DTM (0x10), chunked for DMA. */
static void epaper_send_half(void (*cs_low)(void), void (*cs_high)(void), const uint8_t *data)
{
    static uint8_t buf[SPI_CHUNK_SIZE];
    cs_low();
    uint8_t cmd = R10_DTM;
    gpio_set_level(PIN_EPAPER_DC, 0);
    epaper_write_bytes(&cmd, 1);
    gpio_set_level(PIN_EPAPER_DC, 1);
    for (int chunk = 0; chunk < NUM_CHUNKS; chunk++) {
        memcpy(buf, data + (size_t)chunk * SPI_CHUNK_SIZE, SPI_CHUNK_SIZE);
        epaper_write_bytes(buf, SPI_CHUNK_SIZE);
    }
    cs_high();
}

/* Full display cycle: init, send both halves, PON -> DRF -> POF -> hibernate. */
static void epaper_display_dual(const uint8_t *left, const uint8_t *right)
{
    epaper_init_panel();

    cmd_both(RE0_CCSET, CCSET_CUR, sizeof(CCSET_CUR));
    epaper_wait_busy(1000);
    vTaskDelay(pdMS_TO_TICKS(10));

    epaper_send_half(cs0_only_low, cs0_only_high, left);
    vTaskDelay(pdMS_TO_TICKS(10));
    epaper_send_half(cs1_only_low, cs1_only_high, right);

    cmd_both(R04_PON, NULL, 0);
    epaper_wait_busy(5000);
    vTaskDelay(pdMS_TO_TICKS(30));

    ESP_LOGI(TAG, "DRF — refreshing (~30-40s)...");
    int64_t t0 = esp_timer_get_time();
    cmd_both(R12_DRF, DRF_V, sizeof(DRF_V));
    epaper_wait_busy(60000);
    ESP_LOGI(TAG, "DRF done (%lldms)", (esp_timer_get_time() - t0) / 1000);
    vTaskDelay(pdMS_TO_TICKS(30));

    cmd_both(R02_POF, POF_V, sizeof(POF_V));
    epaper_wait_busy(5000);
    vTaskDelay(pdMS_TO_TICKS(30));

    cmd_both(R07_DSLP, DSLP_V, sizeof(DSLP_V));  /* hibernate the panel */
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
    /* Deselect both chips before SPI bus init (CS LOW = selected). */
    gpio_set_level(PIN_EPAPER_CS0, 1);
    gpio_set_level(PIN_EPAPER_CS1, 1);
    gpio_set_level(PIN_EPAPER_DC, 1);
    gpio_set_level(PIN_EPAPER_RST, 1);
    gpio_set_level(PIN_EPAPER_ENABLE, 1);   /* power the panel */

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
    spi_device_interface_config_t devcfg = {
        .mode = 0,
        .clock_speed_hz = 10 * 1000 * 1000,
        .spics_io_num = -1,   /* both chip-selects are manual GPIOs */
        .queue_size = 4,
    };
    ESP_ERROR_CHECK(spi_bus_add_device(SPI2_HOST, &devcfg, &spi_handle));
}

static void spi_teardown(void)
{
    if (spi_handle) {
        spi_bus_remove_device(spi_handle);
        spi_bus_free(SPI2_HOST);
        spi_handle = NULL;
    }
}

/* Display a full-resolution 1200x1600 image (two 600x1600 halves). */
static void split_and_display(const uint8_t *img)
{
    spi_init();
    epaper_display_dual(img, img + HALF_BYTES);
    spi_teardown();
}

/* Render a text message on the panel (config errors, OTA progress). Left half
 * (600x1600) carries the text; both halves start white. */
static void display_message(const char *msg)
{
    uint8_t *fb = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
    if (!fb) { ESP_LOGE(TAG, "display_message: OOM"); return; }
    memset(fb, COLOR_WHITE_BYTE, TOTAL_IMAGE_SIZE);
    draw_string(fb, HALF_W, HALF_H, 20, 40, msg, 0x0, 3);   /* black text, scale 3 */
    split_and_display(fb);
    heap_caps_free(fb);
}

/* ═══════════════════════════════════════════════════════════════════
 *  Battery — ADC1_CH0 (GPIO1), enable via GPIO21, 2x divider.
 * ═══════════════════════════════════════════════════════════════════ */
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
    ESP_LOGI(TAG, "battery: pin=%d mV -> %d mV", pin_mv, batt_mv);
    if (batt_mv < 2500 || batt_mv > 4500) return 0;   /* sanity gate */
    return batt_mv;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Frame-state gatherer — fill the shared frame_state_t from this board's
 *  sensors, then hand off to the shared builder (common/all).
 * ═══════════════════════════════════════════════════════════════════ */
static void build_frame_state_json(char *buf, size_t buflen,
                                   const char *wake_label, int64_t boot_time_us)
{
    wifi_ap_record_t ap;
    int rssi = (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) ? ap.rssi : 0;
    const esp_app_desc_t *app = esp_app_get_description();
    time_t clk_now_t = time(NULL);

    const char *last_sleep_str =
        (last_sleep_mode == LAST_SLEEP_MODE_TIMER_WAKE)  ? "timer_wake" :
        (last_sleep_mode == LAST_SLEEP_MODE_BUTTON_WAKE) ? "button_wake" :
        (last_sleep_mode == LAST_SLEEP_MODE_POST_REFRESH)? "post_refresh" :
        "none";

    frame_state_t fs = {
        .fw       = (app && app->version[0]) ? app->version : "unknown",
        .boot     = (unsigned)boot_count,
        .wake     = wake_label,
        .regime   = current_regime,
        .uptime_s = (esp_timer_get_time() - boot_time_us) / 1000000LL,
        .bat_mv   = (int)last_battery_mv,   /* always >= 0 -> always emitted */
        .usb      = "none",                 /* E1004 has no USB-host-detect GPIO */
        .last_sleep = last_sleep_str,
        .rssi     = rssi,
        .heap_kb  = (unsigned)(esp_get_free_heap_size() / 1024u),
        .spurious = 0,
        .cfg_ver  = (unsigned)config.cfg_ver,
        .clk_now  = (clk_now_t < 1577836800) ? 0 : (long long)clk_now_t,
        .next_ep  = (long long)(next_refresh_epoch > 0 ? next_refresh_epoch : 0LL),
        .sleep_err_known = last_sleep_err_known,
        .sleep_err_s     = (int)last_sleep_err_s,
        .wifi_cached     = last_wifi_used_cache,
    };
    frame_state_build(buf, buflen, &fs);
}

/* ═══════════════════════════════════════════════════════════════════
 *  Refresh — fetch (or OTA) + display + schedule the next wake.
 * ═══════════════════════════════════════════════════════════════════ */
/* Exponential backoff for repeated "server unreachable" failures. Bumps the
 * RTC-persistent failure streak and returns the next retry interval (60s, 120,
 * ... capped at REFRESH_RETRY_MAX_SECONDS) via the shared SoC-agnostic policy,
 * so a server outage doesn't reboot the device every 60s forever. The caller
 * resets the streak to 0 on any successful server contact. */
static int refresh_retry_backoff_seconds(void)
{
    uint8_t n = consecutive_refresh_failures;
    if (consecutive_refresh_failures < 255) consecutive_refresh_failures++;
    return hokku_backoff_seconds(n, REFRESH_RETRY_SECONDS, REFRESH_RETRY_MAX_SECONDS);
}

static void perform_refresh(const char *wake_label, int64_t boot_time_us)
{
    if (!wifi_connect()) {
        ESP_LOGE(TAG, "WiFi connect failed");
        schedule_retry_in(refresh_retry_backoff_seconds(), "wifi_connect failed");
        return;
    }

    uint8_t *img = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
    if (!img) {
        ESP_LOGE(TAG, "image buffer OOM");
        wifi_shutdown();
        schedule_retry_in(REFRESH_RETRY_SECONDS, "image OOM");
        return;
    }

    char frame_state[384];
    build_frame_state_json(frame_state, sizeof(frame_state), wake_label, boot_time_us);

    int64_t local_at_download = esp_timer_get_time();
    int32_t sleep_seconds = 0;
    int64_t server_epoch  = 0;
    int     http_status   = 0;
    char    fw_update[48] = "";
    hokku_fetch_out_t out = {
        .out_sleep_seconds = &sleep_seconds,
        .out_server_epoch  = &server_epoch,
        .out_http_status   = &http_status,
        .out_fw_update     = fw_update,
        .fw_update_buflen  = sizeof(fw_update),
    };

    bool ok = hokku_http_fetch_image(img, TOTAL_IMAGE_SIZE, config.image_url,
                                     config.screen_name, SCREEN_MODEL, frame_state,
                                     FW_BUILD_TIMESTAMP, &out);

    /* Server-requested OTA: the image body is ignored; update instead. */
    if (fw_update[0] != '\0') {
        heap_caps_free(img);
        perform_ota(fw_update, config.image_url, config.screen_name,
                    SCREEN_MODEL, display_message);   /* reboots on success */
        wifi_shutdown();
        schedule_retry_in(REFRESH_RETRY_SECONDS, "ota returned (failed)");
        return;
    }

    if (!ok) {
        heap_caps_free(img);
        wifi_shutdown();
        if (sleep_seconds > 0) {
            /* Server responded (e.g. 503 busy) — it IS reachable; clear the
             * outage streak and honour its suggested retry interval. */
            consecutive_refresh_failures = 0;
            schedule_retry_in((int)sleep_seconds, "server busy / no image");
        } else {
            /* Couldn't reach the server — back off exponentially. */
            schedule_retry_in(refresh_retry_backoff_seconds(), "fetch failed");
        }
        return;
    }

    wifi_shutdown();

    /* Got an image — server reached and healthy; clear any outage streak. */
    consecutive_refresh_failures = 0;

    /* Schedule the next refresh from the server's absolute clock (drift-free). */
    save_pre_sleep_epoch(server_epoch, local_at_download);
    if (sleep_seconds <= 0) sleep_seconds = SLEEP_FALLBACK_S;
    last_sleep_seconds = sleep_seconds;
    time_t now = now_epoch();
    next_refresh_epoch = (now > 0 ? (int64_t)now : 0) + sleep_seconds;

    /* Display the freshly-downloaded image (bytes are already panel-native). */
    split_and_display(img);
    heap_caps_free(img);
    ESP_LOGI(TAG, "refresh done; next in %d s", (int)sleep_seconds);
}

/* ═══════════════════════════════════════════════════════════════════
 *  Deep sleep — timer + button (GPIO3/4/5) wake.
 * ═══════════════════════════════════════════════════════════════════ */
static void enter_deep_sleep(int64_t sleep_us)
{
    ESP_LOGI(TAG, "Deep sleep %lld s (next_ep=%lld)",
             sleep_us / 1000000LL, (long long)next_refresh_epoch);
    esp_wifi_stop();
    spi_teardown();
    gpio_set_level(PIN_EPAPER_ENABLE, 0);   /* cut panel power */
    gpio_set_level(PIN_BATT_ENABLE, 0);     /* cut the divider */

    if (sleep_us > 0) esp_sleep_enable_timer_wakeup((uint64_t)sleep_us);

    /* Buttons are active-LOW; wake on any of them going low. */
    esp_sleep_enable_ext1_wakeup(BUTTON_WAKE_MASK, ESP_EXT1_WAKEUP_ANY_LOW);
    const int btns[] = { PIN_BUTTON_GREEN, PIN_BUTTON_A, PIN_BUTTON_B };
    for (size_t i = 0; i < sizeof(btns)/sizeof(btns[0]); i++) {
        rtc_gpio_init(btns[i]);
        rtc_gpio_set_direction(btns[i], RTC_GPIO_MODE_INPUT_ONLY);
        rtc_gpio_pullup_en(btns[i]);
        rtc_gpio_pulldown_dis(btns[i]);
    }
    /* Hold the panel-enable + divider low through sleep. */
    rtc_gpio_isolate(PIN_BATT_ENABLE);

    rtc_magic = RTC_MAGIC;  /* keep RTC state valid across the wake */
    esp_deep_sleep_start();
}

/* Compute how long to sleep from the schedule anchor, then sleep. */
static void schedule_and_sleep(void)
{
    int64_t sleep_us;
    if (next_refresh_epoch > 0) {
        time_t now = now_epoch();
        int64_t secs = (now > 0) ? (next_refresh_epoch - (int64_t)now) : (int64_t)last_sleep_seconds;
        if (secs < 1) secs = 1;
        sleep_us = secs * 1000000LL;
    } else if (next_refresh_epoch < 0) {
        int64_t remain_us = -next_refresh_epoch - esp_timer_get_time();
        sleep_us = remain_us > 0 ? remain_us : 1000000LL;
    } else {
        sleep_us = (int64_t)SLEEP_FALLBACK_S * 1000000LL;
    }
    enter_deep_sleep(sleep_us);
}

// cppcheck-suppress unusedFunction
void app_main(void)
{
    int64_t boot_time = esp_timer_get_time();

    hokku_state_validate();
    hokku_log_init();
    ESP_LOGI(TAG, "Firmware %s built %s", FW_VERSION_STRING, FW_BUILD_TIMESTAMP);
    boot_count++;
    current_regime = "battery_idle";

    /* Classify the wake so it's reported in X-Frame-State. */
    const char *wake_label;
    esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
    if (cause == ESP_SLEEP_WAKEUP_EXT1) {
        wake_label = "button_wake";
        last_sleep_mode = LAST_SLEEP_MODE_BUTTON_WAKE;
    } else if (cause == ESP_SLEEP_WAKEUP_TIMER) {
        wake_label = "timer";
        last_sleep_mode = LAST_SLEEP_MODE_TIMER_WAKE;
    } else {
        wake_label = "first_boot";
    }

    /* NVS + config. */
    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    config_load();

    hw_gpio_init();
    last_battery_mv = read_battery_mv();

    /* Sleep-error diagnostic: on a timer wake, compare actual vs expected. */
    if (last_sleep_mode == LAST_SLEEP_MODE_TIMER_WAKE &&
        pre_sleep_server_epoch > 0 && last_sleep_seconds > 0) {
        time_t now = now_epoch();
        if (now > 0) {
            int64_t err = ((int64_t)now - pre_sleep_server_epoch) - last_sleep_seconds;
            if (err > INT32_MAX) err = INT32_MAX;
            if (err < INT32_MIN) err = INT32_MIN;
            last_sleep_err_s = (int32_t)err;
            last_sleep_err_known = true;
        }
    }

    if (!config_version_ok()) {
        char msg[256];
        snprintf(msg, sizeof(msg),
                 "Config version\nmismatch.\n\nExpected: %d\nFound: %d\n\n"
                 "Run hokku-setup to\nreconfigure.", CONFIG_VERSION, config.cfg_ver);
        display_message(msg);
        enter_deep_sleep((int64_t)SLEEP_FALLBACK_S * 1000000LL);
        return;
    }
    if (!config_is_valid()) {
        display_message("Hokku installed but\ncannot read config.\n\n"
                        "Connect USB and run\nhokku-setup to\nconfigure.");
        enter_deep_sleep((int64_t)SLEEP_FALLBACK_S * 1000000LL);
        return;
    }

    perform_refresh(wake_label, boot_time);

    /* A successful refresh proves a freshly-OTA'd app can reach the server and
     * drive the display — confirm it so the bootloader stops watching for a
     * rollback. No-op on a normally-booted (non-pending) app. */
    ota_mark_valid_if_pending();

    schedule_and_sleep();
}
