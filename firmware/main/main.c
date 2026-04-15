/*
 * Hokku 13.3" ACeP 6-color E-Paper Frame - Custom Firmware
 * UC8179C dual-panel controller, 1200x800, SPI interface
 *
 * Features:
 *   - WiFi image download from HTTP server
 *   - Server-driven sleep schedule (X-Sleep-Seconds header)
 *   - USB serial config tool (set WiFi credentials + server URL)
 *   - Deep sleep between refreshes (~8uA target)
 *   - Button wakeup (GPIO1, GPIO12)
 *   - Battery voltage monitoring
 *   - On-screen error messages for misconfiguration
 *
 * Configuration stored in NVS (no compile-time secrets.h needed).
 * Use the hokku-config tool to set WiFi SSID, password, and server URL.
 */

#include <string.h>
#include <stdio.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "driver/usb_serial_jtag.h"
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
#define WIFI_CONNECT_TIMEOUT_MS  15000
#define HTTP_TIMEOUT_MS    30000

/* ── Battery ─────────────────────────────────────────────────────── */
#define BATT_LOW_MV        3400
#define BATT_CHARGE_MV     3300  /* below this: charge-only mode, skip WiFi/display */
#define BATT_DIVIDER_MULT  3.34f  /* calibrated: ADC ~1230mV at pin → 4.1V actual */

/* ── Deep sleep fallback ─────────────────────────────────────────── */
#define SLEEP_3H_US        (3LL * 3600 * 1000000LL)
#define SLEEP_1H_US        (1LL * 3600 * 1000000LL)

/* ── RTC memory (survives deep sleep) ────────────────────────────── */
RTC_DATA_ATTR static int      boot_count = 0;
RTC_DATA_ATTR static uint8_t  wifi_channel = 0;
RTC_DATA_ATTR static uint8_t  wifi_bssid[6] = {0};
RTC_DATA_ATTR static bool     has_wifi_cache = false;
RTC_DATA_ATTR static uint16_t last_battery_mv = 0;
RTC_DATA_ATTR static bool     was_sleeping = false;  /* detect USB reset after deep sleep */
RTC_DATA_ATTR static int32_t  last_sleep_seconds = 0;  /* fallback if server unreachable */

/* ── NVS config ──────────────────────────────────────────────────── */
typedef struct {
    char wifi_ssid[33];
    char wifi_pass[65];
    char image_url[257];
} config_t;

static config_t config = {0};

static bool config_load(void)
{
    nvs_handle_t nvs;
    if (nvs_open("hokku", NVS_READONLY, &nvs) != ESP_OK) return false;

    size_t len;
    len = sizeof(config.wifi_ssid);
    nvs_get_str(nvs, "wifi_ssid", config.wifi_ssid, &len);
    len = sizeof(config.wifi_pass);
    nvs_get_str(nvs, "wifi_pass", config.wifi_pass, &len);
    len = sizeof(config.image_url);
    nvs_get_str(nvs, "image_url", config.image_url, &len);

    nvs_close(nvs);
    return true;
}

static bool config_save_str(const char *key, const char *value)
{
    /* Only write if value actually changed */
    nvs_handle_t nvs;
    if (nvs_open("hokku", NVS_READWRITE, &nvs) != ESP_OK) return false;

    char existing[257] = {0};
    size_t len = sizeof(existing);
    esp_err_t err = nvs_get_str(nvs, key, existing, &len);

    if (err == ESP_OK && strcmp(existing, value) == 0) {
        nvs_close(nvs);
        return true;  /* no change needed */
    }

    err = nvs_set_str(nvs, key, value);
    if (err == ESP_OK) {
        nvs_commit(nvs);
    }
    nvs_close(nvs);
    return err == ESP_OK;
}

static bool config_is_valid(void)
{
    return config.wifi_ssid[0] != '\0' && config.image_url[0] != '\0';
}

