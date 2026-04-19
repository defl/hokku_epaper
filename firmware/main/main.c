/*
 * Hokku 13.3" ACeP 6-color E-Paper Frame - Custom Firmware
 * UC8179C dual-panel controller, 1200x800, SPI interface
 *
 * Features:
 *   - WiFi image download from HTTP server
 *   - Server-driven sleep schedule (X-Sleep-Seconds header)
 *   - Deep sleep between refreshes (~8uA target)
 *   - Button wakeup (GPIO1, GPIO12)
 *   - Battery voltage monitoring
 *   - On-screen error messages for misconfiguration
 *
 * Configuration stored in NVS, flashed via hokku-setup tool over USB.
 */

#include <string.h>
#include <stdio.h>
#include <math.h>
#include <time.h>
#include <sys/time.h>

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
#include "esp_http_client.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "esp_timer.h"
#include "esp_app_desc.h"

/* Private IDF API — µs since last power-on, spanning deep sleep and
 * esp_restart(). No public replacement exists in current IDF. If a
 * future version renames it, add a compat shim here. */
#include "esp_private/esp_clk.h"

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

/* ── Display colors (4bpp nibbles, packed two per byte) ─────────── */
#define COLOR_WHITE_BYTE   0x11  /* two white pixels per byte */

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

/* How long the device stays awake after any displayed image, polling the
 * buttons for a "next image" request. Also serves as the reflash window:
 * esptool-triggered resets work during this time (the chip is active), and
 * deep sleep takes over when it expires. Any button press resets the window
 * so the user gets a fresh 60s after each image change. */
#define AWAKE_WINDOW_US    (60LL * 1000000LL)

/* ── RTC memory (survives deep sleep + esp_restart) ──────────────────
 *
 * All of these must use RTC_NOINIT_ATTR (NOT RTC_DATA_ATTR). ESP-IDF
 * header comment:
 *   RTC_DATA_ATTR     — keeps value during a deep sleep / wake cycle
 *   RTC_NOINIT_ATTR   — keeps value AFTER RESTART or during deep sleep
 *
 * RTC_DATA_ATTR vars are re-initialised from the ELF on every esp_restart,
 * which wipes boot_count / rtc_magic / clock state / safety counters.
 * Observed 2026-04-18: Boot #1 repeated across clean software restarts,
 * clk_now always 0, sleep-error diagnostic always 0, spurious/busy
 * counters never accumulating. RTC_NOINIT_ATTR skips all init and is
 * the correct macro for state that must survive esp_restart.
 *
 * Note: RTC_NOINIT_ATTR initializers are not respected (the section
 * is never loaded), so the values are garbage on a true POR. The
 * rtc_magic check inside app_main catches this and zero-initialises
 * every counter exactly once, then stamps rtc_magic = RTC_MAGIC so
 * subsequent restarts skip the reset. */
#define RTC_MAGIC 0x484F4B55  /* "HOKU" — validates RTC memory isn't stale after flash / POR */
RTC_NOINIT_ATTR static uint32_t rtc_magic;
RTC_NOINIT_ATTR static int      boot_count;
RTC_NOINIT_ATTR static uint8_t  wifi_channel;
RTC_NOINIT_ATTR static uint8_t  wifi_bssid[6];
RTC_NOINIT_ATTR static bool     has_wifi_cache;
RTC_NOINIT_ATTR static uint16_t last_battery_mv;
RTC_NOINIT_ATTR static bool     was_sleeping;  /* detect USB reset after deep sleep */
RTC_NOINIT_ATTR static int32_t  last_sleep_seconds;  /* fallback if server unreachable */

/* Scheduled wake deadline expressed in the RTC slow-clock frame (µs since
 * last POR, via esp_clk_rtc_time()). Written just before we enter sleep or
 * the USB polling loop; compared against esp_clk_rtc_time() on wake so we
 * can tell "spurious early reset" from "timer fired but was misreported
 * as UNDEFINED". 0 = no deadline set (first boot or unknown). */
RTC_NOINIT_ATTR static uint64_t scheduled_wake_rtc_us;

/* How many spurious-reset shortcuts we've taken in a row. The shortcut
 * (skip display init, immediate sleep) saves battery on legitimate USB-
 * host-reset cases, but if something keeps spuriously resetting the chip
 * the device would never give the user a reflash window. After
 * MAX_SPURIOUS_RESETS in a row, we force a full awake window. Reset to 0
 * on any non-spurious wake. */
RTC_NOINIT_ATTR static uint8_t  consecutive_spurious_resets;
#define MAX_SPURIOUS_RESETS 3

/* Display-recovery counter. If BUSY is stuck LOW after split_and_display
 * returns (UC8179C wedged — observed on real hardware, image half-
 * rendered), we esp_restart to give the controller a fresh cold boot.
 * Capped at MAX_BUSY_RECOVERY_REBOOTS to prevent a permanently-broken
 * display from keeping the chip in a reboot loop draining the battery.
 * Reset on any successful display (BUSY reads HIGH post-refresh) and on
 * rtc_magic invalidation. */
RTC_NOINIT_ATTR static uint8_t  consecutive_busy_timeouts;
#define MAX_BUSY_RECOVERY_REBOOTS 3

/* Server epoch (seconds) at the moment we entered deep sleep, computed
 * from the X-Server-Time-Epoch response header plus the local elapsed
 * time between download and sleep entry. The next boot uses this and a
 * fresh epoch from the new download to compute actual_slept vs
 * expected_slept and log the error. 0 = no valid pre-sleep epoch
 * recorded (don't run the sleep check on next boot). */
RTC_NOINIT_ATTR static int64_t  pre_sleep_server_epoch;

/* Every successful HTTP response carries X-Server-Time-Epoch; on receipt
 * we call settimeofday() so the firmware's system clock holds real UTC.
 * The system clock is backed by the RTC slow clock on ESP32-S3 and keeps
 * ticking through deep sleep + esp_restart, so subsequent time(NULL) calls
 * (even days later) give us an accurate "now" we can report back to the
 * server in X-Frame-State.clk_now — server then computes drift as the
 * difference between our reported clock and its own. No RTC anchor needed:
 * the system clock IS the anchor. */

/* Last measured sleep error (actual_slept - expected_slept) in seconds,
 * persisted so the next fetch can report it via X-Frame-State. Set inside
 * the wake-time sleep-check block; cleared whenever pre_sleep_server_epoch
 * is cleared (any path that would invalidate the next sleep check). */
RTC_NOINIT_ATTR static int32_t  last_sleep_err_s;
RTC_NOINIT_ATTR static bool     last_sleep_err_valid;

/* How the chip got from the previous HTTP call to this one. Set inside
 * enter_deep_sleep right before committing to a sleep path, so the NEXT
 * boot can report it via X-Frame-State. */
#define LAST_SLEEP_MODE_NONE        0
#define LAST_SLEEP_MODE_DEEP_SLEEP  1
#define LAST_SLEEP_MODE_USB_POLLING 2
RTC_NOINIT_ATTR static uint8_t  last_sleep_mode;

/* Slack (µs) for the deadline comparison. Absorbs calibration drift of
 * the RTC slow clock between when sleep started and when we re-read the
 * counter on wake. 5 min is comfortably above typical few-percent drift
 * even for 12-hour sleeps. */
#define SCHEDULED_WAKE_SLACK_US (5LL * 60 * 1000000LL)

/* Upper sanity bound on the gap between "now" and a stored deadline.
 * Server-driven schedules max out at 24h; anything beyond 26h means
 * scheduled_wake_rtc_us was written in a different RTC epoch (silicon
 * reset of the RTC counter that didn't also wipe RTC memory) — the
 * stored value is meaningless, so we discard it and fetch. */
#define SCHEDULED_WAKE_SANE_MAX_US (26LL * 3600 * 1000000LL)

/* ── NVS config ──────────────────────────────────────────────────── */
/* Config version — must match the version written by hokku-config/hokku-setup.
 * Increment when NVS config fields change. Source of truth: CLAUDE.md */
#define CONFIG_VERSION  1

typedef struct {
    uint8_t cfg_ver;
    char wifi_ssid[33];
    char wifi_pass[65];
    char image_url[257];
    char screen_name[65];  /* optional display name, sent as X-Screen-Name header */
} config_t;

static config_t config = {0};

