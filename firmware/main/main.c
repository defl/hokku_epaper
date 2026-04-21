/*
 * Hokku 13.3" Spectra-6 E-Paper Frame — Custom Firmware
 * UC8179C dual-panel controller, 1200x800, SPI interface
 *
 * Design reference: firmware.md (root of repo) describes the state machine
 * this implements. Hardware map + USB-detect findings in HARDWARE_FACTS.md.
 *
 * State machine summary:
 *   - USB_AWAKE     GPIO 14 LOW (computer USB): full-power, logs on, never
 *                   deep-sleep (single process lifetime, no esp_restart)
 *   - BATTERY_IDLE  GPIO 14 HIGH: 5 s awake window then deep sleep. Logs off.
 *   - DEEP_SLEEP    EXT1 wake on GPIO 1 (button) or GPIO 14 (USB plug) or timer
 *   - REFRESH       transient — fetch + display, return to enclosing regime
 *
 * Non-obvious rules (from spec, cross-reference firmware.md):
 *   - Boot NEVER auto-refreshes. Only button / schedule / first-time install.
 *   - Button press = esp_restart() with RTC flag → guaranteed fresh state.
 *   - Sleep duration anchored to server epoch (absolute), not relative-to-now.
 *   - GPIO 14 named USB_HOST_DETECT (renamed from CHG_STATUS): it is a
 *     USB-BC host-detect signal, not pure VBUS-detect. Wall chargers do NOT
 *     trigger it — treated as battery mode, which is fine per spec.
 *   - All RTC state uses RTC_NOINIT_ATTR (not RTC_DATA_ATTR) so counters and
 *     clock offset actually survive esp_restart.
 */

#include <string.h>
#include <stdio.h>
#include <inttypes.h>
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
 * esp_restart(). No public replacement exists in current IDF. */
#include "esp_private/esp_clk.h"

static const char *TAG = "hokku";

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
#define PIN_BUTTON_1         1   /* "next image" button, active LOW, RTC-wake capable */
#define PIN_PWR_BUTTON      12   /* Power button — transitions in lockstep with GPIO 14
                                  * on USB-plug events. Not used as an independent
                                  * button source. See HARDWARE_FACTS.md "USB Detection". */
#define PIN_BUTTON_2        40   /* Legacy "switch photo" (NOT RTC wake-capable) */
#define PIN_BUTTON_3        39
#define PIN_WORK_LED         2   /* Red LED (charge indicator) */
#define PIN_WIFI_LED        38   /* WiFi LED (LEDC PWM) */
#define PIN_BATT_ADC         5   /* ADC1_CH4, 3.34:1 divider */
#define PIN_CHG_EN1          4   /* Charger enable (active LOW) */
#define PIN_CHG_EN2         13   /* Charger enable (active LOW) */
#define PIN_USB_DETECT      14   /* LOW = computer USB host present. See HARDWARE_FACTS.md */

/* ── Display parameters ──────────────────────────────────────────── */
#define DISPLAY_W          1200
#define DISPLAY_H           800
#define PANEL_W             600
#define PANEL_SIZE         (DISPLAY_W * DISPLAY_H / 2)  /* 4bpp = 480000 per panel */
#define TOTAL_IMAGE_SIZE   (PANEL_SIZE * 2)
#define SPI_CHUNK_SIZE     4800

#define ROW_BYTES       (DISPLAY_W / 2)
#define ROWS_PER_CHUNK  (SPI_CHUNK_SIZE / ROW_BYTES)
#define NUM_CHUNKS      (DISPLAY_H / ROWS_PER_CHUNK)

#define COLOR_WHITE_BYTE   0x11

/* ── Network / timeouts ──────────────────────────────────────────── */
#define WIFI_CONNECT_TIMEOUT_MS  15000
#define HTTP_TIMEOUT_MS          30000

/* ── Battery ─────────────────────────────────────────────────────── */
#define BATT_LOW_MV        3400
#define BATT_CHARGE_MV     3300
#define BATT_DIVIDER_MULT  3.34f

/* ── Regime timings ──────────────────────────────────────────────── */
/* Battery-mode awake window — spec minimum is 5 s. We use it for
 * honouring any button-press arriving mid-sleep-entry, and for basic
 * reflash reachability if USB appears during the window. */
#define BATTERY_AWAKE_WINDOW_US  (5LL * 1000000LL)

/* Polling interval in both awake regimes. 100 ms is well under any
 * human-noticeable button latency and irrelevant on USB power. */
#define POLL_INTERVAL_MS  100

/* Button debounce: 2 consecutive LOW reads (≥ 200 ms) before we commit
 * to "user pressed the next-image button". Filters both mechanical bounce
 * and the ~100 ms artefact from GPIO 12 racing with GPIO 14 on USB plug. */
#define BUTTON_DEBOUNCE_SAMPLES  2

/* Fallback sleep durations when we have no server-provided schedule */
#define SLEEP_FALLBACK_3H_US  (3LL * 3600 * 1000000LL)

/* Retry delay when a refresh attempt fails (WiFi down, server unreachable,
 * server returned nonsense sleep_seconds). Applied as the next
 * next_refresh_epoch so the regime loops don't hot-retry at 100 ms. */
#define REFRESH_RETRY_SECONDS  60

/* Safety cap — prevent spurious wakes (USB host disconnect resetting the
 * chip, brownouts, silicon quirks) from burning through the battery via
 * repeated boot cycles. After MAX_SPURIOUS_RESETS in a row, fall through
 * to the full awake window instead of short-pathing back to sleep so the
 * user has a reflash window. */
#define MAX_SPURIOUS_RESETS  3

/* ── RTC memory (survives deep sleep + esp_restart) ──────────────────
 *
 * RTC_NOINIT_ATTR (not RTC_DATA_ATTR): initialisers are NOT respected,
 * section is not reloaded on any reset. On true POR the values are
 * garbage — the rtc_magic check inside app_main catches that and
 * zero-initialises everything exactly once.
 *
 * RTC_DATA_ATTR would be wrong: it's re-initialised on every esp_restart,
 * which was the root cause of the "boot_count always 1, clk_now always 0"
 * bug in the pre-redesign firmware. See HARDWARE_FACTS.md deep-sleep notes. */
#define RTC_MAGIC 0x484F4B55  /* "HOKU" — validates RTC memory after POR / flash */