/* ── Forward declarations ────────────────────────────────────────── */
static void epaper_display_dual(const uint8_t *ctrl1_data, const uint8_t *ctrl2_data);

/* ── Globals ─────────────────────────────────────────────────────── */
static spi_device_handle_t spi_handle;
static EventGroupHandle_t  wifi_events;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

/* ═══════════════════════════════════════════════════════════════════
 *  Simple Text Rendering (5x7 font into 4bpp framebuffer)
 * ═══════════════════════════════════════════════════════════════════ */

/* Minimal 5x7 bitmap font for ASCII 32-126. Each char is 5 bytes (columns).
 * Bit 0 = top row, bit 6 = bottom row. */
static const uint8_t font5x7[][5] = {
    {0x00,0x00,0x00,0x00,0x00}, /*   */
    {0x00,0x00,0x5F,0x00,0x00}, /* ! */
    {0x00,0x07,0x00,0x07,0x00}, /* " */
    {0x14,0x7F,0x14,0x7F,0x14}, /* # */
    {0x24,0x2A,0x7F,0x2A,0x12}, /* $ */
    {0x23,0x13,0x08,0x64,0x62}, /* % */
    {0x36,0x49,0x55,0x22,0x50}, /* & */
    {0x00,0x05,0x03,0x00,0x00}, /* ' */
    {0x00,0x1C,0x22,0x41,0x00}, /* ( */
    {0x00,0x41,0x22,0x1C,0x00}, /* ) */
    {0x08,0x2A,0x1C,0x2A,0x08}, /* * */
    {0x08,0x08,0x3E,0x08,0x08}, /* + */
    {0x00,0x50,0x30,0x00,0x00}, /* , */
    {0x08,0x08,0x08,0x08,0x08}, /* - */
    {0x00,0x60,0x60,0x00,0x00}, /* . */
    {0x20,0x10,0x08,0x04,0x02}, /* / */
    {0x3E,0x51,0x49,0x45,0x3E}, /* 0 */
    {0x00,0x42,0x7F,0x40,0x00}, /* 1 */
    {0x42,0x61,0x51,0x49,0x46}, /* 2 */
    {0x21,0x41,0x45,0x4B,0x31}, /* 3 */
    {0x18,0x14,0x12,0x7F,0x10}, /* 4 */
    {0x27,0x45,0x45,0x45,0x39}, /* 5 */
    {0x3C,0x4A,0x49,0x49,0x30}, /* 6 */
    {0x01,0x71,0x09,0x05,0x03}, /* 7 */
    {0x36,0x49,0x49,0x49,0x36}, /* 8 */
    {0x06,0x49,0x49,0x29,0x1E}, /* 9 */
    {0x00,0x36,0x36,0x00,0x00}, /* : */
    {0x00,0x56,0x36,0x00,0x00}, /* ; */
    {0x00,0x08,0x14,0x22,0x41}, /* < */
    {0x14,0x14,0x14,0x14,0x14}, /* = */
    {0x41,0x22,0x14,0x08,0x00}, /* > */
    {0x02,0x01,0x51,0x09,0x06}, /* ? */
    {0x32,0x49,0x79,0x41,0x3E}, /* @ */
    {0x7E,0x11,0x11,0x11,0x7E}, /* A */
    {0x7F,0x49,0x49,0x49,0x36}, /* B */
    {0x3E,0x41,0x41,0x41,0x22}, /* C */
    {0x7F,0x41,0x41,0x22,0x1C}, /* D */
    {0x7F,0x49,0x49,0x49,0x41}, /* E */
    {0x7F,0x09,0x09,0x01,0x01}, /* F */
    {0x3E,0x41,0x41,0x51,0x32}, /* G */
    {0x7F,0x08,0x08,0x08,0x7F}, /* H */
    {0x00,0x41,0x7F,0x41,0x00}, /* I */
    {0x20,0x40,0x41,0x3F,0x01}, /* J */
    {0x7F,0x08,0x14,0x22,0x41}, /* K */
    {0x7F,0x40,0x40,0x40,0x40}, /* L */
    {0x7F,0x02,0x04,0x02,0x7F}, /* M */
    {0x7F,0x04,0x08,0x10,0x7F}, /* N */
    {0x3E,0x41,0x41,0x41,0x3E}, /* O */
    {0x7F,0x09,0x09,0x09,0x06}, /* P */
    {0x3E,0x41,0x51,0x21,0x5E}, /* Q */
    {0x7F,0x09,0x19,0x29,0x46}, /* R */
    {0x46,0x49,0x49,0x49,0x31}, /* S */
    {0x01,0x01,0x7F,0x01,0x01}, /* T */
    {0x3F,0x40,0x40,0x40,0x3F}, /* U */
    {0x1F,0x20,0x40,0x20,0x1F}, /* V */
    {0x7F,0x20,0x18,0x20,0x7F}, /* W */
    {0x63,0x14,0x08,0x14,0x63}, /* X */
    {0x03,0x04,0x78,0x04,0x03}, /* Y */
    {0x61,0x51,0x49,0x45,0x43}, /* Z */
    {0x00,0x00,0x7F,0x41,0x41}, /* [ */
    {0x02,0x04,0x08,0x10,0x20}, /* \ */
    {0x41,0x41,0x7F,0x00,0x00}, /* ] */
    {0x04,0x02,0x01,0x02,0x04}, /* ^ */
    {0x40,0x40,0x40,0x40,0x40}, /* _ */
    {0x00,0x01,0x02,0x04,0x00}, /* ` */
    {0x20,0x54,0x54,0x54,0x78}, /* a */
    {0x7F,0x48,0x44,0x44,0x38}, /* b */
    {0x38,0x44,0x44,0x44,0x20}, /* c */
    {0x38,0x44,0x44,0x48,0x7F}, /* d */
    {0x38,0x54,0x54,0x54,0x18}, /* e */
    {0x08,0x7E,0x09,0x01,0x02}, /* f */
    {0x08,0x14,0x54,0x54,0x3C}, /* g */
    {0x7F,0x08,0x04,0x04,0x78}, /* h */
    {0x00,0x44,0x7D,0x40,0x00}, /* i */
    {0x20,0x40,0x44,0x3D,0x00}, /* j */
    {0x00,0x7F,0x10,0x28,0x44}, /* k */
    {0x00,0x41,0x7F,0x40,0x00}, /* l */
    {0x7C,0x04,0x18,0x04,0x78}, /* m */
    {0x7C,0x08,0x04,0x04,0x78}, /* n */
    {0x38,0x44,0x44,0x44,0x38}, /* o */
    {0x7C,0x14,0x14,0x14,0x08}, /* p */
    {0x08,0x14,0x14,0x18,0x7C}, /* q */
    {0x7C,0x08,0x04,0x04,0x08}, /* r */
    {0x48,0x54,0x54,0x54,0x20}, /* s */
    {0x04,0x3F,0x44,0x40,0x20}, /* t */
    {0x3C,0x40,0x40,0x20,0x7C}, /* u */
    {0x1C,0x20,0x40,0x20,0x1C}, /* v */
    {0x3C,0x40,0x30,0x40,0x3C}, /* w */
    {0x44,0x28,0x10,0x28,0x44}, /* x */
    {0x0C,0x50,0x50,0x50,0x3C}, /* y */
    {0x44,0x64,0x54,0x4C,0x44}, /* z */
    {0x00,0x08,0x36,0x41,0x00}, /* { */
    {0x00,0x00,0x7F,0x00,0x00}, /* | */
    {0x00,0x41,0x36,0x08,0x00}, /* } */
    {0x08,0x08,0x2A,0x1C,0x08}, /* ~ */
};