static bool config_load(void)
{
    nvs_handle_t nvs;
    if (nvs_open("hokku", NVS_READONLY, &nvs) != ESP_OK) return false;

    nvs_get_u8(nvs, "cfg_ver", &config.cfg_ver);

    size_t len;
    len = sizeof(config.wifi_ssid);
    nvs_get_str(nvs, "wifi_ssid", config.wifi_ssid, &len);
    len = sizeof(config.wifi_pass);
    nvs_get_str(nvs, "wifi_pass", config.wifi_pass, &len);
    len = sizeof(config.image_url);
    nvs_get_str(nvs, "image_url", config.image_url, &len);
    len = sizeof(config.screen_name);
    nvs_get_str(nvs, "screen_name", config.screen_name, &len);

    nvs_close(nvs);
    return true;
}

static bool config_is_valid(void)
{
    return config.wifi_ssid[0] != '\0' && config.image_url[0] != '\0';
}

/* ── Forward declarations ────────────────────────────────────────── */
static void epaper_display_dual(const uint8_t *ctrl1_data, const uint8_t *ctrl2_data);
static void split_and_display(const uint8_t *img);

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

/* Draw a single character at (x, y) in a 4bpp framebuffer of size fb_w x fb_h.
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
 * Buffer layout is identical to an image: first 480K = panel 1 (600 wide),
 * second 480K = panel 2 (600 wide). Both filled white, text drawn on panel 1.
 * Sent via split_and_display — same path as downloaded images. */
static void display_message(const char *msg)
{
    uint8_t *fb = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
    if (!fb) {
        ESP_LOGE(TAG, "Cannot allocate framebuffer for message");
        return;
    }

    /* Fill entire 960K with white (both panels) */
    memset(fb, COLOR_WHITE_BYTE, TOTAL_IMAGE_SIZE);

    /* Draw text into panel 1 (first 480K, 600 pixels wide, 1600 rows) */
    int panel_h = PANEL_SIZE / (PANEL_W / 2);  /* 480000 / 300 = 1600 rows */
    draw_string(fb, PANEL_W, panel_h, 20, 40, msg, 0x0, 3);

    /* Display via the same path as images */
    split_and_display(fb);
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

/* Read the UC8179C internal temperature sensor.  Send cmd 0x40 (TSC), wait
 * for BUSY, then do a raw 2-byte SPI read (no cmd prefix) while CTRL1 holds
 * the bus selected.  Matches the June 2025 original's read_tsc() at IROM
 * 0x4200bdb0 — it runs once per panel-data transfer.  The returned bytes
 * are always 0x00 on this board (internal temp sensor disabled; no
 * external RTD wired) and are purely diagnostic, but the act of issuing
 * the command + BUSY wait + read is part of the original's per-refresh
 * flow that we are matching as closely as possible. */
static uint16_t epaper_read_tsc(void)
{
    gpio_set_level(PIN_CTRL1, 0);                /* select panel 1 only */
    epaper_cmd(0x40);
    epaper_wait_busy();

    uint8_t rx[2] = {0};
    spi_transaction_t t = {
        .cmd       = 0,
        .length    = 0,
        .rxlength  = 16,
        .rx_buffer = rx,
    };
    esp_err_t ret = spi_device_polling_transmit(spi_handle, &t);
    gpio_set_level(PIN_CTRL1, 1);

    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "TSR read FAILED: %s", esp_err_to_name(ret));
        return 0xFFFF;
    }
    return ((uint16_t)rx[0] << 8) | rx[1];
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

/* Matches the original firmware's hardware_reset at IROM 0x4200b984:
 *   RST LOW 100ms, RST HIGH 100ms, then wait for BUSY before any cmd.
 * See .private/boot_analysis/FINAL_FINDINGS.md. Our previous
 * 20ms / 20ms / 200ms (no BUSY wait) sequence was the leading suspect
 * for why the display got stuck in half-rendered states that only
 * reflashing the original firmware reliably cleared. */
static void epaper_reset(void)
{
    gpio_set_level(PIN_EPAPER_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(100));
    gpio_set_level(PIN_EPAPER_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(100));
    epaper_wait_busy();
}

/* ── Init sequence (18 commands from IROM disassembly) ───────────── */

static void epaper_init_panel(void)
{
    /* Init sequence matches the June 2025 E_Frame v2.0.26 firmware (IROM
     * 0x4200b9e8), extracted by Ghidra decompilation of the factory dump
     * currently running on the device. See .private/ANALYSIS_FINAL.md.
     *
     * Differences from the April 2025 v2.0.19 sequence we used previously:
     *   - cmd_00 (PANEL_SETTING):        0xDF 0x69 -> 0xDF 0x6B  (bit flip)
     *   - cmd_06 (BOOSTER_SOFT_START):   0xE8 0x28 -> 0xD8 0x18  (diff timing)
     *   - cmd_05 (POWER_ON_MEASURE):     0xE8 0x28 -> 0xD8 0x18  (diff timing)
     *   - cmd_30 (PLL_CONTROL):          (not sent) -> 0x08       (NEW)
     *   - cmd_A4 (CASCADE_SETTING):      0x83 ...  -> removed
     *   - cmd_76 (undocumented):         0x00 ...  -> removed
     * The June values appear to be a vendor bug-fix of the init sequence
     * (booster/PLL/PSR programming) that we'd been missing. */

    static const uint8_t cmd_74[] = {0xC0,0x1C,0x1C,0xCC,0xCC,0xCC,0x15,0x15,0x55};
    static const uint8_t cmd_F0[] = {0x49,0x55,0x13,0x5D,0x05,0x10};
    static const uint8_t cmd_00[] = {0xDF,0x6B};
    static const uint8_t cmd_30[] = {0x08};
    static const uint8_t cmd_50[] = {0xF7};
    static const uint8_t cmd_60[] = {0x03,0x03};
    static const uint8_t cmd_86[] = {0x10};
    static const uint8_t cmd_E3[] = {0x22};
    static const uint8_t cmd_E0[] = {0x01};
    static const uint8_t cmd_61[] = {0x04,0xB0,0x03,0x20};

    static const uint8_t cmd_01[] = {0x0F,0x00,0x28,0x2C,0x28,0x38};
    static const uint8_t cmd_B6[] = {0x07};
    static const uint8_t cmd_06[] = {0xD8,0x18};
    static const uint8_t cmd_B7[] = {0x01};
    static const uint8_t cmd_05[] = {0xD8,0x18};
    static const uint8_t cmd_B0[] = {0x01};
    static const uint8_t cmd_B1[] = {0x02};

    /* Phase A: broadcast to both panels (CTRL1=0, CTRL2=0). */
    struct { uint8_t cmd; const uint8_t *data; size_t len; } phase_a[] = {
        {0x74, cmd_74, sizeof(cmd_74)}, {0xF0, cmd_F0, sizeof(cmd_F0)},
        {0x00, cmd_00, sizeof(cmd_00)}, {0x30, cmd_30, sizeof(cmd_30)},
        {0x50, cmd_50, sizeof(cmd_50)}, {0x60, cmd_60, sizeof(cmd_60)},
        {0x86, cmd_86, sizeof(cmd_86)}, {0xE3, cmd_E3, sizeof(cmd_E3)},
        {0xE0, cmd_E0, sizeof(cmd_E0)}, {0x61, cmd_61, sizeof(cmd_61)},
    };
    /* Phase B: to CTRL1 only (CTRL1=0, CTRL2 stays HIGH). */
    struct { uint8_t cmd; const uint8_t *data; size_t len; } phase_b[] = {
        {0x01, cmd_01, sizeof(cmd_01)}, {0xB6, cmd_B6, sizeof(cmd_B6)},
        {0x06, cmd_06, sizeof(cmd_06)}, {0xB7, cmd_B7, sizeof(cmd_B7)},
        {0x05, cmd_05, sizeof(cmd_05)}, {0xB0, cmd_B0, sizeof(cmd_B0)},
        {0xB1, cmd_B1, sizeof(cmd_B1)},
    };

    for (int i = 0; i < (int)(sizeof(phase_a)/sizeof(phase_a[0])); i++) {
        ctrl_low();  /* both CTRL LOW -> both panels selected */
        epaper_cmd_data(phase_a[i].cmd, phase_a[i].data, phase_a[i].len);
        ctrl_high(); /* deselect both */
    }
    for (int i = 0; i < (int)(sizeof(phase_b)/sizeof(phase_b[0])); i++) {
        /* between phase B commands the original sets both CTRL HIGH then
         * drops only CTRL1; CTRL2 stays HIGH throughout phase B. */
        gpio_set_level(PIN_CTRL2, 1);
        gpio_set_level(PIN_CTRL1, 0);
        epaper_cmd_data(phase_b[i].cmd, phase_b[i].data, phase_b[i].len);
        gpio_set_level(PIN_CTRL1, 1);
    }
    /* Leave both CTRL HIGH (deselected) when init returns. */
}

/* ── Full display update ─────────────────────────────────────────── */