RTC_NOINIT_ATTR static uint32_t rtc_magic;
RTC_NOINIT_ATTR static uint32_t boot_count;

/* Cached WiFi state for fast reconnect */
RTC_NOINIT_ATTR static uint8_t  wifi_channel;
RTC_NOINIT_ATTR static uint8_t  wifi_bssid[6];
RTC_NOINIT_ATTR static bool     has_wifi_cache;

/* Most-recent battery reading, passed to the server in X-Frame-State. */
RTC_NOINIT_ATTR static uint16_t last_battery_mv;

/* Last server-provided sleep interval (seconds), in case the next boot's
 * download fails and we need a fallback. */
RTC_NOINIT_ATTR static int32_t  last_sleep_seconds;

/* Next-refresh schedule expressed as absolute server epoch seconds.
 * This is the canonical schedule anchor — we compute deep-sleep
 * duration from (next_refresh_epoch - now_epoch), not from relative
 * time-since-download. That way display + awake time doesn't drift
 * the next-wake moment later each cycle. 0 = not scheduled. */
RTC_NOINIT_ATTR static int64_t  next_refresh_epoch;

/* Pre-sleep server-epoch snapshot, used to compute "actual vs expected
 * sleep duration" on the next wake for the sleep_err_s diagnostic.
 * 0 = not yet measured (reported as JSON null in X-Frame-State). */
RTC_NOINIT_ATTR static int64_t  pre_sleep_server_epoch;
RTC_NOINIT_ATTR static int32_t  last_sleep_err_s;
RTC_NOINIT_ATTR static bool     last_sleep_err_known;  /* true once we've recorded at least one error */

/* Spurious-wake safety counter. */
RTC_NOINIT_ATTR static uint8_t  consecutive_spurious_resets;

/* How we got here, for the next boot's X-Frame-State last_sleep field. */
#define LAST_SLEEP_MODE_NONE              0
#define LAST_SLEEP_MODE_DEEP_SLEEP        1
#define LAST_SLEEP_MODE_USB_RESTART       2  /* left USB_AWAKE via button-restart */
#define LAST_SLEEP_MODE_BATTERY_RESTART   3  /* left BATTERY_IDLE via button-restart */
RTC_NOINIT_ATTR static uint8_t  last_sleep_mode;

/* Button-press-triggered refresh marker. Set when the poll loop in
 * USB_AWAKE or BATTERY_IDLE detects a debounced button press; followed
 * by esp_restart(). On boot we check this and, if set, do a refresh
 * THEN continue into the regime selected by current usb_host state.
 *
 * Cleared EARLY in app_main (before attempting the refresh) so a
 * crashing refresh doesn't trap us in a button-induced restart loop. */
#define ACTION_NONE                 0
#define ACTION_REFRESH_FROM_BUTTON  1
RTC_NOINIT_ATTR static uint8_t  pending_action;

/* ── NVS config (persisted across flashes via hokku-setup) ────────── */
#define CONFIG_VERSION  1

typedef struct {
    uint8_t cfg_ver;
    char wifi_ssid[33];
    char wifi_pass[65];
    char image_url[257];
    char screen_name[65];
} config_t;

static config_t config = {0};