/* Draw a single character at (x, y) in the 4bpp framebuffer.
 * The framebuffer is in portrait orientation: 1200 wide, 1600 tall.
 * color is a 4-bit nibble value (0=black, 1=white). */
static void draw_char(uint8_t *fb, int fb_w, int fb_h, int x, int y,
                       char ch, uint8_t color, int scale)
{
    if (ch < 32 || ch > 126) ch = '?';
    const uint8_t *glyph = font5x7[ch - 32];

    for (int col = 0; col < 5; col++) {
        uint8_t bits = glyph[col];
        for (int row = 0; row < 7; row++) {
            if (bits & (1 << row)) {
                /* Draw a scale x scale block */
                for (int sy = 0; sy < scale; sy++) {
                    for (int sx = 0; sx < scale; sx++) {
                        int px = x + col * scale + sx;
                        int py = y + row * scale + sy;
                        if (px >= 0 && px < fb_w && py >= 0 && py < fb_h) {
                            int idx = py * fb_w + px;
                            int byte_idx = idx / 2;
                            if (idx % 2 == 0) {
                                fb[byte_idx] = (fb[byte_idx] & 0x0F) | (color << 4);
                            } else {
                                fb[byte_idx] = (fb[byte_idx] & 0xF0) | (color & 0x0F);
                            }
                        }
                    }
                }
            }
        }
    }
}