/* Send 480K to a specific panel via DTM (0x10). ctrl_pin selects the panel. */
static void epaper_send_panel(int ctrl_pin, const uint8_t *image)
{
    /* Read TSC before each panel — the original firmware does this inside
     * its send_panel() per panel (IROM 0x4200be0c calls read_tsc() at
     * entry).  Value is logged for diagnostics. */
    uint16_t tsc = epaper_read_tsc();
    ESP_LOGI(TAG, "TSC Data = 0x%02X, 0x%02X", (tsc >> 8) & 0xFF, tsc & 0xFF);

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

/* Send 480K per panel and refresh. ctrl1_data and ctrl2_data are each 480K.
 *
 * Structure mirrors display_update() from the original firmware
 * (IROM 0x4200acac, disassembled in .private/boot_analysis/FINAL_FINDINGS.md):
 *
 *   gpio_set_level(17, 1)       ; raise display rail
 *   vTaskDelay(10ms)
 *   hardware_reset()            ; first RST: LOW 100 HIGH 100 + BUSY wait
 *   ctrl_high()                 ; deselect both panels
 *   display_init():
 *     gpio_set_level(17, 1)     ; (redundant — already HIGH)
 *     vTaskDelay(1000ms)        ; DC-DC booster stabilisation
 *     hardware_reset()          ; second RST: LOW 100 HIGH 100 + BUSY wait
 *     ... 18 init commands
 *   send_panel(0, ...)
 *   send_panel(1, ...)
 *   display_refresh()           ; PON / DRF / POF with BUSY waits
 *   vTaskDelay(10ms)
 *   gpio_set_level(17, 0)       ; drop display rail between updates
 *
 * Crucially: two hardware resets, a 1000 ms settle between them, and a
 * BUSY wait before init commands. And GPIO 17 is cycled around the
 * update so the UC8179C starts from cold on every refresh — that's
 * what prevents bad internal controller state from persisting across
 * updates. Our previous "warm" path (single RST, no BUSY wait, GPIO 17
 * held HIGH forever) let wedged state survive from one update to the
 * next, which matches the observed symptom. */
static void epaper_display_dual(const uint8_t *ctrl1_data, const uint8_t *ctrl2_data)
{
    /* Step 1: power up the display rail from cold. SYS_POWER may already
     * be HIGH from boot init — force a LOW pulse first so the UC8179C's
     * charge-pump and booster state is definitively reset.
     *
     * 1000ms LOW (extended from 200ms 2026-04-18): a 15V boost rail
     * with a big bulk cap and light leakage load can take hundreds of
     * ms to fully decay — 200ms was observed to leave the controller
     * wedged in exactly the same half-rendered state across reboots.
     * 1 second is conservative; the original firmware holds it LOW
     * between updates (potentially hours), so any duration is fine. */
    gpio_set_level(PIN_SYS_POWER, 0);
    vTaskDelay(pdMS_TO_TICKS(1000));
    gpio_set_level(PIN_SYS_POWER, 1);
    vTaskDelay(pdMS_TO_TICKS(10));

    /* Step 2: deselect both panels before touching anything else */
    ctrl_high();

    /* Step 3: first hardware reset */
    epaper_reset();

    /* Step 4: let the DC-DC booster fully stabilise before the second
     * reset. Matches original firmware's 1000 ms wait inside display_init. */
    vTaskDelay(pdMS_TO_TICKS(1000));

    /* Step 5: second hardware reset (belt-and-suspenders, matches original) */
    epaper_reset();

    /* Step 6: init + image + refresh */
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
    int64_t drf_start_us = esp_timer_get_time();
    epaper_wait_busy();
    int64_t drf_elapsed_ms = (esp_timer_get_time() - drf_start_us) / 1000;
    ESP_LOGI(TAG, "DRF done (%lldms elapsed)", drf_elapsed_ms);

    /* Sanity check: a healthy dual-panel Spectra 6 refresh takes ~19s.
     * A sub-5s DRF means the controller did NOT actually refresh — it's
     * wedged / not responding to SPI, and epaper_wait_busy exited
     * immediately because the external pull-up on GPIO 7 (HARDWARE_FACTS)
     * holds BUSY HIGH when nothing is driving it.
     *
     * We log loudly but do NOT reboot here. An earlier version of this
     * check called esp_restart() and produced an infinite boot loop on
     * a genuinely-dead controller: the reboot doesn't unstick the
     * UC8179C (only physical power-cycle or a factory-firmware reflash
     * does), so every retry DRFs in 0ms and triggers another reboot.
     * Recovery is a user-level action, not a firmware-level one. */
    if (drf_elapsed_ms < 5000) {
        ESP_LOGE(TAG, "DRF completed in %lldms (< 5000ms) — display "
                      "controller is not responding to SPI commands. "
                      "Screen was not refreshed. Physical power-cycle or "
                      "factory-firmware reflash may be required to recover.",
                 drf_elapsed_ms);
    }

    /* POF */
    ctrl_low();
    static const uint8_t pof[] = {0x00};
    epaper_cmd_data(0x02, pof, 1);
    ctrl_high();
    epaper_wait_busy();

    /* Step 7: post-refresh shutdown sequence.  Matches the June 2025
     * original firmware's display_update() at IROM 0x4200acb0 byte-for-
     * byte (Ghidra decompilation, .private/ANALYSIS_FINAL.md).
     *
     * First drive all SPI / button / indicator pins LOW so there is no
     * residual voltage on MOSI/SCLK that could back-bias the UC8179C
     * through its ESD diodes when we drop SYS_POWER.  Hold for 1 second
     * so the controller's internal charge-pump / booster stages settle.
     * Then put the display into hardware reset (RST LOW) BEFORE cutting
     * the power rail — this prevents the controller latching up during
     * the brown-out when SYS_POWER goes away.
     *
     * Our previous shorter teardown (just POF + 10 ms + SYS_POWER LOW)
     * cut power while the signal lines were still driven, which is the
     * leading candidate for why the display occasionally ended up wedged
     * in a state only a factory-firmware reflash could clear. */
    gpio_set_level(PIN_EPAPER_SCLK, 0);
    gpio_set_level(PIN_EPAPER_BUSY, 0);
    gpio_set_level(PIN_EPAPER_MOSI, 0);
    gpio_set_level(PIN_BUTTON_2,    0);
    gpio_set_level(PIN_BUTTON_3,    0);
    gpio_set_level(PIN_WIFI_LED,    0);
    vTaskDelay(pdMS_TO_TICKS(1000));

    gpio_set_level(PIN_CTRL1,       0);
    gpio_set_level(PIN_CTRL2,       0);
    gpio_set_level(PIN_EPAPER_RST,  0);
    gpio_set_level(PIN_SYS_POWER,   0);

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
    /* Create-once and reuse. Previously allocated a fresh EventGroup on
     * every call, which leaked one per button-press in the first-boot
     * window. */
    if (wifi_events == NULL) {
        wifi_events = xEventGroupCreate();
    } else {
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);
    }
    wifi_init_once();

    wifi_config_t wifi_cfg = {
        .sta = {
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    /* strncpy with n == sizeof(dst) leaves the buffer non-terminated for a
     * source of exactly that length. Force a NUL so the WiFi stack never
     * reads past the buffer. */
    strncpy((char *)wifi_cfg.sta.ssid, config.wifi_ssid, sizeof(wifi_cfg.sta.ssid) - 1);
    wifi_cfg.sta.ssid[sizeof(wifi_cfg.sta.ssid) - 1] = '\0';
    strncpy((char *)wifi_cfg.sta.password, config.wifi_pass, sizeof(wifi_cfg.sta.password) - 1);
    wifi_cfg.sta.password[sizeof(wifi_cfg.sta.password) - 1] = '\0';

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
    /* Response-header captures, populated from HTTP_EVENT_ON_HEADER in
     * http_event_handler and read by download_image after perform().
     *
     * HISTORY: this used to be done via esp_http_client_get_header() after
     * perform() returned. That was always wrong — that function reads
     * REQUEST headers, not response headers, so we silently ignored every
     * X-Sleep-Seconds / X-Server-Time-Epoch the server sent. Scheduled
     * wakes relied on last_sleep_seconds fallback or 3h default; the user-
     * visible symptom was "scheduled refresh didn't happen". Fixed by
     * capturing directly from the event stream. */
    char     sleep_seconds_hdr[32];
    char     server_epoch_hdr[32];
} http_download_ctx_t;

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    http_download_ctx_t *ctx = (http_download_ctx_t *)evt->user_data;
    if (!ctx) return ESP_OK;

    switch (evt->event_id) {
        case HTTP_EVENT_ON_CONNECTED:
            /* Reset buffer on each new connection (handles redirects).
             * Without this, a 308 redirect's body accumulates before the
             * real image data, causing a size mismatch. Header captures
             * reset too so we only see the final response's values. */
            ctx->received = 0;
            ctx->sleep_seconds_hdr[0] = '\0';
            ctx->server_epoch_hdr[0]  = '\0';
            break;
        case HTTP_EVENT_ON_HEADER:
            if (evt->header_key && evt->header_value) {
                if (strcasecmp(evt->header_key, "X-Sleep-Seconds") == 0) {
                    strncpy(ctx->sleep_seconds_hdr, evt->header_value,
                            sizeof(ctx->sleep_seconds_hdr) - 1);
                    ctx->sleep_seconds_hdr[sizeof(ctx->sleep_seconds_hdr) - 1] = '\0';
                } else if (strcasecmp(evt->header_key, "X-Server-Time-Epoch") == 0) {
                    strncpy(ctx->server_epoch_hdr, evt->header_value,
                            sizeof(ctx->server_epoch_hdr) - 1);
                    ctx->server_epoch_hdr[sizeof(ctx->server_epoch_hdr) - 1] = '\0';
                }
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

/* Build a compact JSON payload describing the frame's current state.
 * Sent as X-Frame-State on every HTTP call so the server can display
 * full device state in the web UI without needing a serial connection.
 * wake_label = classifier result for THIS boot; caller = what triggered
 * THIS fetch ("wake" for the first post-boot fetch, "button" for an
 * in-awake-window button press). */
static void build_frame_state_json(char *buf, size_t buflen,
                                   const char *wake_label, const char *caller,
                                   int64_t boot_time_us)
{
    int rssi = 0;
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        rssi = ap.rssi;
    }

    size_t free_heap = esp_get_free_heap_size();

    const esp_app_desc_t *app = esp_app_get_description();
    const char *fw = (app && app->version[0]) ? app->version : "unknown";

    int chg_low = (gpio_get_level(PIN_CHG_STATUS) == 0);

    /* Firmware's current wall-clock time from its system clock. The clock
     * was set via settimeofday() on the most recent X-Server-Time-Epoch
     * response, and keeps ticking through deep sleep + esp_restart (system
     * time is RTC-backed on ESP32-S3). Omit if the clock has never been
     * set (epoch < 2020-01-01 — we're in 2026, so any time < that means
     * uninitialised / first boot). */
    time_t clk_now = time(NULL);
    if (clk_now < 1577836800) {
        clk_now = 0;  /* signals "not set" to the server */
    }

    const char *last_sleep_str =
        (last_sleep_mode == LAST_SLEEP_MODE_DEEP_SLEEP) ? "deep_sleep" :
        (last_sleep_mode == LAST_SLEEP_MODE_USB_POLLING) ? "usb_polling" :
        "none";

    int64_t uptime_s = (esp_timer_get_time() - boot_time_us) / 1000000LL;

    /* If we have a valid sleep error from the post-wake check, include it;
     * otherwise omit to avoid sending stale values after a cold boot or
     * any path that cleared pre_sleep_server_epoch. */
    if (last_sleep_err_valid) {
        snprintf(buf, buflen,
            "{\"fw\":\"%s\",\"boot\":%d,\"wake\":\"%s\",\"caller\":\"%s\","
            "\"uptime_s\":%lld,\"bat_mv\":%d,\"chg\":\"%s\","
            "\"last_sleep\":\"%s\",\"rssi\":%d,\"heap_kb\":%u,"
            "\"spurious\":%u,\"cfg_ver\":%u,\"clk_now\":%lld,"
            "\"sleep_err_s\":%d}",
            fw, boot_count, wake_label, caller,
            (long long)uptime_s, (int)last_battery_mv,
            chg_low ? "charging" : "idle",
            last_sleep_str, rssi, (unsigned)(free_heap / 1024u),
            (unsigned)consecutive_spurious_resets,
            (unsigned)config.cfg_ver, (long long)clk_now,
            (int)last_sleep_err_s);
    } else {
        snprintf(buf, buflen,
            "{\"fw\":\"%s\",\"boot\":%d,\"wake\":\"%s\",\"caller\":\"%s\","
            "\"uptime_s\":%lld,\"bat_mv\":%d,\"chg\":\"%s\","
            "\"last_sleep\":\"%s\",\"rssi\":%d,\"heap_kb\":%u,"
            "\"spurious\":%u,\"cfg_ver\":%u,\"clk_now\":%lld}",
            fw, boot_count, wake_label, caller,
            (long long)uptime_s, (int)last_battery_mv,
            chg_low ? "charging" : "idle",
            last_sleep_str, rssi, (unsigned)(free_heap / 1024u),
            (unsigned)consecutive_spurious_resets,
            (unsigned)config.cfg_ver, (long long)clk_now);
    }
}

/* Download image and extract X-Sleep-Seconds + X-Server-Time-Epoch headers.
 * Returns image buffer (caller frees) or NULL on failure.
 * *out_sleep_seconds and *out_server_epoch are set if their headers are
 * present, otherwise unchanged. Either pointer may be NULL.
 * wake_label and caller feed into the X-Frame-State JSON. */
static uint8_t *download_image(int32_t *out_sleep_seconds, int64_t *out_server_epoch,
                               const char *wake_label, const char *caller,
                               int64_t boot_time_us)
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

    /* Send screen name so the server can identify this device */
    if (config.screen_name[0] != '\0') {
        esp_http_client_set_header(client, "X-Screen-Name", config.screen_name);
    }

    /* Full device state in a single compact JSON header. Replaces the
     * older X-Battery-mV header — battery is now inside this dict along
     * with firmware version, wake cause, boot count, etc. */
    char frame_state[384];
    build_frame_state_json(frame_state, sizeof(frame_state),
                           wake_label ? wake_label : "unknown",
                           caller ? caller : "wake",
                           boot_time_us);
    esp_http_client_set_header(client, "X-Frame-State", frame_state);

    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);

    /* Headers captured by http_event_handler during the response. Reading
     * them here (after perform() completes) is safe because we copied
     * into ctx, not stored pointers into esp_http_client's internals. */
    if (ctx.sleep_seconds_hdr[0] != '\0' && out_sleep_seconds != NULL) {
        int32_t secs = atoi(ctx.sleep_seconds_hdr);
        if (secs > 0) {
            *out_sleep_seconds = secs;
            ESP_LOGI(TAG, "X-Sleep-Seconds: %d", secs);
        } else {
            ESP_LOGW(TAG, "X-Sleep-Seconds present but non-positive: '%s' (parsed=%d)",
                     ctx.sleep_seconds_hdr, (int)secs);
        }
    } else {
        ESP_LOGW(TAG, "X-Sleep-Seconds header missing from response (status=%d)", status);
    }
    if (ctx.server_epoch_hdr[0] != '\0' && out_server_epoch != NULL) {
        int64_t epoch = atoll(ctx.server_epoch_hdr);
        if (epoch > 0) {
            *out_server_epoch = epoch;
            /* Set the firmware's system clock to server time. Backed by the
             * RTC slow clock — survives deep sleep + esp_restart. On the
             * next X-Frame-State we report time(NULL) directly and the
             * server sees actual drift. */
            struct timeval tv = { .tv_sec = (time_t)epoch, .tv_usec = 0 };
            settimeofday(&tv, NULL);
            /* Log the absolute wallclock we just set (human-readable) so
             * it's obvious in serial output what time the chip now thinks
             * it is. */
            struct tm t;
            gmtime_r(&tv.tv_sec, &t);
            char buf[40];
            strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S UTC", &t);
            ESP_LOGI(TAG, "X-Server-Time-Epoch: %lld — system clock set to %s",
                     epoch, buf);
        } else {
            ESP_LOGW(TAG, "X-Server-Time-Epoch present but non-positive: '%s'", ctx.server_epoch_hdr);
        }
    } else {
        ESP_LOGW(TAG, "X-Server-Time-Epoch header missing from response (status=%d)", status);
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

/* Called after split_and_display() returns. If BUSY is still LOW the
 * display controller is wedged (image half-rendered, UC8179C stuck
 * mid-refresh). Normally recovers after an esp_restart cold boot —
 * the display re-initialises from scratch on the next attempt.
 *
 * Capped at MAX_BUSY_RECOVERY_REBOOTS consecutive attempts so a
 * permanently-broken display doesn't keep the chip in a reboot loop.
 * If we hit the cap, we log loudly and fall through to normal sleep
 * flow with whatever image state the screen is in — user will notice
 * and can intervene (factory-dump reflash etc.).
 *
 * Returns true if BUSY is healthy (HIGH / idle) so the caller knows
 * it's safe to proceed. On a reboot-triggered recovery this function
 * never returns.
 *
 * The counter also resets to 0 on a successful display, restoring
 * the full recovery budget for the next future failure. */
static bool check_display_busy_or_reboot(const char *context)
{
    int busy = gpio_get_level(PIN_EPAPER_BUSY);
    if (busy != 0) {
        /* BUSY HIGH — display idle and healthy. */
        if (consecutive_busy_timeouts > 0) {
            ESP_LOGI(TAG, "[%s] display recovered after %d reboot(s)",
                     context, (int)consecutive_busy_timeouts);
        }
        consecutive_busy_timeouts = 0;
        return true;
    }

    /* BUSY stuck LOW — something went wrong during display_dual. */
    if (consecutive_busy_timeouts < MAX_BUSY_RECOVERY_REBOOTS) {
        consecutive_busy_timeouts++;
        ESP_LOGW(TAG, "[%s] BUSY stuck LOW after display — rebooting to recover (attempt %d/%d)",
                 context, (int)consecutive_busy_timeouts, MAX_BUSY_RECOVERY_REBOOTS);
        /* Flush serial output before restart so the warning actually lands. */
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();
        /* Never returns */
    }

    ESP_LOGE(TAG, "[%s] BUSY stuck LOW after %d recovery reboot(s) — giving up",
             context, (int)consecutive_busy_timeouts);
    /* Reset so a future successful cycle re-enables the mechanism. */
    consecutive_busy_timeouts = 0;
    return false;
}

/* WiFi + download + display one image. Updates *sleep_seconds and
 * *server_epoch from the server's response headers (when present), and
 * stamps *local_time_at_download_us with esp_timer_get_time() at the
 * moment download completed — together those let the next boot compute
 * "actual slept" vs "expected slept" against the server's wall clock.
 * On success returns true and last_sleep_seconds is refreshed.
 * On failure triple-blinks WIFI_LED and returns false (current image
 * unchanged; out-params untouched). */
static bool fetch_and_display_image(int32_t *sleep_seconds,
                                    int64_t *server_epoch,
                                    int64_t *local_time_at_download_us,
                                    const char *wake_label,
                                    const char *caller,
                                    int64_t boot_time_us)
{
    uint8_t *img = NULL;
    if (wifi_connect()) {
        gpio_set_level(PIN_WIFI_LED, 1);
        img = download_image(sleep_seconds, server_epoch, wake_label, caller, boot_time_us);
        if (local_time_at_download_us) *local_time_at_download_us = esp_timer_get_time();
        wifi_shutdown();
        gpio_set_level(PIN_WIFI_LED, 0);
    }
    if (!img) {
        ESP_LOGW(TAG, "Fetch failed, keeping current image");
        for (int i = 0; i < 3; i++) {
            gpio_set_level(PIN_WIFI_LED, 1);
            vTaskDelay(pdMS_TO_TICKS(100));
            gpio_set_level(PIN_WIFI_LED, 0);
            vTaskDelay(pdMS_TO_TICKS(100));
        }
        return false;
    }
    if (*sleep_seconds > 0) last_sleep_seconds = *sleep_seconds;
    split_and_display(img);
    free(img);
    return true;
}

/* Single awake window used after every displayed image (normal boot, error
 * screen, or button-triggered refresh). Polls the two wake-capable buttons
 * (GPIO 1 and GPIO 12); on any press we fetch + display the next image and
 * extend the window for another full AWAKE_WINDOW_US so the user can keep
 * tapping through images. When the window expires with no press, return.
 *
 * The window also doubles as the reflash-via-esptool opportunity — the
 * chip stays active throughout, so a hardware reset from the host will
 * drop it into the ROM bootloader. */
static void stay_awake_with_buttons(int32_t *sleep_seconds,
                                    int64_t *server_epoch,
                                    int64_t *local_time_at_download_us,
                                    const char *wake_label,
                                    int64_t boot_time_us)
{
    /* Bring the buttons out of any RTC-peripheral mode before configuring
     * them as digital inputs. Needed because:
     *   - factory firmware leaves GPIO 1 / GPIO 12 configured as RTC wake
     *     sources with their hold state retained (hold survives chip reset
     *     up to POR), which makes gpio_config() silently ineffective —
     *     the pin keeps reading whatever the RTC peripheral is driving;
     *   - enter_deep_sleep rtc_gpio_init()'s them to attach the wake
     *     pull-ups, and on wake we need them back in digital mode.
     * Observed symptom without this: "button always pressed" — stay_awake
     * detects a press 50 ms after entry, every cycle. */
    if (rtc_gpio_is_valid_gpio(PIN_BUTTON_1)) {
        rtc_gpio_hold_dis(PIN_BUTTON_1);
        rtc_gpio_deinit(PIN_BUTTON_1);
    }
    if (rtc_gpio_is_valid_gpio(PIN_PWR_BUTTON)) {
        rtc_gpio_hold_dis(PIN_PWR_BUTTON);
        rtc_gpio_deinit(PIN_PWR_BUTTON);
    }
    gpio_reset_pin(PIN_BUTTON_1);
    gpio_reset_pin(PIN_PWR_BUTTON);

    /* Configure the two wake-capable buttons as polled inputs with the
     * internal pull-up engaged. GPIO 40 (legacy "switch photo" button)
     * isn't wake-capable on ESP32-S3 and is deliberately ignored. */
    gpio_config_t btn_cfg = {
        .pin_bit_mask = (1ULL << PIN_BUTTON_1) | (1ULL << PIN_PWR_BUTTON),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&btn_cfg);

    /* Let the internal pull-up settle before the first read. Bumped from
     * 20 ms → 100 ms after seeing "button always pressed" still happen
     * occasionally on first-boot-after-factory-firmware-flash despite
     * the RTC-GPIO de-isolation above. The pin has board-level
     * capacitance to overcome AND the internal pull-up is a relatively
     * weak 45 kΩ; 100 ms is well outside the RC time constant of any
     * plausible external load. */
    vTaskDelay(pdMS_TO_TICKS(100));

    /* Snapshot the initial state right after the settle delay. A pin
     * that reads LOW here is stuck (external hardware latched it LOW,
     * RTC isolation the de-init call didn't clear, board-level quirk),
     * and polling it for 0-as-press will fire immediately every cycle.
     *
     * Observed 2026-04-18: GPIO 12 reads LOW on first boot after flashing
     * factory firmware even with rtc_gpio_deinit + gpio_reset_pin +
     * pull-up + 100ms settle. We disable press-detection on any pin that
     * isn't HIGH at settle — user never pressed anything, so a pin
     * reading LOW here cannot be a real press. The pin is still a valid
     * deep-sleep wake source (EXT1 config in enter_deep_sleep); this
     * only suppresses the false "held = pressed" reading while awake. */
    int initial_btn1 = gpio_get_level(PIN_BUTTON_1);
    int initial_pwr  = gpio_get_level(PIN_PWR_BUTTON);
    bool btn1_pollable = (initial_btn1 == 1);
    bool pwr_pollable  = (initial_pwr  == 1);
    ESP_LOGI(TAG, "Awake for %ds — press button for next image (also: reflash window) "
                  "[initial: GPIO1=%d%s GPIO12=%d%s]",
             (int)(AWAKE_WINDOW_US / 1000000),
             initial_btn1, btn1_pollable ? "" : " STUCK-IGNORED",
             initial_pwr,  pwr_pollable  ? "" : " STUCK-IGNORED");

    int64_t deadline = esp_timer_get_time() + AWAKE_WINDOW_US;

    while (esp_timer_get_time() < deadline) {
        int btn1 = gpio_get_level(PIN_BUTTON_1);
        int pwr  = gpio_get_level(PIN_PWR_BUTTON);
        bool pressed = (btn1_pollable && btn1 == 0) ||
                       (pwr_pollable  && pwr  == 0);
        if (pressed) {
            vTaskDelay(pdMS_TO_TICKS(50));  /* debounce */
            int btn1b = gpio_get_level(PIN_BUTTON_1);
            int pwrb  = gpio_get_level(PIN_PWR_BUTTON);
            pressed = (btn1_pollable && btn1b == 0) ||
                      (pwr_pollable  && pwrb  == 0);
            if (pressed) {
                ESP_LOGI(TAG, "Button pressed — fetching next image "
                              "[GPIO1=%d GPIO12=%d]", btn1b, pwrb);
                /* Wait for release so a held button doesn't re-trigger.
                 * Only wait on pollable pins — a stuck-LOW pin would
                 * never release. */
                while ((btn1_pollable && gpio_get_level(PIN_BUTTON_1) == 0) ||
                       (pwr_pollable  && gpio_get_level(PIN_PWR_BUTTON) == 0)) {
                    vTaskDelay(pdMS_TO_TICKS(50));
                }
                fetch_and_display_image(sleep_seconds, server_epoch, local_time_at_download_us,
                                        wake_label, "button", boot_time_us);
                /* Reset the window so user gets a fresh 60s after the
                 * new image (or after the error-blink on failure). */
                deadline = esp_timer_get_time() + AWAKE_WINDOW_US;
                continue;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    ESP_LOGI(TAG, "Awake window expired — entering deep sleep");
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

/* Compute and store an estimate of the server epoch at the moment we're
 * about to enter deep sleep, by adjusting the most-recent server_epoch
 * forward by the local time elapsed since download. Pass server_epoch=0
 * to clear (e.g. after a download failure or on the spurious-reset path —
 * any next-boot sleep-error log would be meaningless). */
static void save_pre_sleep_epoch(int64_t server_epoch, int64_t local_time_at_download_us)
{
    if (server_epoch > 0) {
        int64_t delta_s = (esp_timer_get_time() - local_time_at_download_us) / 1000000LL;
        pre_sleep_server_epoch = server_epoch + delta_s;
    } else {
        pre_sleep_server_epoch = 0;
    }
}

/* Log a human-friendly "going to sleep for … / wake at …" line. Used
 * both before real deep sleep and before the USB polling loop. Takes
 * the duration in microseconds and the reason prefix so the same
 * formatter can be reused for both contexts. If the system clock has
 * been synced (post-2020 time) we also include the absolute wall-clock
 * target so you can match serial output against the server log. */
static void log_sleep_intent(const char *prefix, int64_t duration_us)
{
    int64_t secs = duration_us / 1000000LL;
    int h = (int)(secs / 3600);
    int m = (int)((secs % 3600) / 60);
    int s = (int)(secs % 60);
    time_t now_t = time(NULL);
    if (now_t > 1577836800) {
        time_t wake_t = now_t + (time_t)secs;
        struct tm wake_tm;
        gmtime_r(&wake_t, &wake_tm);
        char buf[40];
        strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S UTC", &wake_tm);
        ESP_LOGI(TAG, "%s %dh %dm %ds (wake at %s)", prefix, h, m, s, buf);
    } else {
        ESP_LOGI(TAG, "%s %dh %dm %ds (wall clock not synced)", prefix, h, m, s);
    }
}

/* Contract: sleep for `sleep_us` microseconds from the call site (actual
 * sleep may be slightly shorter if a USB-connected polling loop recomputes
 * the remaining time). The reflash / UI awake window is the caller's
 * responsibility — this function goes to sleep right away.
 * sleep_us == 0 means button-only wake (no timer, no deadline stored). */
static void enter_deep_sleep(int64_t sleep_us)
{
    /* Capture the deadline at entry, in the RTC clock's frame, and persist
     * it + was_sleeping to RTC memory. Survives both real deep sleep and
     * esp_restart (including the USB polling loop's restart), letting the
     * next boot distinguish "scheduled wake" from "spurious early reset". */
    uint64_t entry_rtc_us = esp_clk_rtc_time();
    uint64_t deadline_rtc_us = (sleep_us > 0) ? entry_rtc_us + (uint64_t)sleep_us : 0;
    was_sleeping = true;
    rtc_magic = RTC_MAGIC;
    scheduled_wake_rtc_us = deadline_rtc_us;

    int64_t remaining_us = (sleep_us > 0) ? sleep_us : 0;

    /* USB polling loop — only applies when USB power is connected. On USB,
     * deep_sleep_start would immediately cause a USB disconnect → host reset
     * → reboot loop; polling in light sleep keeps the device reachable for
     * reflashing. chg_monitor stays running so the LED blinks throughout.
     *
     * Exit condition is the RTC clock, NOT an accumulator of
     * vTaskDelay() durations: pdMS_TO_TICKS rounds down to the nearest
     * tick (10 ms at 100 Hz), so summing nominal-1-second chunks
     * under-counts real time by ~1 % per chunk. Over a 12-hour refresh
     * interval that drift reaches ~7 minutes — enough to exit the loop
     * and esp_restart() well BEFORE the deadline, after which the next
     * boot's classifier would see "gap > SLACK" and skip the fetch.
     * Checking esp_clk_rtc_time() directly sidesteps the drift entirely. */
    bool usb_connected = (gpio_get_level(PIN_CHG_STATUS) == 0);
    if (usb_connected && remaining_us > 0) {
        log_sleep_intent("USB connected — polling loop for", remaining_us);
        last_sleep_mode = LAST_SLEEP_MODE_USB_POLLING;
        while (1) {
            /* Single read per iteration: the while-condition + later
             * subtraction were two separate clock reads, so a context
             * switch in between could push `now` past the deadline and
             * make `deadline - now` underflow uint64_t to a huge value
             * (then chunk_ms = 1000, one wasted second of vTaskDelay).
             *
             * NOTE: previously this loop also bailed out when CHG_STATUS
             * flipped HIGH, intending to detect "USB unplugged". But
             * CHG_STATUS means "actively charging", not "cable connected" —
             * so a battery topping off (legitimately going HIGH while
             * still plugged in) caused a fall-through to real deep sleep,
             * followed by USB-host-reset, followed by spurious-reset
             * short-path loop, hitting the safety valve every few minutes
             * and triggering a full refresh. That's the "refreshes every
             * few minutes on USB" bug. We now poll until the deadline
             * regardless of CHG_STATUS. Consequence: if the user truly
             * unplugs USB mid-sleep, the chip stays awake polling until
             * deadline (battery drain). Acceptable trade-off vs the
             * reset-loop. */
            uint64_t now_rtc_us = esp_clk_rtc_time();
            if (now_rtc_us >= deadline_rtc_us) break;
            uint64_t left_us = deadline_rtc_us - now_rtc_us;
            int chunk_ms = (left_us > 1000000ULL) ? 1000 : (int)(left_us / 1000);
            if (chunk_ms < 1) chunk_ms = 1;
            vTaskDelay(pdMS_TO_TICKS(chunk_ms));
        }
        ESP_LOGI(TAG, "Wait complete — restarting");
        chg_monitor_stop();
        esp_restart();
        /* Never returns */
    }

    /* Real deep sleep prep. Stop chg_monitor last so the LED keeps
     * blinking until the moment the chip powers down. */
    chg_monitor_stop();
    gpio_set_level(PIN_SYS_POWER, 0);
    gpio_set_level(PIN_WORK_LED, 0);

    /* Shut down SPI bus — guarded because enter_deep_sleep can be called
     * on the is_usb_reset_after_sleep path BEFORE spi_init(), in which case
     * spi_handle is still NULL. spi_bus_remove_device(NULL) is undefined. */
    if (spi_handle != NULL) {
        spi_bus_remove_device(spi_handle);
        spi_bus_free(SPI2_HOST);
        spi_handle = NULL;
    }

    /* Configure timer wakeup for the actual remaining time, not the
     * original caller-requested sleep_us. */
    if (remaining_us > 0) {
        esp_sleep_enable_timer_wakeup((uint64_t)remaining_us);
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

    if (remaining_us > 0) {
        log_sleep_intent("Entering deep sleep for", remaining_us);
    } else {
        ESP_LOGI(TAG, "Entering deep sleep (no timer, button wake only)");
    }

    /* Mark our sleep path for the next boot's X-Frame-State report. */
    last_sleep_mode = LAST_SLEEP_MODE_DEEP_SLEEP;

    esp_deep_sleep_start();
    /* Never returns */
}

/* ═══════════════════════════════════════════════════════════════════
 *  Main
 * ═══════════════════════════════════════════════════════════════════ */

void app_main(void)
{
    /* Validate RTC memory. After an esptool flash (hard reset), RTC memory
     * retains stale values from the previous firmware run. The magic value
     * lets us detect this and treat it as a fresh boot. */
    if (rtc_magic != RTC_MAGIC) {
        /* RTC memory is stale — clear everything */
        rtc_magic = 0;
        boot_count = 0;
        wifi_channel = 0;
        memset(wifi_bssid, 0, sizeof(wifi_bssid));
        has_wifi_cache = false;
        last_battery_mv = 0;
        was_sleeping = false;
        last_sleep_seconds = 0;
        scheduled_wake_rtc_us = 0;
        consecutive_spurious_resets = 0;
        consecutive_busy_timeouts = 0;
        pre_sleep_server_epoch = 0;
        last_sleep_err_s = 0;
        last_sleep_err_valid = false;
        last_sleep_mode = LAST_SLEEP_MODE_NONE;
        /* Also reset the system clock. ESP-IDF stores its wallclock
         * offset in a separate RTC variable (s_boot_time) that isn't
         * zeroed by our rtc_magic check and also survives esp_restart
         * + DTR/RTS chip reset (only a real POR wipes it). Without this,
         * a freshly-flashed frame inherits whatever time the previous
         * firmware synced, reports misleading clk_drift on its very
         * first call, and makes log timestamps confusing. Clear so our
         * first clk_now reading only appears AFTER a fresh server sync. */
        struct timeval tv = {0, 0};
        settimeofday(&tv, NULL);
    }

    /* Mark RTC memory as valid for subsequent esp_restart() paths. Previously
     * we only set this inside enter_deep_sleep(), which meant an esp_restart
     * from the display-recovery or spurious-reset valves before ever reaching
     * deep sleep would boot with rtc_magic still stale — the validation
     * block above would then zero out every persisted counter on every
     * retry, defeating the MAX_BUSY_RECOVERY_REBOOTS / spurious-reset caps
     * and producing an infinite boot loop (observed 2026-04-18 with the
     * DRF-timing reboot valve). Setting magic here makes the counters
     * genuinely survive esp_restart, so the caps actually cap. Power-cycle
     * / flash-erase still lands on uninit RTC memory → rtc_magic != RTC_MAGIC
     * → fresh state, which is the only reset path we want. */
    rtc_magic = RTC_MAGIC;

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

    /* Wait for USB-Serial/JTAG console to connect so boot logs are visible.
     * Flashing via esptool works regardless of this delay — esptool resets
     * the chip into ROM bootloader before app_main runs. */
    vTaskDelay(pdMS_TO_TICKS(5000));

    /* Determine wakeup cause */
    esp_sleep_wakeup_cause_t wakeup = esp_sleep_get_wakeup_cause();
    bool is_scheduled_wake = (wakeup == ESP_SLEEP_WAKEUP_TIMER || wakeup == ESP_SLEEP_WAKEUP_EXT1);

    /* An "unexplained" wake is anything that wasn't a timer/button but
     * landed here with was_sleeping set — USB host disconnect resetting
     * the chip, brownout, or ESP32-S3 silicon quirk misreporting a timer
     * wake as UNDEFINED. We can't tell them apart by wakeup cause alone,
     * but the RTC slow clock can: clearly-before-deadline = real early
     * reset (skip fetch, sleep remainder); at-or-past-deadline = timer
     * fired but was misreported (must fetch).
     *
     * esp_clk_rtc_time() keeps ticking through deep sleep and esp_restart,
     * and the deep-sleep timer uses the same RTC slow clock, so drift
     * between them cancels out to first order. */
    const bool prior_sleep = was_sleeping;
    was_sleeping = false;

    bool is_usb_reset_after_sleep = false;
    if (!is_scheduled_wake && prior_sleep && scheduled_wake_rtc_us != 0) {
        uint64_t now_rtc_us = esp_clk_rtc_time();
        if (now_rtc_us < scheduled_wake_rtc_us) {
            uint64_t gap_us = scheduled_wake_rtc_us - now_rtc_us;
            if (gap_us > SCHEDULED_WAKE_SANE_MAX_US) {
                /* Implausible gap → RTC counter reset while RTC memory
                 * survived. Treat the stored deadline as garbage. */
                ESP_LOGW(TAG, "Implausible deadline gap (%.1fh) — discarding",
                         gap_us / 3600000000.0);
                scheduled_wake_rtc_us = 0;
            } else if (gap_us > SCHEDULED_WAKE_SLACK_US) {
                /* Clearly before the deadline — early reset. */
                is_usb_reset_after_sleep = true;
            }
            /* else: within slack of deadline → misclassified timer wake, fetch. */
        }
        /* else: at/past deadline → misclassified timer wake, fetch. */
    }

    /* Short machine-readable wake label used both for the human log and
     * the X-Frame-State JSON sent to the server. */
    const char *wake_label =
        is_usb_reset_after_sleep ? "spurious" :
        wakeup == ESP_SLEEP_WAKEUP_TIMER ? "timer" :
        wakeup == ESP_SLEEP_WAKEUP_EXT1 ? "button" :
        prior_sleep ? "misclassified" :
        "first_boot";

    ESP_LOGI(TAG, "Boot #%d, wakeup=%d (%s)", boot_count, wakeup, wake_label);
    /* NOTE: only TIMER + EXT1 are enabled as wake sources; other values
     * from esp_sleep_get_wakeup_cause() (EXT0, ULP, GPIO, UART, TOUCHPAD)
     * shouldn't occur under our configuration. If one does, the classifier
     * above treats it as !is_scheduled_wake, so it falls through to the
     * deadline-based analysis like any other unexplained wake. */

    /* Early-exit for "spurious reset before deadline" — skip display/SPI/WiFi
     * init entirely and go straight back to sleep. Saves ~600 ms of CPU +
     * display-rail power per spurious reset.
     *
     * SAFETY VALVE: if we've taken this shortcut MAX_SPURIOUS_RESETS times
     * in a row, fall through to the full path so the user gets a 60 s
     * awake window for reflash. Without this, a chip that keeps spurious-
     * resetting (e.g. brownout-during-sleep, persistent silicon quirk)
     * could be unreflashable — only ~5 s awake per cycle, none of it with
     * peripherals initialised. */
    if (is_usb_reset_after_sleep) {
        if (consecutive_spurious_resets < MAX_SPURIOUS_RESETS) {
            consecutive_spurious_resets++;
            ESP_LOGI(TAG, "Early wake (spurious-reset short-path #%d/%d) — image already on display",
                     consecutive_spurious_resets, MAX_SPURIOUS_RESETS);
            uint64_t now_rtc_us = esp_clk_rtc_time();
            int64_t sleep_us = (now_rtc_us < scheduled_wake_rtc_us)
                ? (int64_t)(scheduled_wake_rtc_us - now_rtc_us)
                : 60LL * 1000000LL;
            /* No fresh download → no fresh epoch. Disable next-boot sleep
             * check so we don't log a misleading actual-vs-expected. */
            pre_sleep_server_epoch = 0;
            enter_deep_sleep(sleep_us);
            /* Never returns */
        }
        /* Hit the cap — break the loop by taking the full path so the
         * user can reflash. Counter resets below. */
        ESP_LOGW(TAG, "Spurious-reset count hit %d — forcing full awake window for reflash",
                 consecutive_spurious_resets);
    }
    /* Past the shortcut: any non-spurious wake (and the cap-exceeded
     * fall-through) resets the counter. */
    consecutive_spurious_resets = 0;

    /* Hardware init — only runs on paths that actually need the display */
    hw_gpio_init();
    /* hw_gpio_init already drives SYS_POWER HIGH. Previously we also drove
     * EPAPER_PWR_EN (GPIO 3) HIGH here. Dropped on 2026-04-18 after
     * .private/boot_analysis/FINAL_FINDINGS.md confirmed the original
     * factory firmware *never* drives GPIO 3 HIGH anywhere in the binary
     * (zero calls across both gpio_set_level entry points after fixing
     * the disassembly alignment bug). If GPIO 3 is an active-LOW enable
     * or should stay floating, driving it HIGH would silently disable
     * the display rail — matching our "controller is dead, BUSY never
     * goes LOW in response to commands" symptom. Drive it LOW explicitly
     * so behaviour is well-defined. */
    gpio_set_level(PIN_EPAPER_PWR_EN, 0);
    gpio_set_level(PIN_WORK_LED, 1);
    gpio_set_level(PIN_WIFI_LED, 0);

    /* Start charger monitor (blinks LED while charging) */
    chg_monitor_start();

    vTaskDelay(pdMS_TO_TICKS(500));  /* let display controller fully power up */
    spi_init();

    /* Read battery */
    last_battery_mv = read_battery_mv();
    ESP_LOGI(TAG, "Battery: %d mV", last_battery_mv);

    /* Check config version — do this before the USB-reset shortcut so
     * error screens are always shown even after a reset. */
    if (config.cfg_ver != CONFIG_VERSION) {
        ESP_LOGE(TAG, "Config version mismatch: got %d, need %d", config.cfg_ver, CONFIG_VERSION);
        char msg[256];
        snprintf(msg, sizeof(msg),
                 "Config version\n"
                 "mismatch.\n"
                 "\n"
                 "Expected: %d\n"
                 "Found: %d\n"
                 "\n"
                 "Run hokku-setup to\n"
                 "reconfigure.\n"
                 "\n"
                 "Press reset to\n"
                 "try again.",
                 CONFIG_VERSION, config.cfg_ver);
        display_message(msg);
        /* Awake window so the user can reflash. Buttons won't help here
         * (config is broken), but pressing one will trigger a fetch attempt
         * that fails with the LED-blink feedback — harmless. */
        int32_t dummy_sleep = 0;
        int64_t dummy_epoch = 0;
        int64_t dummy_local_us = 0;
        stay_awake_with_buttons(&dummy_sleep, &dummy_epoch, &dummy_local_us,
                                wake_label, boot_time);
        pre_sleep_server_epoch = 0;
        enter_deep_sleep(0);
        /* Never returns */
    }

    /* Check config validity */
    if (!config_is_valid()) {
        ESP_LOGE(TAG, "No valid config found. Display setup message.");
        display_message(
            "Hokku installed but\n"
            "cannot read config.\n"
            "\n"
            "Connect USB and run\n"
            "hokku-setup to\n"
            "configure.\n"
            "\n"
            "Press reset to\n"
            "try again."
        );
        int32_t dummy_sleep = 0;
        int64_t dummy_epoch = 0;
        int64_t dummy_local_us = 0;
        stay_awake_with_buttons(&dummy_sleep, &dummy_epoch, &dummy_local_us,
                                wake_label, boot_time);
        pre_sleep_server_epoch = 0;
        enter_deep_sleep(0);
        /* Never returns */
    }

    /* Early reset (USB host disconnect, brownout, etc.) before the scheduled
       wake deadline: config is valid, image is already on display, skip the
       fetch and sleep only the time remaining to the original deadline.
       enter_deep_sleep honors its sleep_us contract (reflash window is
       internal, not additive), so the deadline doesn't drift. */
    /* (is_usb_reset_after_sleep branch is handled earlier, before display init) */

    /* ── Normal boot path: WiFi → download (with sleep header) → display ── */

    int32_t sleep_seconds = 0;
    int64_t server_epoch = 0;
    int64_t local_time_at_download_us = 0;
    uint8_t *img = NULL;

    if (wifi_connect()) {
        gpio_set_level(PIN_WIFI_LED, 1);
        img = download_image(&sleep_seconds, &server_epoch,
                             wake_label, "wake", boot_time);
        local_time_at_download_us = esp_timer_get_time();
        wifi_shutdown();
        gpio_set_level(PIN_WIFI_LED, 0);
    }

    /* Sleep-accuracy check: only meaningful on a real timer wake where we
     * have both a pre-sleep epoch from the previous run and a fresh epoch
     * from this run. Compares server-wall-clock elapsed (since pre-sleep
     * snapshot) minus this boot's awake-before-download time against the
     * sleep_seconds we asked for last time. Negative = woke early,
     * positive = overslept. Also stored in RTC so the NEXT boot's
     * X-Frame-State can include the measured error.
     *
     * Note we DON'T invalidate a previously-measured error on boots
     * where we can't produce a fresh one (button wakes, etc.) — the
     * last good measurement remains interesting info for the user. */
    if (wakeup == ESP_SLEEP_WAKEUP_TIMER && pre_sleep_server_epoch > 0 &&
        server_epoch > 0 && last_sleep_seconds > 0) {
        int64_t time_awake_s = local_time_at_download_us / 1000000LL;
        int64_t wall_elapsed_s = server_epoch - pre_sleep_server_epoch;
        int64_t actual_slept_s = wall_elapsed_s - time_awake_s;
        int64_t expected_slept_s = last_sleep_seconds;
        int64_t error_s = actual_slept_s - expected_slept_s;
        ESP_LOGI(TAG, "Sleep check: expected=%llds actual=%llds error=%+llds",
                 expected_slept_s, actual_slept_s, error_s);
        /* Clamp to int32 range for RTC storage — sleeps over ~68 years
         * would overflow but are obviously out of scope. */
        if (error_s > INT32_MAX) error_s = INT32_MAX;
        if (error_s < INT32_MIN) error_s = INT32_MIN;
        last_sleep_err_s = (int32_t)error_s;
        last_sleep_err_valid = true;
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

        ESP_LOGE(TAG, "Download failed, displaying error message.");
        display_message(msg);
        /* Awake window so the user can reflash OR press a button to retry
         * the fetch. If a retry succeeds inside stay_awake, sleep_seconds
         * + server_epoch + local_time_at_download_us all get updated from
         * the server response; otherwise fall back to 3h. */
        stay_awake_with_buttons(&sleep_seconds, &server_epoch, &local_time_at_download_us,
                                wake_label, boot_time);
        /* Same "subtract elapsed since last successful download" correction
         * as the success path below. If we never got a successful download
         * (local_time_at_download_us still 0), fall back to a flat 3h retry. */
        int64_t fail_sleep_us;
        if (sleep_seconds > 0 && local_time_at_download_us > 0) {
            int64_t elapsed_us = esp_timer_get_time() - local_time_at_download_us;
            fail_sleep_us = (int64_t)sleep_seconds * 1000000LL - elapsed_us;
            if (fail_sleep_us < 5 * 1000000LL) fail_sleep_us = 5 * 1000000LL;
        } else {
            fail_sleep_us = SLEEP_3H_US;
        }
        save_pre_sleep_epoch(server_epoch, local_time_at_download_us);
        enter_deep_sleep(fail_sleep_us);
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

    /* Note: epaper_display_dual ended by driving SYS_POWER LOW to match
     * the June original's per-update teardown. We deliberately leave it
     * LOW through the awake window + USB polling to save power. The
     * next refresh cold-boots the controller via display_dual's own
     * SYS_POWER LOW-then-HIGH sequence at entry. */

    /* Recover from a wedged display controller by rebooting. If BUSY is
     * still LOW here, epaper_display_dual hit one or more BUSY timeouts
     * and the screen is in a half-rendered state. A fresh cold-boot of
     * the controller (via esp_restart, which re-runs hw_gpio_init +
     * spi_init + epaper_reset from scratch) almost always un-sticks it.
     * Capped so a permanently-broken display doesn't drain the battery
     * in a reboot loop. This function may not return. */
    check_display_busy_or_reboot("main fetch");

    /* Unified awake window: same path for true first boot, scheduled wakes,
     * button wakes, and misclassified-but-at-deadline wakes. Gives the
     * user 60s to press a button for the next image, and serves as the
     * reflash-via-esptool opportunity. Button presses extend the window. */
    stay_awake_with_buttons(&sleep_seconds, &server_epoch, &local_time_at_download_us,
                            wake_label, boot_time);

    /* Safety net: if we somehow reach here with sleep_seconds <= 0 (missing
     * or zero X-Sleep-Seconds header, parser glitch, whatever), never arm
     * a zero-length timer — that would leave the chip in button-only-wake
     * state, which is how one frame in the wild missed its 06:00 refresh
     * entirely and needed a physical button press to come back. Prefer the
     * last-known-good sleep duration; fall back to 3h so the chip retries
     * instead of sleeping forever. */
    if (sleep_seconds <= 0) {
        ESP_LOGW(TAG, "sleep_seconds <= 0 — falling back (last=%d)", (int)last_sleep_seconds);
        sleep_seconds = (last_sleep_seconds > 0) ? last_sleep_seconds : (int32_t)(SLEEP_3H_US / 1000000LL);
    }

    /* X-Sleep-Seconds is the interval the server expects between *downloads*,
     * computed from the server's clock to hit the next scheduled refresh_time
     * (or DEBUG_FAST_REFRESH_SECONDS in debug mode). So we need to deduct the
     * time already spent since the download — the ~20s display refresh, the
     * 60s awake window, any retry button-press extensions. Without this we
     * were oversleeping by ~(display+awake) every cycle: 180s debug mode
     * was effectively 270s, 6h refresh drifted ~90s late per cycle.
     *
     * Floor at MIN_SLEEP_US so a very short server interval + a slow display
     * refresh can't arm a zero/negative deadline (which would behave as
     * button-only-wake — the same foot-gun the <=0 safety net above catches). */
    int64_t elapsed_us = esp_timer_get_time() - local_time_at_download_us;
    int64_t sleep_us = (int64_t)sleep_seconds * 1000000LL - elapsed_us;
    const int64_t MIN_SLEEP_US = 5 * 1000000LL;
    if (sleep_us < MIN_SLEEP_US) {
        ESP_LOGW(TAG, "sleep_us capped at MIN: server=%ds, elapsed=%llds → %llds",
                 (int)sleep_seconds, elapsed_us / 1000000LL, MIN_SLEEP_US / 1000000LL);
        sleep_us = MIN_SLEEP_US;
    } else {
        ESP_LOGI(TAG, "sleep_us = %llds (server=%ds, elapsed=%llds since download)",
                 sleep_us / 1000000LL, (int)sleep_seconds, elapsed_us / 1000000LL);
    }
    save_pre_sleep_epoch(server_epoch, local_time_at_download_us);
    enter_deep_sleep(sleep_us);
    /* Never returns */
}