static bool config_load(void)
{
    nvs_handle_t nvs;
    if (nvs_open("hokku", NVS_READONLY, &nvs) != ESP_OK) return false;
    nvs_get_u8(nvs, "cfg_ver", &config.cfg_ver);
    size_t len;
    len = sizeof(config.wifi_ssid);  nvs_get_str(nvs, "wifi_ssid",   config.wifi_ssid,   &len);
    len = sizeof(config.wifi_pass);  nvs_get_str(nvs, "wifi_pass",   config.wifi_pass,   &len);
    len = sizeof(config.image_url);  nvs_get_str(nvs, "image_url",   config.image_url,   &len);
    len = sizeof(config.screen_name);nvs_get_str(nvs, "screen_name", config.screen_name, &len);
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
static void log_level_apply(bool usb_awake);

/* ── Shared globals ──────────────────────────────────────────────── */
static spi_device_handle_t spi_handle;
static EventGroupHandle_t  wifi_events;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

/* Set by wifi_connect() on each successful connect: true iff the fast-
 * reconnect path (cached BSSID + channel) actually worked. False if we
 * fell through to full scan, or if this is a first-time connect. Read by
 * build_frame_state_json to surface the hit-rate to the server. */
static bool last_wifi_used_cache = false;

/* Runtime regime string — set when entering USB_AWAKE / BATTERY_IDLE so
 * the frame-state builder can report what state the firmware is in RIGHT
 * NOW (the `wake` field says how we got here; this says what we're doing).
 * "boot" during early init before regime dispatch. */
static const char *current_regime = "boot";

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
        .pin_bit_mask = (1ULL << PIN_USB_DETECT),
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
    /* Step 0: restore BUSY to INPUT. The previous refresh's shutdown
     * sequence switched BUSY to OUTPUT-LOW to bleed the signal line
     * before cutting SYS_POWER (see ANALYSIS_FINAL.md). If we left it
     * that way, epaper_wait_busy would read our own output (LOW) and
     * timeout instead of seeing the controller's ready signal.
     *
     * Configuring with pull-disabled matches hw_gpio_init's original
     * INPUT config — the external pull-up on the PCB handles the
     * idle-HIGH state; any internal pull-up would fight the controller. */
    gpio_config_t busy_in = {
        .pin_bit_mask = (1ULL << PIN_EPAPER_BUSY),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
    };
    gpio_config(&busy_in);

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
    /* BUSY is INPUT from our side during normal operation (so we can
     * poll the controller's status). The factory firmware briefly
     * switches it to OUTPUT + drives LOW during this teardown — see
     * ANALYSIS_FINAL.md "drives while still output" — to bleed residual
     * voltage from the BUSY signal line alongside the other signal pins.
     * We restore it to INPUT at the start of the next display cycle
     * (see epaper_display_dual's pre-reset block). */
    gpio_set_direction(PIN_EPAPER_BUSY, GPIO_MODE_OUTPUT);

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

/* Helper: check ESP-IDF error, log and return false on failure (non-fatal).
 * Used inside wifi_connect to avoid ESP_ERROR_CHECK panic-on-error — a
 * transient WiFi driver hiccup would otherwise crash the USB_AWAKE regime
 * that's supposed to stay alive forever. */
#define WIFI_TRY(expr) do {                                             \
    esp_err_t __err = (expr);                                           \
    if (__err != ESP_OK) {                                              \
        ESP_LOGW(TAG, "%s -> %s (continuing)", #expr, esp_err_to_name(__err)); \
        return false;                                                   \
    }                                                                   \
} while (0)

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

    /* WIFI_AUTH_OPEN as the threshold accepts any auth level the AP
     * advertises (OPEN / WEP / WPA / WPA2 / WPA3). Previously we hard-
     * coded WPA2_PSK which worked on our WPA3-SAE AP only because
     * ESP-IDF negotiates leniently; if the AP flips to WPA3-only it
     * would reject our association silently. OPEN means "I'll accept
     * whatever you offer." */
    wifi_config_t wifi_cfg = {
        .sta = {
            .threshold.authmode = WIFI_AUTH_OPEN,
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

    WIFI_TRY(esp_wifi_set_mode(WIFI_MODE_STA));
    WIFI_TRY(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    WIFI_TRY(esp_wifi_start());
    WIFI_TRY(esp_wifi_connect());

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
        /* Explicit: cache path worked if we were ATTEMPTING the cache.
         * Matches wifi_cfg.sta.bssid_set, which was only set in the
         * cache-attempt branch above. */
        last_wifi_used_cache = wifi_cfg.sta.bssid_set;
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
            /* Explicit: full-scan branch, cache was NOT used. */
            last_wifi_used_cache = false;
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
 *
 * Schema (see the matching client-side parse in webserver.py):
 *   fw            firmware version string
 *   boot          boot counter (incremented every app_main; RTC-persistent)
 *   wake          how we got here: first_boot | button_restart | timer |
 *                 button_wake | usb_sched
 *   regime        what we're doing right now: usb_awake | battery_idle | boot
 *   uptime_s      seconds since the current app_main entry
 *   bat_mv        battery voltage in millivolts (most recent ADC read)
 *   usb           "host" (computer USB enumerated) | "none"
 *                 NB: NOT the same as "charging" — wall charger reads "none"
 *                     because GPIO 14 is a USB-host-detect signal, not VBUS.
 *   last_sleep    previous regime we came out of:
 *                 deep_sleep      — woke from ESP deep sleep
 *                 usb_restart     — esp_restart from USB_AWAKE (button)
 *                 battery_restart — esp_restart from BATTERY_IDLE (button)
 *                 none            — fresh boot after POR / flash
 *   rssi          WiFi signal strength of the current AP, dBm
 *   heap_kb       free heap in KiB
 *   spurious      consecutive spurious-reset short-paths taken (safety valve)
 *   cfg_ver       NVS config schema version
 *   clk_now       firmware wall-clock, Unix epoch seconds (0 if unset)
 *   next_ep       scheduled next-refresh, Unix epoch seconds (0 if unscheduled)
 *   sleep_err_s   actual_slept - expected_slept from the last wake (null if
 *                 we have no prior sleep interval to compare against)
 *   wifi_cached   true iff the most recent WiFi connect succeeded via the
 *                 fast-reconnect cache (BSSID + channel) without a full scan */
static void build_frame_state_json(char *buf, size_t buflen,
                                   const char *wake_label,
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

    const char *usb = (gpio_get_level(PIN_USB_DETECT) == 0) ? "host" : "none";

    /* Firmware's current wall-clock time from its system clock. Set via
     * settimeofday() from each X-Server-Time-Epoch response; survives
     * deep sleep + esp_restart (RTC-backed). 0 = never set. */
    time_t clk_now_t = time(NULL);
    int64_t clk_now = (clk_now_t < 1577836800) ? 0 : (int64_t)clk_now_t;

    const char *last_sleep_str =
        (last_sleep_mode == LAST_SLEEP_MODE_DEEP_SLEEP)      ? "deep_sleep" :
        (last_sleep_mode == LAST_SLEEP_MODE_USB_RESTART)     ? "usb_restart" :
        (last_sleep_mode == LAST_SLEEP_MODE_BATTERY_RESTART) ? "battery_restart" :
        "none";

    int64_t uptime_s = (esp_timer_get_time() - boot_time_us) / 1000000LL;

    /* sleep_err_s: always emitted, as either an int or JSON null. */
    char sleep_err_buf[16];
    if (last_sleep_err_known) {
        snprintf(sleep_err_buf, sizeof(sleep_err_buf), "%d", (int)last_sleep_err_s);
    } else {
        strcpy(sleep_err_buf, "null");
    }

    snprintf(buf, buflen,
        "{\"fw\":\"%s\",\"boot\":%u,\"wake\":\"%s\",\"regime\":\"%s\","
        "\"uptime_s\":%lld,\"bat_mv\":%d,\"usb\":\"%s\","
        "\"last_sleep\":\"%s\",\"rssi\":%d,\"heap_kb\":%u,"
        "\"spurious\":%u,\"cfg_ver\":%u,\"clk_now\":%lld,"
        "\"next_ep\":%lld,\"sleep_err_s\":%s,\"wifi_cached\":%s}",
        fw, (unsigned)boot_count, wake_label, current_regime,
        (long long)uptime_s, (int)last_battery_mv, usb,
        last_sleep_str, rssi, (unsigned)(free_heap / 1024u),
        (unsigned)consecutive_spurious_resets,
        (unsigned)config.cfg_ver, (long long)clk_now,
        (long long)next_refresh_epoch,
        sleep_err_buf,
        last_wifi_used_cache ? "true" : "false");
}

/* Download image and extract X-Sleep-Seconds + X-Server-Time-Epoch headers.
 * Returns image buffer (caller frees) or NULL on failure.
 * *out_sleep_seconds and *out_server_epoch are set if their headers are
 * present, otherwise unchanged. Either pointer may be NULL.
 * wake_label feeds into the X-Frame-State JSON (regime is read from
 * current_regime global; see header comment on build_frame_state_json). */
static uint8_t *download_image(int32_t *out_sleep_seconds, int64_t *out_server_epoch,
                               const char *wake_label,
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

    /* Full device state in a single compact JSON header. The server
     * stores the whole dict per screen for the dashboard Details view. */
    char frame_state[384];
    build_frame_state_json(frame_state, sizeof(frame_state),
                           wake_label ? wake_label : "unknown",
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

    /* 50 samples with short delays for good averaging. Initialise `raw`
     * and check the read result — a failed read with uninitialised `raw`
     * is UB and would contaminate the average with stack garbage. */
    int raw_sum = 0;
    int good_reads = 0;
    for (int i = 0; i < 50; i++) {
        int raw = 0;
        if (adc_oneshot_read(handle, ADC_CHANNEL_4, &raw) == ESP_OK) {
            raw_sum += raw;
            good_reads++;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    if (good_reads == 0) {
        ESP_LOGE("BATT", "all 50 ADC reads failed");
        if (cali) adc_cali_delete_scheme_curve_fitting(cali);
        adc_oneshot_del_unit(handle);
        return 0;
    }
    int raw_avg = raw_sum / good_reads;

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

static void chg_monitor_task(void *arg)
{
    bool led_on = false;
    while (1) {
        /* GPIO 14 LOW = USB host enumerated → blink LED as "charging"
         * indicator. On wall-charger-only (no USB data signaling) GPIO 14
         * reads HIGH, so LED stays off — same as running on battery. We
         * can't distinguish "fully-charged-on-USB" from "on-battery" with
         * this signal alone, so they share behaviour. Spec-acceptable. */
        int charging = (gpio_get_level(PIN_USB_DETECT) == 0);
        if (charging) {
            led_on = !led_on;
            gpio_set_level(PIN_WORK_LED, led_on ? 1 : 0);
        } else {
            gpio_set_level(PIN_WORK_LED, 0);
            led_on = false;
        }
        vTaskDelay(pdMS_TO_TICKS(500));  /* 1 Hz blink */
    }
}

static void chg_monitor_start(void)
{
    if (!chg_monitor_task_handle) {
        xTaskCreate(chg_monitor_task, "chg_mon", 2048, NULL, 1, &chg_monitor_task_handle);
    }
}

/* Called before we touch PIN_WORK_LED in a teardown path (enter_deep_sleep,
 * button_triggered_restart) so the monitor task can't race with us —
 * without this, the task can re-assert the LED after our final "off"
 * and leave it lit until the chip actually powers down. */
static void chg_monitor_stop(void)
{
    if (chg_monitor_task_handle) {
        vTaskDelete(chg_monitor_task_handle);
        chg_monitor_task_handle = NULL;
    }
}

/* ═══════════════════════════════════════════════════════════════════
 *  Power-state detection
 * ═══════════════════════════════════════════════════════════════════ */

/* USB host detection — GPIO 14 LOW means a computer USB host is enumerated.
 *
 * NOT a pure VBUS-detect. Wall chargers / USB battery banks that provide
 * VBUS without USB data signaling leave GPIO 14 HIGH. See HARDWARE_FACTS.md
 * "USB Detection" for the empirical probe results.
 *
 * Single-shot read. Use during boot classification where we want the
 * instantaneous state; for regime-loop polling use usb_host_present_stable()
 * which debounces against cable-wiggle / RF-interference glitches. */
static bool usb_host_present(void)
{
    return gpio_get_level(PIN_USB_DETECT) == 0;
}

/* Debounced USB detection. Returns the last STABLE state of GPIO 14,
 * flipped only after DEBOUNCE consecutive opposite reads. Called from
 * regime-loop polling (every POLL_INTERVAL_MS), so DEBOUNCE = 3 gives
 * ~300 ms of hysteresis — fast enough that a real plug/unplug is still
 * noticed promptly, slow enough that a single glitchy sample doesn't
 * bounce us between regimes. */
#define USB_DEBOUNCE_SAMPLES  3
static bool usb_host_present_stable(void)
{
    static int stable_level = 1;  /* start assuming no host (HIGH) */
    static int opposite_streak = 0;
    int cur = gpio_get_level(PIN_USB_DETECT);
    if (cur != stable_level) {
        if (++opposite_streak >= USB_DEBOUNCE_SAMPLES) {
            stable_level = cur;
            opposite_streak = 0;
        }
    } else {
        opposite_streak = 0;
    }
    return stable_level == 0;
}

/* Debounced button-1 read. Returns true only after N consecutive LOW
 * samples at POLL_INTERVAL_MS. Internal state is static — one edge
 * per poll loop call.
 *
 * Why debounce in software despite the hardware being clean: GPIO 12
 * (PWR_BUTTON) transitions in lockstep with GPIO 14 (USB_DETECT) on
 * USB-plug events. We don't use GPIO 12 as a button here, but GPIO 1
 * can also have brief mechanical bounce. 2 samples × 100 ms = 200 ms
 * debounce, well within spec. */
static bool button1_pressed_debounced(void)
{
    static int low_count = 0;
    static bool reported = false;

    int lvl = gpio_get_level(PIN_BUTTON_1);
    if (lvl == 0) {
        if (low_count < 255) low_count++;
        if (low_count >= BUTTON_DEBOUNCE_SAMPLES && !reported) {
            reported = true;
            return true;
        }
    } else {
        low_count = 0;
        reported = false;
    }
    return false;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Schedule helpers (server-epoch anchored)
 * ═══════════════════════════════════════════════════════════════════ */

/* Current time from the firmware's system clock. Returns 0 if the clock
 * has never been set (pre-2020 epoch). */
static time_t now_epoch(void)
{
    time_t t = time(NULL);
    return (t < 1577836800) ? 0 : t;
}

/* True if the scheduled next-refresh moment has passed (or isn't set).
 * "Not set" counts as due because the only safe behavior when we don't
 * know when to refresh next is to refresh now — server tells us when
 * next on each response. */
static bool refresh_due(void)
{
    time_t now = now_epoch();
    if (now == 0) return true;            /* no clock — fetch to sync */
    if (next_refresh_epoch == 0) return true;
    return now >= next_refresh_epoch;
}

/* Reschedule the next refresh N seconds from now. Used on refresh-failure
 * paths (WiFi down, server unreachable, server returned nonsense
 * sleep_seconds) to keep the regime loops from hot-retrying at 100 ms.
 * Logs the delay so the reason is clear in the serial output. */
static void schedule_retry_in(int seconds, const char *reason)
{
    time_t now = now_epoch();
    if (now > 0) {
        next_refresh_epoch = (int64_t)now + seconds;
    } else {
        /* No clock yet — we can't anchor to an absolute epoch. Leave
         * next_refresh_epoch as-is; refresh_due() will return true
         * immediately because now==0, but the regime loops will just
         * try again next poll tick. In USB this is fine (we want
         * tight retries until the clock syncs); in battery we'll
         * already have dropped into deep sleep before hitting this. */
        next_refresh_epoch = 0;
    }
    /* No fresh pre_sleep_server_epoch either — clear it so the next
     * boot doesn't log a bogus sleep_err_s. */
    pre_sleep_server_epoch = 0;
    last_sleep_err_known = false;
    ESP_LOGW(TAG, "Refresh retry in %d s (%s)", seconds, reason);
}

/* ═══════════════════════════════════════════════════════════════════
 *  Logging regime control
 * ═══════════════════════════════════════════════════════════════════ */

/* Runtime log gating per the spec: logs on in USB_AWAKE, off on battery.
 * esp_log_level_set at NONE silences ESP_LOGI/W/E/D calls immediately. */
static void log_level_apply(bool usb_awake)
{
    esp_log_level_set("*", usb_awake ? ESP_LOG_INFO : ESP_LOG_NONE);
}

/* ═══════════════════════════════════════════════════════════════════
 *  Deep sleep entry
 * ═══════════════════════════════════════════════════════════════════ */

static void save_pre_sleep_epoch(int64_t server_epoch, int64_t local_time_at_download_us)
{
    if (server_epoch <= 0) {
        pre_sleep_server_epoch = 0;
        last_sleep_err_known = false;
        return;
    }
    int64_t delta_s = (esp_timer_get_time() - local_time_at_download_us) / 1000000LL;
    pre_sleep_server_epoch = server_epoch + delta_s;
}

/* Configure EXT1 wake on GPIO 1 (next-image button) + GPIO 14 (USB plug),
 * arm the timer for the scheduled refresh, then esp_deep_sleep_start().
 *
 * sleep_us of 0 = no timer, button/USB wake only (used when no refresh
 * is scheduled — shouldn't normally happen, fallback only).
 *
 * Does NOT include a USB-polling / esp_restart loop. Per spec, USB
 * presence is handled at wake time via the EXT1 source, not via stay-
 * awake-then-restart shenanigans. */
static void enter_deep_sleep(int64_t sleep_us)
{
    ESP_LOGI(TAG, "Deep sleep for %lld s (next-refresh-epoch=%lld)",
             sleep_us / 1000000LL, (long long)next_refresh_epoch);

    /* Teardown. Stop chg_monitor FIRST so it can't toggle WORK_LED
     * between our off-write and esp_deep_sleep_start. */
    chg_monitor_stop();
    if (spi_handle != NULL) {
        spi_bus_remove_device(spi_handle);
        spi_bus_free(SPI2_HOST);
        spi_handle = NULL;
    }
    gpio_set_level(PIN_WORK_LED, 0);
    gpio_set_level(PIN_WIFI_LED, 0);

    /* Record our path so the next boot's X-Frame-State can report it. */
    last_sleep_mode = LAST_SLEEP_MODE_DEEP_SLEEP;

    /* Timer wake */
    if (sleep_us > 0) {
        esp_sleep_enable_timer_wakeup((uint64_t)sleep_us);
    }

    /* EXT1 wake: GPIO 1 (button) + GPIO 14 (USB plug), both active LOW.
     *
     * GPIO 12 is deliberately excluded: it transitions in lockstep with
     * GPIO 14 on USB plug so it would double-fire; and with its unusual
     * stuck-low behaviour on some boots, it can look permanently-pressed.
     * See HARDWARE_FACTS.md "USB Detection" + "GPIO 12". */
    esp_sleep_enable_ext1_wakeup(
        (1ULL << PIN_BUTTON_1) | (1ULL << PIN_USB_DETECT),
        ESP_EXT1_WAKEUP_ANY_LOW
    );

    /* RTC GPIO setup for clean wake-source state. Pull-ups on both pins
     * so that a released button / absent USB both read HIGH through
     * deep sleep and only a real LOW drive wakes the chip. */
    rtc_gpio_init(PIN_BUTTON_1);
    rtc_gpio_pullup_en(PIN_BUTTON_1);
    rtc_gpio_init(PIN_USB_DETECT);
    rtc_gpio_pullup_en(PIN_USB_DETECT);

    /* Isolate unused pins to minimise quiescent current. */
    const int isolate_pins[] = {
        PIN_EPAPER_CS, PIN_WORK_LED, PIN_EPAPER_PWR_EN,
        PIN_BATT_ADC, PIN_EPAPER_RST, PIN_EPAPER_BUSY,
        PIN_CTRL2, PIN_EPAPER_SCLK, PIN_SYS_POWER, PIN_CTRL1,
    };
    for (size_t i = 0; i < sizeof(isolate_pins)/sizeof(isolate_pins[0]); i++) {
        rtc_gpio_isolate(isolate_pins[i]);
    }

    rtc_magic = RTC_MAGIC;  /* ensure counters survive the wake */
    esp_deep_sleep_start();
}

/* ═══════════════════════════════════════════════════════════════════
 *  Refresh action (fetch + display)
 * ═══════════════════════════════════════════════════════════════════ */

/* Perform one complete refresh cycle: WiFi up → HTTP download → update
 * clock + schedule from response → WiFi down → display image.
 *
 * Returns true on success. On failure, shows an error message on the
 * display (unless WiFi itself couldn't come up, in which case we leave
 * the prior image in place).
 *
 * wake_label and caller are for the X-Frame-State JSON. */
static bool perform_refresh(const char *wake_label, int64_t boot_time_us)
{
    int32_t sleep_seconds = 0;
    int64_t server_epoch = 0;
    int64_t local_time_at_download_us = 0;
    uint8_t *img = NULL;

    if (!wifi_connect()) {
        ESP_LOGE(TAG, "WiFi connect failed — leaving prior image on display");
        schedule_retry_in(REFRESH_RETRY_SECONDS, "wifi_connect failed");
        return false;
    }
    gpio_set_level(PIN_WIFI_LED, 1);

    img = download_image(&sleep_seconds, &server_epoch, wake_label, boot_time_us);
    local_time_at_download_us = esp_timer_get_time();

    wifi_shutdown();
    gpio_set_level(PIN_WIFI_LED, 0);

    /* Update persisted schedule: absolute server epoch at which the next
     * refresh is due. Anchors to server time so display + awake time
     * doesn't drift the wake moment later each cycle.
     *
     * If server_epoch is bad (<=0) or sleep_seconds is nonsense (<=0 —
     * malformed response, misconfigured server), fall through to the
     * retry-in-60s helper below so we don't hot-loop. */
    if (server_epoch > 0 && sleep_seconds > 0) {
        next_refresh_epoch = server_epoch + sleep_seconds;
        last_sleep_seconds = sleep_seconds;
        save_pre_sleep_epoch(server_epoch, local_time_at_download_us);
        ESP_LOGI(TAG, "Next refresh scheduled for epoch %lld (in %d s)",
                 (long long)next_refresh_epoch, (int)sleep_seconds);
    } else if (img) {
        /* Download succeeded but the response was missing / invalid
         * scheduling headers. Display the image we got (below) but
         * don't trust the schedule. */
        schedule_retry_in(REFRESH_RETRY_SECONDS,
                          "server response missing/invalid X-Sleep-Seconds");
    }

    if (!img) {
        char msg[384];
        snprintf(msg, sizeof(msg),
                 "Image download failed.\n"
                 "\n"
                 "Tried to connect to:\n"
                 "%s\n"
                 "\n"
                 "Will retry in %d s.\n"
                 "Press reset to try\n"
                 "again now.",
                 config.image_url, REFRESH_RETRY_SECONDS);
        display_message(msg);
        schedule_retry_in(REFRESH_RETRY_SECONDS, "download failed");
        return false;
    }

    ESP_LOGI(TAG, "Displaying image...");
    split_and_display(img);
    heap_caps_free(img);
    ESP_LOGI(TAG, "Image displayed.");

    /* Warn (but don't auto-restart) on stuck display. Per the new design,
     * recovery from wedged controllers is a user action (button press
     * triggers the fresh-restart path). */
    if (gpio_get_level(PIN_EPAPER_BUSY) == 0) {
        ESP_LOGE(TAG, "Post-refresh: BUSY still LOW — display may be wedged. "
                      "Press button or power-cycle to recover.");
    }

    return true;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Regime: USB_AWAKE
 * ═══════════════════════════════════════════════════════════════════ */

static void regime_usb_awake(int64_t boot_time_us);
static void regime_battery_idle(int64_t boot_time_us);

/* Signal a button-press refresh by setting the RTC action flag and
 * restarting. Boot path will detect the flag, clear it early, refresh,
 * and continue into whichever regime matches current usb_host state.
 * See firmware.md "Button = full reboot".
 *
 * `sleep_mode` tells the next boot's X-Frame-State which regime we left:
 * LAST_SLEEP_MODE_USB_RESTART when pressed in USB_AWAKE,
 * LAST_SLEEP_MODE_BATTERY_RESTART when pressed during the BATTERY_IDLE
 * window. Caller-provides rather than introspecting current_regime so
 * it's impossible to forget to keep them in sync. */
static void button_triggered_restart(uint8_t sleep_mode)
{
    ESP_LOGI(TAG, "Button pressed — restarting for fresh-state refresh");
    pending_action = ACTION_REFRESH_FROM_BUTTON;
    rtc_magic = RTC_MAGIC;
    last_sleep_mode = sleep_mode;
    /* Stop chg_monitor before touching the LED so the monitor task
     * can't re-assert WORK_LED between our off-write and esp_restart. */
    chg_monitor_stop();
    gpio_set_level(PIN_WORK_LED, 0);
    vTaskDelay(pdMS_TO_TICKS(50));  /* let log flush */
    esp_restart();
}

static void regime_usb_awake(int64_t boot_time_us)
{
    current_regime = "usb_awake";
    log_level_apply(true);
    ESP_LOGI(TAG, "Entering USB_AWAKE regime");

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));

        if (!usb_host_present_stable()) {
            ESP_LOGI(TAG, "USB host gone — transitioning to BATTERY_IDLE");
            regime_battery_idle(boot_time_us);
            /* regime_battery_idle terminates in deep sleep; never returns */
            return;
        }

        if (button1_pressed_debounced()) {
            button_triggered_restart(LAST_SLEEP_MODE_USB_RESTART);
            return;
        }

        if (refresh_due()) {
            perform_refresh("usb_sched", boot_time_us);
            /* next_refresh_epoch now set from server response; loop continues */
        }
    }
}

/* ═══════════════════════════════════════════════════════════════════
 *  Regime: BATTERY_IDLE (brief window) → DEEP_SLEEP
 * ═══════════════════════════════════════════════════════════════════ */

static void regime_battery_idle(int64_t boot_time_us)
{
    current_regime = "battery_idle";
    /* Keep logs on for the window — this is the reflash-reachable
     * moment, so visibility helps if someone plugs USB in right at
     * the tail of a refresh cycle. Logs go off once we commit to
     * deep sleep (via the implicit ESP-IDF chip-off). */
    log_level_apply(true);
    ESP_LOGI(TAG, "Entering BATTERY_IDLE regime (%d s awake window)",
             (int)(BATTERY_AWAKE_WINDOW_US / 1000000LL));

    int64_t deadline_us = esp_timer_get_time() + BATTERY_AWAKE_WINDOW_US;

    while (esp_timer_get_time() < deadline_us) {
        vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));

        if (usb_host_present_stable()) {
            ESP_LOGI(TAG, "USB plugged during battery window — switching to USB_AWAKE");
            regime_usb_awake(boot_time_us);
            return;
        }

        if (button1_pressed_debounced()) {
            button_triggered_restart(LAST_SLEEP_MODE_BATTERY_RESTART);
            return;
        }
    }

    /* Window expired — commit to deep sleep until the next scheduled
     * refresh (or the user plugs USB / presses a button, which wakes
     * us via EXT1).
     *
     * perform_refresh always sets next_refresh_epoch to a future value
     * on this boot (either from the server's schedule or via
     * schedule_retry_in on failure). So the common case is a future
     * next_refresh_epoch; the SLEEP_FALLBACK_3H_US branch only fires
     * when we have no clock at all (never successfully synced from
     * server). There is no "past-due + future-sleep" branch because
     * it's unreachable under the retry-helper invariant. */
    time_t now = now_epoch();
    int64_t sleep_us;
    if (next_refresh_epoch > 0 && now > 0 && next_refresh_epoch > now) {
        sleep_us = (int64_t)(next_refresh_epoch - now) * 1000000LL;
    } else {
        /* No valid / future schedule: wake in 3 h and retry. */
        sleep_us = SLEEP_FALLBACK_3H_US;
    }
    enter_deep_sleep(sleep_us);
    /* Never returns */
}

/* ═══════════════════════════════════════════════════════════════════
 *  Wake-cause classification & initial action dispatch
 * ═══════════════════════════════════════════════════════════════════ */

typedef enum {
    WAKE_FIRST_BOOT,      /* POR / flash / rtc_magic stale — seed image */
    WAKE_PENDING_ACTION,  /* RTC flag requests refresh (button-restart) */
    WAKE_TIMER,           /* scheduled refresh due */
    WAKE_BUTTON,          /* EXT1, GPIO 1 LOW — refresh */
    WAKE_USB_PLUG,        /* EXT1, GPIO 14 LOW — NO refresh, enter USB regime */
    WAKE_SPURIOUS,        /* unexplained restart — don't refresh, back to sleep */
} wake_cause_t;

/* Decide what triggered this boot. Order matters: pending_action wins
 * over every hardware cause so the button-restart path is reliable. */
static wake_cause_t classify_wake(void)
{
    if (rtc_magic != RTC_MAGIC) return WAKE_FIRST_BOOT;

    if (pending_action == ACTION_REFRESH_FROM_BUTTON) return WAKE_PENDING_ACTION;

    esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();

    if (cause == ESP_SLEEP_WAKEUP_TIMER) return WAKE_TIMER;

    if (cause == ESP_SLEEP_WAKEUP_EXT1) {
        /* Inspect the wake pin status to decide which source fired.
         * If both are LOW (USB plug while button pressed, or the
         * GPIO 12-racing-GPIO 14 artefact), prefer USB_PLUG — it
         * matches the hardware quirk where USB plug drives GPIO 12
         * LOW anyway. */
        uint64_t pins = esp_sleep_get_ext1_wakeup_status();
        if (pins & (1ULL << PIN_USB_DETECT)) return WAKE_USB_PLUG;
        if (pins & (1ULL << PIN_BUTTON_1))   return WAKE_BUTTON;
        return WAKE_SPURIOUS;
    }

    /* UNDEFINED wakeup + last_sleep was DEEP_SLEEP = spurious reset
     * (USB host disconnect, brownout, or silicon quirk) */
    if (last_sleep_mode == LAST_SLEEP_MODE_DEEP_SLEEP) return WAKE_SPURIOUS;

    /* Anything else (including esp_restart not via pending_action) is
     * treated like a first boot for the purposes of "what do we do?" —
     * err toward refresh to resync state. */
    return WAKE_FIRST_BOOT;
}

static const char *wake_cause_name(wake_cause_t c)
{
    switch (c) {
        case WAKE_FIRST_BOOT:     return "first_boot";
        case WAKE_PENDING_ACTION: return "button_restart";
        case WAKE_TIMER:          return "timer";
        case WAKE_BUTTON:         return "button_wake";
        case WAKE_USB_PLUG:       return "usb_plug";
        case WAKE_SPURIOUS:       return "spurious";
    }
    return "?";
}

/* ═══════════════════════════════════════════════════════════════════
 *  app_main
 * ═══════════════════════════════════════════════════════════════════ */

void app_main(void)
{
    int64_t boot_time = esp_timer_get_time();

    /* ── Step 1: validate RTC state ─────────────────────────────────
     *
     * rtc_magic lives in RTC_NOINIT memory — garbage on POR, intact
     * across deep sleep + esp_restart. First time we see it mismatch,
     * zero everything including the wallclock offset. */
    if (rtc_magic != RTC_MAGIC) {
        rtc_magic = 0;
        boot_count = 0;
        wifi_channel = 0;
        memset(wifi_bssid, 0, sizeof(wifi_bssid));
        has_wifi_cache = false;
        last_battery_mv = 0;
        last_sleep_seconds = 0;
        next_refresh_epoch = 0;
        pre_sleep_server_epoch = 0;
        last_sleep_err_s = 0;
        last_sleep_err_known = false;
        consecutive_spurious_resets = 0;
        last_sleep_mode = LAST_SLEEP_MODE_NONE;
        pending_action = ACTION_NONE;
        struct timeval tv = {0, 0};
        settimeofday(&tv, NULL);
    }
    rtc_magic = RTC_MAGIC;  /* validate for the rest of this boot chain */

    boot_count++;

    /* ── Step 2: classify wake, capture pending_action EARLY ────────
     *
     * Grab pending_action and clear it NOW, before any refresh attempt.
     * If the refresh crashes / triggers a watchdog reset, the next boot
     * sees pending_action == NONE and we fall through to whatever the
     * hardware cause dictates — no infinite "button press keeps crashing"
     * loop. */
    wake_cause_t wake = classify_wake();
    pending_action = ACTION_NONE;

    /* ── Step 3: NVS + config ───────────────────────────────────────
     *
     * We always load this first. Error-screen paths below rely on it. */
    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    config_load();

    /* ── Step 4: hardware init ──────────────────────────────────────
     *
     * Always. We need the GPIO state for USB-detect even on the
     * spurious-early-wake short-path, and the LED behavior is uniform. */
    hw_gpio_init();

    /* PWR_EN (GPIO 3) HIGH matches the June original firmware. Required
     * for the battery ADC analog front-end. NEVER driven LOW in the
     * June original after this initial HIGH. */
    gpio_set_level(PIN_EPAPER_PWR_EN, 1);
    gpio_set_level(PIN_WORK_LED,     0);  /* chg_monitor takes over */
    gpio_set_level(PIN_WIFI_LED,     0);

    chg_monitor_start();

    /* Apply initial log level based on whether USB is present. This
     * may get flipped later when we enter a regime, but the early-
     * boot messages should respect the mode too. */
    log_level_apply(usb_host_present());

    ESP_LOGI(TAG, "Boot #%" PRIu32 ", wake=%s, usb=%s",
             boot_count,
             wake_cause_name(wake),
             usb_host_present() ? "computer" : "absent");

    vTaskDelay(pdMS_TO_TICKS(500));  /* analog front-end settle */
    spi_init();

    last_battery_mv = read_battery_mv();
    ESP_LOGI(TAG, "Battery: %d mV", last_battery_mv);

    /* ── Step 5: spurious-reset safety valve ────────────────────────
     *
     * If the wake looked unexplained AND there's a next-refresh schedule
     * we haven't reached yet, just go back to sleep — the controller
     * already displays the last image, no need to redo everything. Bounded
     * so persistent spurious wakes can't lock us out of reflashing. */
    if (wake == WAKE_SPURIOUS) {
        if (consecutive_spurious_resets < MAX_SPURIOUS_RESETS) {
            consecutive_spurious_resets++;
            ESP_LOGI(TAG, "Spurious reset (%u/%u) — back to sleep",
                     consecutive_spurious_resets, MAX_SPURIOUS_RESETS);
            time_t now = now_epoch();
            int64_t remaining_us = (next_refresh_epoch > 0 && now > 0 && next_refresh_epoch > now)
                ? (int64_t)(next_refresh_epoch - now) * 1000000LL
                : SLEEP_FALLBACK_3H_US;
            /* No fresh download → clear pre-sleep epoch so next boot
             * doesn't log a bogus sleep_err_s. */
            pre_sleep_server_epoch = 0;
            enter_deep_sleep(remaining_us);
        }
        ESP_LOGW(TAG, "Spurious-reset cap hit — treating as normal boot for reflash reachability");
    }
    /* Reset the counter once we've taken a path out: either this wasn't a
     * spurious wake, or it was but we hit the cap and are breaking the loop
     * to give the user a reflash window. Either way the streak ends here. */
    consecutive_spurious_resets = 0;

    /* ── Step 6: config sanity — show error + enter battery idle ───
     *
     * If config is broken we can't download anything. Paint a helpful
     * message and enter BATTERY_IDLE so the user has a window to reflash. */
    if (config.cfg_ver != CONFIG_VERSION) {
        char msg[256];
        snprintf(msg, sizeof(msg),
                 "Config version\nmismatch.\n\n"
                 "Expected: %d\nFound: %d\n\n"
                 "Run hokku-setup to\nreconfigure.",
                 CONFIG_VERSION, config.cfg_ver);
        display_message(msg);
        regime_battery_idle(boot_time);
        return;
    }
    if (!config_is_valid()) {
        display_message(
            "Hokku installed but\n"
            "cannot read config.\n\n"
            "Connect USB and run\n"
            "hokku-setup to\n"
            "configure."
        );
        regime_battery_idle(boot_time);
        return;
    }

    /* ── Step 7: decide whether this boot should refresh ───────────
     *
     * Per spec: boot alone is NOT a refresh trigger. Only these cases
     * lead to a refresh at boot time:
     *
     *   WAKE_FIRST_BOOT      — seed the image on install / after flash
     *   WAKE_PENDING_ACTION  — user pressed the button while awake
     *   WAKE_BUTTON          — user pressed the button while sleeping
     *   WAKE_TIMER           — scheduled time hit
     *
     * These cases do NOT refresh:
     *
     *   WAKE_USB_PLUG — user just plugged USB. Spec is explicit: move
     *                   into USB regime but don't change the image.
     *   WAKE_SPURIOUS — handled above (early return to sleep). */
    bool do_refresh = (wake == WAKE_FIRST_BOOT)
                   || (wake == WAKE_PENDING_ACTION)
                   || (wake == WAKE_BUTTON)
                   || (wake == WAKE_TIMER);

    if (do_refresh) {
        const char *label =
            (wake == WAKE_FIRST_BOOT)     ? "first_boot" :
            (wake == WAKE_PENDING_ACTION) ? "button_restart" :
            (wake == WAKE_BUTTON)         ? "button_wake" :
                                            "timer";
        /* Report the regime we're ABOUT to enter (based on current USB
         * state) rather than the transient "boot" string. If USB goes
         * away during the refresh, regime_battery_idle will correct it
         * on entry. */
        current_regime = usb_host_present() ? "usb_awake" : "battery_idle";

        /* Snapshot the PREVIOUS sleep's anchor BEFORE perform_refresh
         * overwrites pre_sleep_server_epoch and last_sleep_seconds with
         * THIS boot's response. Without this snapshot the sleep-error
         * check compares "time since this boot's download" against
         * "this boot's requested sleep duration" — two unrelated
         * numbers that always yield a meaningless value near
         * -last_sleep_seconds (observed 2026-04-20: sleep_err_s=-157s
         * on a clean timer wake where the real error was ~3 s). */
        int64_t prior_sleep_entry_epoch = pre_sleep_server_epoch;
        int32_t prior_sleep_duration    = last_sleep_seconds;

        perform_refresh(label, boot_time);

        /* Sleep-error diagnostic: only meaningful on timer wakes where
         * we have BOTH a prior sleep anchor AND a prior duration to
         * compare against. `now` is post-refresh wall-clock, which
         * bakes in ~20 s of this boot's WiFi+download+display time —
         * inherent ±display-granular precision. Good enough to flag
         * "is my frame waking minutes late" vs "seconds late". */
        if (wake == WAKE_TIMER && prior_sleep_entry_epoch > 0 && prior_sleep_duration > 0) {
            time_t now = now_epoch();
            if (now > 0) {
                int64_t actual_slept_s = (int64_t)now - prior_sleep_entry_epoch;
                int64_t err            = actual_slept_s - prior_sleep_duration;
                if (err > INT32_MAX) err = INT32_MAX;
                if (err < INT32_MIN) err = INT32_MIN;
                last_sleep_err_s     = (int32_t)err;
                last_sleep_err_known = true;
            }
        }
    }

    /* ── Step 8: enter the regime that matches current USB state ───
     *
     * Battery or USB, same decision rule: poll usb_host once. The
     * regime functions own further transitions between each other. */
    if (usb_host_present()) {
        regime_usb_awake(boot_time);
    } else {
        regime_battery_idle(boot_time);
    }
    /* Both regimes eventually commit to deep sleep (battery path) or
     * loop forever (USB path). Never returns. */
}