/* Draw a string at (x, y) with given scale. Wraps at fb_w. */
static void draw_string(uint8_t *fb, int fb_w, int fb_h, int x, int y,
                         const char *str, uint8_t color, int scale)
{
    int char_w = 6 * scale;  /* 5 pixels + 1 gap */
    int char_h = 8 * scale;  /* 7 pixels + 1 gap */
    int cx = x, cy = y;

    while (*str) {
        if (*str == '\n') {
            cx = x;
            cy += char_h;
            str++;
            continue;
        }
        if (cx + char_w > fb_w) {
            cx = x;
            cy += char_h;
        }
        if (cy + char_h > fb_h) break;
        draw_char(fb, fb_w, fb_h, cx, cy, *str, color, scale);
        cx += char_w;
        str++;
    }
}

/* Display a text message on the e-ink screen.
 * Blanks everything to white, draws black text on panel 1 only.
 * Panel 1 = left half of landscape view (600x1600 portrait data, 480K at 4bpp).
 * Panel 2 stays blank white. scale=3 → ~33 chars/line, ~66 lines. */
static void display_message(const char *msg)
{
    uint8_t *fb = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
    if (!fb) {
        ESP_LOGE(TAG, "Cannot allocate framebuffer for message");
        return;
    }

    /* Fill entire buffer with white (nibble 0x1 = white in Spectra 6) */
    memset(fb, 0x11, TOTAL_IMAGE_SIZE);

    /* Draw black text into panel 1 only (first 480K) */
    draw_string(fb, 600, 1600, 20, 40, msg, 0x0, 3);

    /* Display: panel 1 has text, panel 2 is blank white */
    epaper_display_dual(fb, fb + PANEL_SIZE);
    heap_caps_free(fb);
}

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
    strncpy((char *)wifi_cfg.sta.ssid, config.wifi_ssid, sizeof(wifi_cfg.sta.ssid));
    strncpy((char *)wifi_cfg.sta.password, config.wifi_pass, sizeof(wifi_cfg.sta.password));

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
 *  HTTP Image Download (reads X-Sleep-Seconds header)
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

/* Download image and extract X-Sleep-Seconds header.
 * Returns image buffer (caller frees) or NULL on failure.
 * *out_sleep_seconds is set if header present, otherwise unchanged. */
static uint8_t *download_image(int32_t *out_sleep_seconds)
{
    uint8_t *buf = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
    if (!buf) {
        ESP_LOGE(TAG, "Failed to allocate image buffer from PSRAM");
        return NULL;
    }

    http_download_ctx_t ctx = { .buf = buf, .received = 0, .capacity = TOTAL_IMAGE_SIZE };

    esp_http_client_config_t http_cfg = {
        .url = config.image_url,
        .event_handler = http_event_handler,
        .user_data = &ctx,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .buffer_size = 4096,
    };

    esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);

    /* Read X-Sleep-Seconds header before cleanup */
    char *sleep_hdr = NULL;
    if (err == ESP_OK) {
        esp_http_client_get_header(client, "X-Sleep-Seconds", &sleep_hdr);
    }

    /* Parse sleep seconds before cleanup destroys the header data */
    if (sleep_hdr != NULL && sleep_hdr[0] != '\0') {
        int32_t secs = atoi(sleep_hdr);
        if (secs > 0) {
            *out_sleep_seconds = secs;
            ESP_LOGI(TAG, "X-Sleep-Seconds: %d", secs);
        }
    }

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

    /* 50 samples with short delays for good averaging. */
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

    if (sleep_us > 0) {
        ESP_LOGI(TAG, "Entering deep sleep for %.1f hours",
                 sleep_us / 3600000000.0);
    } else {
        ESP_LOGI(TAG, "Entering deep sleep (no timer, button wake only)");
    }

    was_sleeping = true;
    esp_deep_sleep_start();
    /* Never returns */
}

/* ═══════════════════════════════════════════════════════════════════
 *  USB Serial Config Protocol
 * ═══════════════════════════════════════════════════════════════════ */

static bool usb_serial_installed = false;

static void usb_serial_init(void)
{
    if (usb_serial_installed) return;
    usb_serial_jtag_driver_config_t cfg = {
        .rx_buffer_size = 512,
        .tx_buffer_size = 512,
    };
    if (usb_serial_jtag_driver_install(&cfg) == ESP_OK) {
        usb_serial_installed = true;
    }
}

static void usb_serial_deinit(void)
{
    if (usb_serial_installed) {
        usb_serial_jtag_driver_uninstall();
        usb_serial_installed = false;
    }
}

static void usb_serial_send(const char *str)
{
    if (!usb_serial_installed) return;
    usb_serial_jtag_write_bytes((const uint8_t *)str, strlen(str), pdMS_TO_TICKS(100));
}

/* Read a line from USB serial. Returns number of chars read (0 if timeout). */
static int usb_serial_readline(char *buf, int bufsize, int timeout_ms)
{
    int pos = 0;
    int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;

    while (pos < bufsize - 1 && esp_timer_get_time() < deadline) {
        uint8_t ch;
        int n = usb_serial_jtag_read_bytes(&ch, 1, pdMS_TO_TICKS(100));
        if (n <= 0) continue;
        if (ch == '\n' || ch == '\r') {
            if (pos > 0) break;  /* end of line */
            continue;  /* skip leading newlines */
        }
        buf[pos++] = (char)ch;
    }
    buf[pos] = '\0';
    return pos;
}

/* Process a single HOKKU: command. Returns true if DONE received. */
static bool process_config_command(const char *line)
{
    if (strncmp(line, "HOKKU:", 6) != 0) return false;
    const char *cmd = line + 6;

    if (strcmp(cmd, "PING") == 0) {
        usb_serial_send("OK:PONG\n");
        return false;
    }

    if (strcmp(cmd, "DONE") == 0) {
        usb_serial_send("OK:DONE\n");
        return true;
    }

    if (strncmp(cmd, "SET ", 4) == 0) {
        const char *kv = cmd + 4;
        const char *eq = strchr(kv, '=');
        if (!eq) {
            usb_serial_send("ERR:missing '=' in SET command\n");
            return false;
        }
        char key[32] = {0};
        int klen = (int)(eq - kv);
        if (klen >= (int)sizeof(key)) klen = sizeof(key) - 1;
        strncpy(key, kv, klen);
        const char *value = eq + 1;

        if (strcmp(key, "wifi_ssid") == 0 || strcmp(key, "wifi_pass") == 0 ||
            strcmp(key, "image_url") == 0) {
            if (config_save_str(key, value)) {
                char resp[300];
                snprintf(resp, sizeof(resp), "OK:%s=%s\n", key,
                         strcmp(key, "wifi_pass") == 0 ? "****" : value);
                usb_serial_send(resp);
            } else {
                usb_serial_send("ERR:NVS write failed\n");
            }
        } else {
            char resp[64];
            snprintf(resp, sizeof(resp), "ERR:unknown key '%s'\n", key);
            usb_serial_send(resp);
        }
        return false;
    }

    if (strncmp(cmd, "GET ", 4) == 0) {
        const char *key = cmd + 4;

        if (strcmp(key, "ALL") == 0) {
            /* Reload config from NVS */
            config_load();
            char resp[512];
            snprintf(resp, sizeof(resp),
                     "OK:wifi_ssid=%s\nOK:wifi_pass=****\nOK:image_url=%s\n",
                     config.wifi_ssid, config.image_url);
            usb_serial_send(resp);
            return false;
        }

        char value[257] = {0};
        nvs_handle_t nvs;
        if (nvs_open("hokku", NVS_READONLY, &nvs) == ESP_OK) {
            size_t len = sizeof(value);
            nvs_get_str(nvs, key, value, &len);
            nvs_close(nvs);
        }
        char resp[300];
        if (strcmp(key, "wifi_pass") == 0) {
            snprintf(resp, sizeof(resp), "OK:%s=****\n", key);
        } else {
            snprintf(resp, sizeof(resp), "OK:%s=%s\n", key, value);
        }
        usb_serial_send(resp);
        return false;
    }

    if (strcmp(cmd, "ERASE") == 0) {
        nvs_handle_t nvs;
        if (nvs_open("hokku", NVS_READWRITE, &nvs) == ESP_OK) {
            nvs_erase_all(nvs);
            nvs_commit(nvs);
            nvs_close(nvs);
            usb_serial_send("OK:erased\n");
        } else {
            usb_serial_send("ERR:NVS open failed\n");
        }
        return false;
    }

    usb_serial_send("ERR:unknown command\n");
    return false;
}

/* Listen for config commands during boot. Returns true if config mode was entered. */
static bool config_listen(int initial_timeout_ms)
{
    usb_serial_init();

    char line[320];
    bool in_config_mode = false;

    int64_t deadline = esp_timer_get_time() + (int64_t)initial_timeout_ms * 1000;

    while (esp_timer_get_time() < deadline) {
        int n = usb_serial_readline(line, sizeof(line), 100);
        if (n == 0) continue;

        if (strncmp(line, "HOKKU:", 6) == 0) {
            if (!in_config_mode) {
                in_config_mode = true;
                /* Extend deadline to 30s from now */
                deadline = esp_timer_get_time() + 30000000LL;
                ESP_LOGI(TAG, "Entered config mode");
            }
            /* Reset deadline on each command */
            deadline = esp_timer_get_time() + 30000000LL;

            if (process_config_command(line)) {
                /* DONE received */
                break;
            }
        }
    }

    usb_serial_deinit();

    if (in_config_mode) {
        /* Reload config after changes */
        config_load();
    }

    return in_config_mode;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Main
 * ═══════════════════════════════════════════════════════════════════ */

void app_main(void)
{
    boot_count++;
    int64_t boot_time = esp_timer_get_time();

    /* Init NVS early (required for config + WiFi) */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    /* Load config from NVS */
    config_load();

    /* Wait for USB-Serial/JTAG to be ready, listen for config commands */
    ESP_LOGI(TAG, "Boot #%d — listening for config commands (5s)...", boot_count);
    config_listen(5000);

    /* Determine wakeup cause */
    esp_sleep_wakeup_cause_t wakeup = esp_sleep_get_wakeup_cause();
    bool is_scheduled_wake = (wakeup == ESP_SLEEP_WAKEUP_TIMER || wakeup == ESP_SLEEP_WAKEUP_EXT1);

    /* Detect USB reset after deep sleep */
    bool is_usb_reset_after_sleep = (!is_scheduled_wake && was_sleeping);
    was_sleeping = false;

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

    /* Check config validity */
    if (!config_is_valid()) {
        ESP_LOGE(TAG, "No valid config found. Display setup message.");
        display_message(
            "Hokku installed but\n"
            "cannot read config.\n"
            "\n"
            "Re-install using the\n"
            "hokku-config tool.\n"
            "\n"
            "Connect USB and run:\n"
            "hokku-config set\n"
            "  --ssid <wifi>\n"
            "  --password <pass>\n"
            "  --url <server-url>"
        );
        /* Sleep forever — only button/USB reset wakes */
        enter_deep_sleep(0);
        /* Never returns */
    }

    /* USB reset after deep sleep: image is already on display, skip refresh.
       Just wait 30s (for reflashing) then go back to sleep. */
    if (is_usb_reset_after_sleep) {
        ESP_LOGI(TAG, "USB reset after deep sleep — image already on display.");
        ESP_LOGI(TAG, "Waiting 30s for reflash window...");
        vTaskDelay(pdMS_TO_TICKS(30000));
        int64_t sleep_us = last_sleep_seconds > 0
            ? (int64_t)last_sleep_seconds * 1000000LL
            : SLEEP_3H_US;
        enter_deep_sleep(sleep_us);
        /* Never returns */
    }

    /* ── Normal boot path: WiFi → download (with sleep header) → display ── */

    int32_t sleep_seconds = 0;
    uint8_t *img = NULL;

    if (wifi_connect()) {
        gpio_set_level(PIN_WIFI_LED, 1);
        img = download_image(&sleep_seconds);
        wifi_shutdown();
        gpio_set_level(PIN_WIFI_LED, 0);
    }

    /* Download failed — show error on screen */
    if (!img) {
        char msg[512];
        snprintf(msg, sizeof(msg),
                 "Image download failed.\n"
                 "\n"
                 "Tried to connect to:\n"
                 "%s\n"
                 "\n"
                 "Press reset to try\n"
                 "again.\n"
                 "\n"
                 "Will retry\n"
                 "automatically in\n"
                 "3 hours.",
                 config.image_url);

        /* Only show error on first boot; on scheduled wake, keep existing image */
        if (is_true_first_boot) {
            display_message(msg);
        } else {
            ESP_LOGW(TAG, "Download failed, keeping current image on display.");
        }

        enter_deep_sleep(SLEEP_3H_US);
        /* Never returns */
    }

    /* Store sleep seconds in RTC for fallback */
    if (sleep_seconds > 0) {
        last_sleep_seconds = sleep_seconds;
    }

    /* Display the image */
    ESP_LOGI(TAG, "Displaying image...");
    split_and_display(img);
    free(img);
    ESP_LOGI(TAG, "Image displayed.");

    if (is_scheduled_wake) {
        /* Woke from deep sleep (timer or button) — wait 30s then sleep again */
        int64_t elapsed_us = esp_timer_get_time() - boot_time;
        int64_t remaining_us = 30000000LL - elapsed_us;
        if (remaining_us > 0) {
            ESP_LOGI(TAG, "Staying awake for %d more seconds (30s boot window)...",
                     (int)(remaining_us / 1000000));
            vTaskDelay(pdMS_TO_TICKS(remaining_us / 1000));
        }

        int64_t sleep_us = (int64_t)sleep_seconds * 1000000LL;
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
                int64_t sleep_us = (int64_t)sleep_seconds * 1000000LL;
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
                    next = download_image(&sleep_seconds);
                    wifi_shutdown();
                    gpio_set_level(PIN_WIFI_LED, 0);
                }
                if (next) {
                    split_and_display(next);
                    free(next);
                    if (sleep_seconds > 0) last_sleep_seconds = sleep_seconds;
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

    ESP_LOGI(TAG, "60s timeout — entering deep sleep.");
    int64_t sleep_us = (int64_t)sleep_seconds * 1000000LL;
    enter_deep_sleep(sleep_us);
    /* Never returns */
}
