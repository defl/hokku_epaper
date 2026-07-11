/*
 * Hokku EPaper firmware for Bigme F7 (XR872AT + EK79655 7-color EPD)
 *
 * Flow:
 *   platform_init() auto-connects WiFi from sysinfo flash credentials.
 *   On NETWORK_UP: init EPD, then loop:
 *     GET /hokku/screen/ → stream 192000-byte 4bpp response body to EPD → refresh
 *     sleep X-Sleep-Seconds (from response header)
 *
 * WiFi provisioning via UART console:
 *   net sta config <ssid> <password>
 *   net sta enable
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdarg.h>

#include "kernel/os/os.h"
#include "common/framework/platform_init.h"
#include "common/framework/net_ctrl.h"
#include "net/HTTPClient/HTTPCUsr_api.h"
#include "net/HTTPClient/API/HTTPClient.h"
#include "net/HTTPClient/API/HTTPClientCommon.h"
#include "lwip/netif.h"
#include "lwip/dhcp.h"
#include "lwip/ip_addr.h"

#include "image/image.h"
#include "ota/ota.h"
#include "driver/chip/hal_wdg.h"
#include "driver/chip/hal_adc.h"
#include "driver/chip/hal_wakeup.h"
#include "net/wlan/wlan.h"
#include "common/framework/sysinfo.h"
#include "pm/pm.h"

#include "epd.h"
#include "led.h"
#include "hokku_config.h"

/* Static IP config — used when DHCP is unavailable on the network */
#define STATIC_IP_ADDR   "192.168.6.199"
#define STATIC_GW_ADDR   "192.168.6.254"
#define STATIC_NM_ADDR   "255.255.255.0"

#define HOKKU_SERVER_URL        "http://192.168.6.111:8080/hokku/screen/"
#define SCREEN_NAME             "bigme-f7"
#define SCREEN_MODEL            "bigme_f7"
#define FIRMWARE_VERSION        "1.2.2"

#define EPD_IMAGE_BYTES         192000U  /* 800 x 480 x 4bpp / 8 */
#define DEFAULT_SLEEP_SECONDS   300
#define HTTP_TIMEOUT_S          90       /* covers 192KB DL + EPD streaming time */

#define REFRESH_THREAD_STACK    (8 * 1024)
#define REFRESH_THREAD_PRIO     OS_THREAD_PRIO_APP

/* Config schema version reported to the server (frame-state cfg_ver). */
#define HOKKU_CFG_VER           1
/* Firmware build stamp (X-Firmware-Build). Clean builds keep this fresh. */
#define HOKKU_BUILD_TS          (__DATE__ " " __TIME__)

static OS_Thread_t g_refresh_thread;
static int         g_epd_ready = 0;

/*
 * Serializes the network+flash critical section so an OTA (flash erase/write via a
 * second HTTP session) can never run concurrently with a periodic refresh or a
 * second OTA. The refresh thread holds it around do_refresh(); the console `ota`
 * command TRY-locks it and refuses if the refresh thread is busy. hokku_do_ota()
 * itself does NOT lock — its callers already hold the lock (no recursive lock).
 */
static OS_Mutex_t  g_ota_lock;

/* --------------------------------------------------------------------------
 * Reporting (Phase 1): activity log ring + software wall-clock + frame-state.
 * ------------------------------------------------------------------------ */

/*
 * Activity log ring. Every hlog() line is echoed to the serial console AND
 * appended here; the accumulated buffer is POSTed to the server as the request
 * body on each fetch (mirrors the ESP32 firmware's RTC log ring) and reset only
 * after a 200. Truncating (not circular): once full it stops appending until the
 * next successful upload resets it — bounded and simple.
 */
#define HOKKU_LOG_RING_SZ       2048U
static char     g_log_ring[HOKKU_LOG_RING_SZ];
static uint32_t g_log_len;

static void hlog(const char *fmt, ...)
{
    char    line[160];
    va_list ap;
    int     n;

    va_start(ap, fmt);
    n = vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    if (n < 0)
        return;
    if (n > (int)sizeof(line) - 1)
        n = (int)sizeof(line) - 1;

    printf("%s", line);                       /* still to serial console */
    if (g_log_len + (uint32_t)n < HOKKU_LOG_RING_SZ) {
        memcpy(g_log_ring + g_log_len, line, (size_t)n);
        g_log_len += (uint32_t)n;
    }
}
static void hlog_reset(void) { g_log_len = 0; }

/*
 * Software wall-clock anchored to the server epoch (X-Server-Time-Epoch), so we
 * can report clk_now without an RTC. base==0 means never synced. Good enough for
 * the always-on loop; Phase 3 will back this with the RTC across hibernation.
 */
static uint32_t g_clk_epoch_base;    /* server epoch captured at last sync */
static uint32_t g_clk_uptime_base;   /* OS_GetTime() (secs) at last sync */

static void hokku_clock_set(uint32_t server_epoch)
{
    g_clk_epoch_base  = server_epoch;
    g_clk_uptime_base = OS_GetTime();
}
static uint32_t hokku_clock_now(void)
{
    if (g_clk_epoch_base == 0)
        return 0;
    return g_clk_epoch_base + (OS_GetTime() - g_clk_uptime_base);
}

/* SDK SRAM heap span (same accessor the `heap` console command uses). */
extern void heap_get_space(uint8_t **start, uint8_t **end, uint8_t **current);

/* Wake reason captured once at boot (frame-state "wake"): "timer" = hibernation wake. */
static const char *g_wake = "first_boot";

static void hokku_capture_wake(void)
{
    uint32_t ev = HAL_Wakeup_GetEvent();
    if (ev & PM_WAKEUP_SRC_WKTIMER)
        g_wake = "timer";
    else if (ev != 0)                          /* 0 == cold power-on */
        g_wake = "wake_io";
}

/*
 * Battery read on ADC channel 4 (pin PA14) — the pack-sense line the OEM firmware
 * uses (NOT ADC_CHANNEL_VBAT, which reads the SoC's regulated internal rail and was
 * the source of the bogus steady ~2.58 V). Returns mV in the Li-ion range, or 0 if
 * unavailable/implausible (build_frame_state then omits it, so the server shows no
 * battery rather than a wrong value). Channel + scaling confirmed by disassembling
 * the OEM firmware. HAL_ADC_Conv_Polling auto-configures the PA14->CH4 pinmux.
 */
static int g_adc_ready = 0;
uint32_t hokku_battery_mv(void)          /* also used by the `cfg show` diagnostics */
{
    uint32_t data = 0, mv;

    if (!g_adc_ready) {
        ADC_InitParam p;
        memset(&p, 0, sizeof(p));
        p.freq  = 1000000;
        p.delay = 10;
        p.mode  = ADC_CONTI_CONV;
#if (__CONFIG_CHIP_ARCH_VER == 2)
        p.vref_mode = ADC_VREF_MODE_1;
#endif
        if (HAL_ADC_Init(&p) != HAL_OK)
            return 0;
        g_adc_ready = 1;
    }
    if (HAL_ADC_Conv_Polling(ADC_CHANNEL_4, &data, 1000) != HAL_OK)   /* PA14 pack sense, NOT VBAT */
        return 0;
    /* OEM battery scaling (reverse-engineered from the OEM boot partition's
     * adc_voltage_get @VMA 0x20122c): a ratio-1 external channel (2500 mV ref,
     * 12-bit) behind a ~4.37:1 divider on PA14, compiled as
     * (raw*295000/1105920)*10 (= raw * 2.6674). No u32 overflow (4095*295000 <
     * 2^32). Clamp to the 4.2 V charge limit exactly like the OEM. */
    mv = (data * 295000U / 1105920U) * 10U;
    if (mv > 4200U)
        mv = 4200U;
    return (mv >= 3000U && mv <= 4200U) ? mv : 0;
}

/* Build the compact X-Frame-State telemetry JSON (server parses ota/bat_mv/clk_now). */
static void build_frame_state(char *buf, size_t sz)
{
    hokku_config_t *cfg = hokku_config_get();
    wlan_sta_ap_t   ap;
    int             rssi = 0;
    uint8_t        *hs, *he, *hc;
    uint32_t        bat = hokku_battery_mv();
    char            batfield[24] = "";

    if (wlan_sta_ap_info(&ap) == 0)
        rssi = (int)(int8_t)ap.rssi;         /* stored as signed dBm in a u8 */

    heap_get_space(&hs, &he, &hc);           /* free ~= end - current watermark */

    if (bat > 0)
        snprintf(batfield, sizeof(batfield), ",\"bat_mv\":%u", (unsigned)bat);

    snprintf(buf, sz,
        "{\"fw\":\"%s\",\"uptime_s\":%u,\"heap_kb\":%u,\"rssi\":%d,"
        "\"regime\":\"%s\",\"wake\":\"%s\",\"cfg_ver\":%u,\"clk_now\":%u,\"ota\":1%s}",
        FIRMWARE_VERSION,
        (unsigned)OS_GetTime(),
        (unsigned)((he - hc) / 1024),
        rssi,
        (cfg->power_mode == HOKKU_PWR_AWAKE || led_usb_present()) ? "usb_awake" : "battery",
        g_wake,
        (unsigned)HOKKU_CFG_VER,
        (unsigned)hokku_clock_now(),
        batfield);
}

/*
 * A/B try-boot rollback state.
 *
 * g_boot_seq is the image sequence the bootloader launched US from (captured in
 * platform_init_level0 before we touch the OTA cfg). g_rollback_armed is set once
 * we have (a) repointed the OTA cfg at the OTHER (known-good) slot and (b) started
 * the watchdog. While armed, any fault/hang before hokku_rollback_commit() lets the
 * watchdog reset the chip; the bootloader then boots the known-good slot.
 *
 * On our target unit the candidate lives in slot 0 and the live OEM
 * firmware in slot 1, so g_boot_seq==0 and the rollback target is seq 1. The logic
 * is unit-agnostic: the good slot is always (g_boot_seq + 1) % IMAGE_SEQ_NUM.
 */
static image_seq_t g_boot_seq;
static int         g_rollback_armed = 0;

/*
 * Phase B0 — brick-safe watchdog-semantics bench test.
 *
 * Set HOKKU_B0_WDGTEST to 1 to build the B0 test firmware (a one-off). It does NOT
 * arm the A/B rollback (never repoints the OTA cfg), so the device just reboots into
 * itself. Purpose: prove the load-bearing assumption that a HAL_WDG_Init(WDG_EVT_RESET)
 * TIMEOUT (not HAL_WDG_Reboot) yields a full system reset that re-enters the bootloader
 * with CPUA_BOOT_FLAG == COLD_RESET (0). The community SDK source cannot establish this
 * (its ROM WDG_HwInit has no XR872 WDG->CFG path), so we measure it on the real silicon.
 *
 * On cold boot: print the reset registers, arm WDG (RESET, 2 s), then hang (no feed).
 * After the timeout, if the assumption holds we reboot through the bootloader and land
 * here again with a watchdog reset-source bit set and boot flag 0 -> print CONFIRMED and
 * halt. If it drops to BROM instead, the assumption is false (caught safely, USB-recoverable).
 */
#define HOKKU_B0_WDGTEST 0

/* Reset-cause registers, captured raw in level0 (earliest app code) to avoid any
 * later SDK clear. Addresses from hal_prcm.h / hal_wdg.h (arch v2). */
#define PRCM_CPUA_BOOT_FLAG_REG   (*(volatile uint32_t *)0x40040100U)
#define PRCM_CPUA_BOOT_ARG_REG    (*(volatile uint32_t *)0x40040108U)
#define PRCM_CPU_RESET_SOURCE_REG (*(volatile uint32_t *)0x40040218U)
#define WDG_CFG_REG               (*(volatile uint32_t *)0x40040954U)  /* TIMER_BASE+0xA0+0xB4 */
#define RST_SRC_PWRON_BIT         (1U << 0)
#define RST_SRC_WDG_ALL_BIT       (1U << 8)
#define RST_SRC_WDG_CPU_MASK      (0x3U << 9)

#if HOKKU_B0_WDGTEST
static uint32_t g_b0_rst_src, g_b0_boot_flag, g_b0_boot_arg, g_b0_wdg_cfg;
#endif

/*
 * Read a numeric response header. The SDK's HTTPClientFindFirstHeader only sets
 * the search clue; HTTPClientGetNextHeader actually returns the matched text
 * (which may include the "Name:" prefix, so we skip past any ':'). Returns 1 and
 * writes *out on success. (The prior code called only FindFirstHeader and never
 * read the value, so X-Sleep-Seconds silently fell back to the default.)
 */
static int read_resp_header_uint(HTTP_SESSION_HANDLE h, const char *name, uint32_t *out)
{
    char   v[48];
    UINT32 len = sizeof(v);
    int    ok = 0;

    HTTPClientFindFirstHeader(h, (char *)name, v, &len);
    len = sizeof(v);
    if (HTTPClientGetNextHeader(h, v, &len) == HTTP_CLIENT_SUCCESS) {
        char *p = strchr(v, ':');
        p = p ? p + 1 : v;
        while (*p == ' ' || *p == '\t')
            p++;
        *out = (uint32_t)strtoul(p, NULL, 10);
        ok = 1;
    }
    HTTPClientFindCloseHeader(h);
    return ok;
}

/* Read a string response header into out[]. Returns 1 if a non-empty value was found. */
static int read_resp_header_str(HTTP_SESSION_HANDLE h, const char *name, char *out, size_t outsz)
{
    char   v[64];
    UINT32 len = sizeof(v);
    int    ok = 0;

    out[0] = '\0';
    HTTPClientFindFirstHeader(h, (char *)name, v, &len);
    len = sizeof(v);
    if (HTTPClientGetNextHeader(h, v, &len) == HTTP_CLIENT_SUCCESS) {
        char *p = strchr(v, ':');
        p = p ? p + 1 : v;
        while (*p == ' ' || *p == '\t')
            p++;
        strncpy(out, p, outsz - 1);
        out[outsz - 1] = '\0';
        for (int i = (int)strlen(out) - 1; i >= 0 &&
             (out[i] == '\r' || out[i] == '\n' || out[i] == ' '); i--)
            out[i] = '\0';
        ok = (out[0] != '\0');
    }
    HTTPClientFindCloseHeader(h);
    return ok;
}

/*
 * Derive the firmware.bin URL from the configured screen server_url, e.g.
 *   "http://host:port/hokku/screen/" -> "http://host:port/hokku/firmware.bin?model=bigme_f7"
 * by keeping everything up to and including "/hokku/" and appending the OTA path.
 */
static void hokku_build_firmware_url(char *out, size_t outsz)
{
    const char *base = hokku_config_get()->server_url;
    const char *hk   = strstr(base, "/hokku/");

    if (hk) {
        size_t prefix = (size_t)(hk - base) + 7;    /* through "/hokku/" */
        if (prefix < outsz) {
            memcpy(out, base, prefix);
            out[prefix] = '\0';
            strncat(out, "firmware.bin?model=" SCREEN_MODEL, outsz - strlen(out) - 1);
            return;
        }
    }
    snprintf(out, outsz, "%s", base);               /* fallback: unrecognised URL shape */
}

/*
 * Run an A/B OTA update: stream the server's firmware image into the INACTIVE
 * slot, flip the boot cfg to it, and reboot into it. On success this does NOT
 * return — it reboots, and platform_init_level0 re-arms the rollback watchdog so
 * a bad new image auto-reverts to the slot we are running now. On ANY failure it
 * returns with the current image still active and the boot cfg unchanged (the
 * half-written slot is never booted).
 *
 * Safe because: (1) the rollback WDG was already stopped at boot-commit, so the
 * multi-second download won't trip it; (2) ota_get_image validates the written
 * section chain before we flip anything; (3) the cfg flip is the last step.
 */
static void hokku_do_ota(const char *server_ver)
{
    char url[192];
    const image_ota_param_t *iop = image_get_ota_param();
    image_seq_t upd = (image_seq_t)((image_get_running_seq() + 1) % IMAGE_SEQ_NUM);
    uint32_t wr_start = iop ? iop->addr[upd] : 0;
    uint32_t wr_end   = iop ? wr_start + IMAGE_AREA_SIZE(iop->img_max_size) : 0;

    /* Safety guard: the SDK OTA erases [addr[upd], addr[upd]+img_max_size) BEFORE
     * a single byte is downloaded. The update target ALTERNATES by A/B policy:
     * running seq0 -> writes slot1 (0x181000); running seq1 -> writes slot0
     * (bl_size, 0x8000). BOTH are valid — the guard must allow either. It only
     * rejects an address BELOW the bootloader (would erase the bootloader) or one
     * whose end runs into the sysinfo/config partition at 0x300000 — e.g. a
     * mis-provisioned header (ota_addr=0xFFFFFFFF) that wraps addr[1] to ~0x7FFF.
     * Cheap insurance; a no-op on the confirmed unit in either direction. */
    if (!iop || wr_start < iop->bl_size || wr_end > 0x300000U || wr_end <= wr_start) {
        hlog("hokku: OTA refused — update slot 0x%x..0x%x outside safe window\n",
             (unsigned)wr_start, (unsigned)wr_end);
        return;
    }

    hokku_build_firmware_url(url, sizeof(url));
    hlog("hokku: OTA start (server ver '%s') <- %s\n", server_ver, url);

    if (ota_init() != OTA_STATUS_OK) {
        hlog("hokku: OTA ota_init failed\n");
        return;
    }
    if (ota_get_image(OTA_PROTOCOL_HTTP, url) != OTA_STATUS_OK) {
        hlog("hokku: OTA download/write failed (slot unchanged)\n");
        return;
    }
    /* No verify trailer in our image (built without mkimage -O); the per-section
     * checksum walk inside ota_get_image already ran. VERIFY_NONE still commits
     * the boot cfg to the freshly written slot. */
    if (ota_verify_image(OTA_VERIFY_NONE, NULL) != OTA_STATUS_OK) {
        hlog("hokku: OTA verify/commit failed (slot unchanged)\n");
        return;
    }
    hlog("hokku: OTA OK — rebooting into new image\n");
    OS_MSleep(200);                                 /* flush log over UART first */
    ota_reboot();                                   /* HAL_WDG_Reboot(); no return */
    hlog("hokku: OTA reboot returned?!\n");         /* unreached */
}

/* Console-triggered OTA (`ota` command) — updates from the configured server.
 * TRY-locks the OTA/flash mutex so it refuses (rather than racing) if the refresh
 * thread is mid-cycle. On OTA success hokku_do_ota reboots and never returns. */
void hokku_ota_manual(void)
{
    if (!OS_MutexIsValid(&g_ota_lock) || OS_MutexLock(&g_ota_lock, 0) != OS_OK) {
        hlog("hokku: OTA busy (refresh in progress) — retry in a few seconds\n");
        return;
    }
    hlog("hokku: manual OTA requested from console\n");
    hokku_do_ota("manual");                    /* reboots on success */
    OS_MutexUnlock(&g_ota_lock);               /* only reached if OTA failed */
}

/*
 * Fetch one image from the server and stream it byte-by-byte to the EPD.
 * Returns the number of seconds to sleep before the next refresh, or -1 on
 * any error (caller should retry after a short backoff).
 */
static int do_refresh(void)
{
    hokku_config_t *cfg = hokku_config_get();
    HTTPParameters  params;
    HTTP_CLIENT     info;
    char            buf[512];
    char            frame_state[384];
    UINT32          bytes_streamed = 0;
    UINT32          log_sent;
    int             sleep_sec = (int)cfg->default_sleep_s;
    int             ret;

    build_frame_state(frame_state, sizeof(frame_state));

    log_sent = g_log_len;                     /* bytes delivered by this POST */
    memset(&params, 0, sizeof(params));
    strncpy(params.Uri, cfg->server_url, sizeof(params.Uri) - 1);
    params.HttpVerb  = VerbPost;              /* POST so the log ring rides as the body */
    params.nTimeout  = HTTP_TIMEOUT_S;
    params.pData     = g_log_ring;            /* request body = accumulated activity log */
    params.pLength   = log_sent;              /* snapshot; dropped only after a 200 */

    hlog("hokku: POST %s (log %u B)\n", cfg->server_url, (unsigned)g_log_len);
    ret = HTTPC_open(&params);
    if (ret != HTTP_CLIENT_SUCCESS) {
        hlog("hokku: HTTP open failed (%d)\n", ret);
        return -1;
    }

    /* Request headers — server uses these for telemetry / OTA checks */
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Screen-Name",      cfg->screen_name, 1);
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Screen-Model",     SCREEN_MODEL,     1);
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Firmware-Version", FIRMWARE_VERSION, 1);
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Firmware-Build",   HOKKU_BUILD_TS,   1);
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Frame-State",      frame_state,      1);
    HTTPClientAddRequestHeaders(params.pHTTP, "Content-Type",       "text/plain",     1);

    ret = HTTPC_request(&params, NULL);
    if (ret != HTTP_CLIENT_SUCCESS) {
        hlog("hokku: HTTP request failed (%d)\n", ret);
        HTTPC_close(&params);
        return -1;
    }

    /* Check HTTP status code */
    if (HTTPC_get_request_info(&params, &info) != HTTP_CLIENT_SUCCESS) {
        HTTPC_close(&params);
        return -1;
    }
    if (info.HTTPStatusCode != 200) {
        hlog("hokku: server returned %u\n", (unsigned)info.HTTPStatusCode);
        HTTPC_close(&params);
        /* For 503/404 (no image ready), use a short retry */
        return (info.HTTPStatusCode == 503 || info.HTTPStatusCode == 404) ? 30 : -1;
    }

    /* The POST body (log) reached the server in a 200 — drop the delivered prefix,
     * keeping anything hlog() appended during this exchange for the next upload. */
    if (g_log_len >= log_sent) {
        g_log_len -= log_sent;
        memmove(g_log_ring, g_log_ring + log_sent, g_log_len);
    } else {
        hlog_reset();
    }

    /* Capture sleep + server clock + any OTA signal from response headers */
    char fw_update[32] = "";
    {
        uint32_t v;
        if (read_resp_header_uint(params.pHTTP, "X-Sleep-Seconds", &v) && v > 0)
            sleep_sec = (int)v;
        if (read_resp_header_uint(params.pHTTP, "X-Server-Time-Epoch", &v) && v > 1600000000U)
            hokku_clock_set(v);              /* sanity: after 2020-09-13 */
        read_resp_header_str(params.pHTTP, "X-Firmware-Update", fw_update, sizeof(fw_update));
    }

    /* OTA takes priority over display: the server told us to update. Discard the
     * image body, close the socket, and run the A/B update (reboots on success;
     * returns only on failure, in which case we keep the current image). */
    if (fw_update[0]) {
        hlog("hokku: firmware update signalled -> %s\n", fw_update);
        HTTPC_close(&params);
        hokku_do_ota(fw_update);
        return -1;                           /* OTA failed: short backoff, unchanged */
    }

    /* Stream response body to EPD (CMD 0x10 was not sent yet — do it now) */
    epd_send_cmd(0x10);  /* DTM: data start transmission */

    while (bytes_streamed < EPD_IMAGE_BYTES) {
        UINT32 want = EPD_IMAGE_BYTES - bytes_streamed;
        UINT32 to_read = want < sizeof(buf) ? want : (UINT32)sizeof(buf);
        UINT32 n = 0;

        ret = HTTPC_read(&params, buf, to_read, &n);
        if (n > 0) {
            UINT32 usable = n;
            if (bytes_streamed + usable > EPD_IMAGE_BYTES)
                usable = EPD_IMAGE_BYTES - bytes_streamed;
            for (uint32_t i = 0; i < usable; i++)
                epd_send_data((uint8_t)buf[i]);
            bytes_streamed += n;  /* count all received, stream only usable */
        }
        if (ret != HTTP_CLIENT_SUCCESS)
            break;
    }

    HTTPC_close(&params);

    if (bytes_streamed < EPD_IMAGE_BYTES) {
        hlog("hokku: short image: %u / %u bytes\n",
               (unsigned)bytes_streamed, (unsigned)EPD_IMAGE_BYTES);
        return -1;
    }

    hlog("hokku: image received, refreshing display...\n");
    epd_refresh();  /* ~30 s */
    hlog("hokku: refresh done, sleeping %d s\n", sleep_sec);

    return sleep_sec;
}

/*
 * Deep sleep until the next refresh, then WAKE = full restart (hibernation). The
 * device re-runs the whole boot flow (rollback-commit, config, WiFi, fetch) on
 * wake, so no state needs retaining across sleep beyond the flash config. Does not
 * return; if pm_enter_mode somehow fails, fall back to a clean reboot.
 */
static void hokku_hibernate(uint32_t sleep_s)
{
    if (sleep_s < 5)
        sleep_s = 5;
    if (sleep_s > 60000)                       /* HAL wake timer tops out ~18.6 h */
        sleep_s = 60000;
    hlog("hokku: battery mode — hibernating %u s (wake -> full reboot)\n", (unsigned)sleep_s);
    OS_MSleep(60);                             /* let the log line flush over UART */
    HAL_Wakeup_SetTimer_Sec(sleep_s);
    pm_enter_mode(PM_MODE_HIBERNATION);
    HAL_WDG_Reboot();                          /* unreached on success */
}

/* True when the device should deep-sleep (vs stay awake) between refreshes. */
static int hokku_should_sleep(void)
{
    switch (hokku_config_get()->power_mode) {
    case HOKKU_PWR_SLEEP: return 1;
    case HOKKU_PWR_AWAKE: return 0;
    default:              return !led_usb_present();   /* AUTO: sleep only on battery */
    }
}

static void refresh_thread_fn(void *arg)
{
    (void)arg;
    hlog("hokku: refresh thread started (wake=%s)\n", g_wake);

    if (!g_epd_ready) {
        hlog("hokku: EPD init\n");
        epd_init();
        g_epd_ready = 1;
    }

    while (1) {
        /* Hold the OTA/flash lock across the whole refresh (which may itself run
         * an OTA on X-Firmware-Update) so a console `ota` can't run concurrently. */
        OS_MutexLock(&g_ota_lock, OS_WAIT_FOREVER);
        int sleep_sec = do_refresh();          /* may reboot via OTA and never return */
        OS_MutexUnlock(&g_ota_lock);
        if (sleep_sec < 0)
            sleep_sec = 30;                    /* error backoff */

        if (hokku_should_sleep())
            hokku_hibernate((uint32_t)sleep_sec);   /* battery: deep sleep, restarts on wake */
        else
            OS_MSleep((uint32_t)sleep_sec * 1000);  /* USB/awake: keep looping */
    }
}

static void net_cb(uint32_t event, uint32_t data, void *arg)
{
    uint16_t type = EVENT_SUBTYPE(event);

    hlog("hokku: net event 0x%04x data=0x%08x\n", (unsigned)type, (unsigned)data);

    switch (type) {
    case NET_CTRL_MSG_WLAN_CONNECTED: {
        hokku_config_t *cfg = hokku_config_get();
        struct netif   *nif = netif_list;
        ip_addr_t       ip, gw, nm;

        if (!nif) {
            hlog("hokku: WLAN connected but no netif yet\n");
            break;
        }
        if (cfg->use_dhcp) {
            /* Leave the SDK's DHCP client running; it fires NETWORK_UP on lease. */
            hlog("hokku: WLAN connected, using DHCP\n");
            break;
        }
        /* Static IP from config. The SDK has already brought the netif up (it
         * started DHCP on link-up), so a bare netif_set_up() is a no-op and fires
         * no callback. lwIP 2.x only fires the status callback (which the SDK maps
         * to NETWORK_UP) when the address actually changes via netif_set_addr() —
         * direct nif->ip_addr = ... does NOT trigger it. ip_2_ip4() pulls the v4
         * address out of the dual-stack ip_addr_t union. */
        if (!ipaddr_aton(cfg->ip, &ip) || !ipaddr_aton(cfg->gw, &gw) ||
            !ipaddr_aton(cfg->nm, &nm)) {
            hlog("hokku: bad static IP in config — leaving DHCP to run\n");
            break;
        }
        dhcp_stop(nif);
        netif_set_addr(nif, ip_2_ip4(&ip), ip_2_ip4(&nm), ip_2_ip4(&gw));
        netif_set_up(nif);
        hlog("hokku: static IP set  %s  gw=%s\n", cfg->ip, cfg->gw);
        break;
    }
    case NET_CTRL_MSG_NETWORK_UP: {
        struct netif *nif2 = netif_list;
        if (nif2)
            hlog("hokku: network up  ip=%s\n", ipaddr_ntoa(&nif2->ip_addr));
        if (!OS_ThreadIsValid(&g_refresh_thread)) {
            OS_ThreadCreate(&g_refresh_thread,
                            "hokku_refresh",
                            refresh_thread_fn,
                            NULL,
                            REFRESH_THREAD_PRIO,
                            REFRESH_THREAD_STACK);
        }
        break;
    }
    case NET_CTRL_MSG_NETWORK_DOWN:
        hlog("hokku: network down\n");
        break;
    default:
        break;
    }
}

/*
 * XR872AT XIP bias fix.
 *
 * On this silicon the flash instruction cache maps the XIP VMA window (base
 * 0x400000) to flash via OPI_MEM_CTRL->BIAS_ADDR0 (0x4000B088): the low 28 bits
 * hold the flash byte offset where the XIP image starts, bit31 enables the bias.
 * (The arch-v1 FLASH_CACHE->READ_BIAS_ADDR register at 0x4000C098 does NOT exist
 *  on this part — confirmed: zero references in OEM firmware.)
 *
 * Our app_xip section data sits at flash 0x13040 (header at 0x13000). When the
 * bias is left at 0 the bootloader's identity map sends VMA 0x470534
 * (platform_init_level1) to flash[0x70534] = wrong code -> MemManage fault at
 * PC=0x40000000. The SDK's stock platform_init_level0 is supposed to program the
 * bias via image_get_section_addr()+HAL_Xip_Init(), but on our build it ends up
 * 0 (suspected: section lookup fails before the console UART is up, so its error
 * print is lost). We override the __weak level0 to mirror the stock minimal init
 * (pm_start, flash, image), call HAL_Xip_Init() with the offset hardcoded so it
 * cannot depend on the failing lookup, then force BIAS_ADDR0 as a safety net.
 */
#define OPI_MEM_CTRL_BIAS_ADDR0   (*(volatile uint32_t *)0x4000B088U)
#define XIP_BIAS_ENABLE           0x80000000U

/*
 * Phase B rollback test: set HOKKU_B_BREAK_XIP to 1 to build a DELIBERATELY BROKEN
 * candidate. The XIP bias is pointed at a wrong flash offset (slot-1's app_xip),
 * so the first XIP instruction after platform_init_level0 faults — reproducing the
 * no-boot brick class. The rollback arm in hokku_rollback_arm() runs BEFORE
 * HAL_Xip_Init(), so the candidate still repoints the cfg to the good slot and arms
 * the watchdog before it faults; the watchdog must then roll back to the OEM.
 */
#define HOKKU_B_BREAK_XIP 0

/*
 * app_xip flash offset is PER-SLOT. slot 0's app_xip data sits at 0x13040 (header
 * 0x13000 + 0x40); the two A/B app-chains are one image-area apart, so slot 1's is
 * 0x13040 + XIP_SLOT_STRIDE = 0x18C040. XIP_SLOT_STRIDE is the app-base spacing
 * (slot1 app base 0x181000 - slot0 app base 0x8000). An image running from slot N
 * MUST map its OWN app_xip: a fixed slot-0 offset would send a slot-1 (OTA'd) image
 * to slot 0's code on its first XIP instruction and MemManage-fault. So the offset
 * is derived from the slot we actually booted, not hardcoded.
 */
#define XIP_FLASH_DATA_OFFSET     0x13040U   /* slot 0 app_xip data */
#define XIP_SLOT_STRIDE           0x179000U  /* per-slot app_xip spacing */

extern void pm_start(void);
extern int  HAL_Flash_Init(uint32_t flash);
extern int  image_init(uint32_t flash, uint32_t addr, uint32_t max_size);
extern int  HAL_Xip_Init(uint32_t flash, uint32_t xaddr);
extern void platform_cache_init(void);

/* Flash offset of the app_xip section for the slot we booted from (boot_seq). */
static uint32_t hokku_xip_offset(uint32_t boot_seq)
{
#if HOKKU_B_BREAK_XIP
    (void)boot_seq;
    return 0x18C040U;   /* deliberately slot-1's offset while on slot 0 -> XIP fault (rollback test) */
#else
    return XIP_FLASH_DATA_OFFSET + boot_seq * XIP_SLOT_STRIDE;
#endif
}

/*
 * Arm A/B try-boot rollback. Runs from SRAM in platform_init_level0, BEFORE
 * HAL_Xip_Init() and before any XIP code executes — so it guards the exact
 * no-boot brick class (a wrong XIP bias faults the moment the
 * first XIP instruction runs, which is after level0 returns).
 *
 * Ordering is deliberate and load-bearing:
 *   1. capture the slot we booted from (running_seq, set by image_init's cfg read)
 *   2. repoint the OTA cfg at the OTHER slot (known-good)  <-- MUST be before (3)
 *   3. start the watchdog (no feed until hokku_rollback_commit)
 * If a fault occurs between (2) and (3) there is no watchdog yet, but the cfg
 * already points at the good slot, so any later reset still rolls back. If we
 * armed the watchdog first, a fire in that window would reboot into the still-bad
 * cfg and crash-loop. image_set_cfg only persists IMAGE_STATE_VERIFIED and does a
 * write+readback (2 tries) internally; we arm the watchdog only if it succeeded.
 *
 * All callees are ROM functions (image_*, HAL_WDG_*), valid before XIP is up.
 */
static void hokku_rollback_arm(void)
{
    image_cfg_t   cfg;
    WDG_InitParam wdg;

    g_boot_seq = image_get_running_seq();
    image_seq_t good = (image_seq_t)((g_boot_seq + 1) % IMAGE_SEQ_NUM);

    /* Only arm if the fallback slot actually holds a valid image. An OTA erases
     * its target (the "other") slot UP FRONT, before downloading — so a failed or
     * interrupted OTA can leave that slot blank. Repointing the boot cfg at a
     * blank slot and arming the WDG would risk a reset INTO an unbootable slot
     * (the exact "both slots dead" case). If the fallback isn't valid, skip the
     * rollback this boot: run our own (booted) slot best-effort; USB BROM recovery
     * remains the backstop. All callees here are ROM funcs, valid before XIP. */
    if (image_check_sections(good) != IMAGE_VALID)
        return;

    cfg.seq   = good;
    cfg.state = IMAGE_STATE_VERIFIED;
    if (image_set_cfg(&cfg) != 0) {
        /* Could not repoint the cfg (flash write/readback failed). Arming the
         * watchdog now would crash-loop into our own (unproven) slot, so don't.
         * We boot best-effort; USB BROM recovery remains the backstop. */
        return;
    }

    memset(&wdg, 0, sizeof(wdg));
    wdg.hw.event      = WDG_EVT_RESET;          /* full system reset -> ROM -> bootloader */
    wdg.hw.timeout    = WDG_TIMEOUT_16SEC;      /* hardware max; covers boot-to-main */
    wdg.hw.resetCycle = WDG_DEFAULT_RESET_CYCLE;
    HAL_WDG_Init(&wdg);
    HAL_WDG_Start();
    g_rollback_armed = 1;
}

/*
 * Confirm the boot reached a healthy milestone: re-point the OTA cfg back at our
 * own (now-proven) slot and stop the watchdog. Called from main() right after
 * platform_init() returns — i.e. once XIP, the SDK init, and the console are up,
 * but before the WiFi/EPD work (which can exceed the 16 s window). Reaching this
 * point means the brick class (no-boot) did not occur, which is exactly what the
 * rollback guards against; functional faults past here are recoverable normally.
 */
void hokku_rollback_commit(void)
{
    image_cfg_t cfg;

    if (!g_rollback_armed)
        return;

    cfg.seq   = g_boot_seq;
    cfg.state = IMAGE_STATE_VERIFIED;
    if (image_set_cfg(&cfg) != 0) {
        /* Leave the watchdog running: if we cannot commit, better to roll back
         * to the known-good slot than to adopt an unconfirmed image. */
        hlog("hokku: rollback COMMIT FAILED (set_cfg) — will roll back to seq %d\n",
               (g_boot_seq + 1) % IMAGE_SEQ_NUM);
        return;
    }
    HAL_WDG_Stop();
    g_rollback_armed = 0;
    hlog("hokku: boot confirmed healthy; running seq %d, rollback disarmed\n",
           g_boot_seq);
}

#if HOKKU_B0_WDGTEST
/* Phase B0 watchdog-semantics test. Prints reset cause; on a cold/power-on boot it
 * arms WDG_EVT_RESET (2 s) and hangs; on a watchdog-induced boot it reports and halts. */
static void hokku_b0_wdgtest(void)
{
    int pwron   = !!(g_b0_rst_src & RST_SRC_PWRON_BIT);
    int wdg_all = !!(g_b0_rst_src & RST_SRC_WDG_ALL_BIT);
    int wdg_cpu = (g_b0_rst_src & RST_SRC_WDG_CPU_MASK) >> 9;

    printf("\nB0: reset_source=0x%08x (pwron=%d wdg_all=%d wdg_cpu=%d)\n",
           (unsigned)g_b0_rst_src, pwron, wdg_all, wdg_cpu);
    printf("B0: boot_flag=0x%x (0==COLD_RESET) boot_arg=0x%08x wdg_cfg=0x%08x\n",
           (unsigned)g_b0_boot_flag, (unsigned)g_b0_boot_arg, (unsigned)g_b0_wdg_cfg);

    if (wdg_all || wdg_cpu) {
        /* Loop-print the verdict so a UART capture started any time after the
         * CH340 re-enumerates (post power-on) will catch it. No re-arm. */
        const char *verdict = (wdg_all && g_b0_boot_flag == 0)
            ? "PASS — rollback reset semantics are SAFE"
            : "FAIL — DO NOT trust rollback";
        while (1) {
            printf("B0: *** WATCHDOG RESET CONFIRMED — bootloader ran, app re-reached. ***\n");
            printf("B0: WDG timeout %s a full system reset; boot_flag %s COLD_RESET.\n",
                   wdg_all ? "PRODUCED" : "did NOT produce (CPU-only)",
                   g_b0_boot_flag == 0 ? "==" : "!=");
            printf("B0: F1 verdict: %s\n", verdict);
            OS_MSleep(2000);
        }
    }

    printf("B0: cold/power-on boot — arming HAL_WDG_Init(WDG_EVT_RESET, 2s)+Start, then hanging.\n");
    printf("B0: expect a reset in ~2 s; watch for the bootloader log + this app re-running.\n");
    {
        WDG_InitParam wdg;
        memset(&wdg, 0, sizeof(wdg));
        wdg.hw.event      = WDG_EVT_RESET;
        wdg.hw.timeout    = WDG_TIMEOUT_2SEC;
        wdg.hw.resetCycle = WDG_DEFAULT_RESET_CYCLE;
        HAL_WDG_Init(&wdg);
        HAL_WDG_Start();
    }
    while (1) { }   /* hang, no feed -> watchdog must fire */
}
#endif /* HOKKU_B0_WDGTEST */

/* Strong override of the SDK's __weak platform_init_level0 (runs from SRAM). */
void platform_init_level0(void)
{
    uint32_t boot_seq;

    pm_start();
    HAL_Flash_Init(0);
    image_init(0, 0, 0);
    /* Cached from image_init's cfg read; capture BEFORE hokku_rollback_arm()
     * repoints the persisted cfg, so it reflects the slot we actually booted. */
    boot_seq = (uint32_t)image_get_running_seq();
#if HOKKU_B0_WDGTEST
    /* Capture reset-cause registers as early as possible (raw, no HAL/XIP dependency)
     * and do NOT arm the A/B rollback — B0 must keep the cfg pointing at itself. */
    g_b0_rst_src   = PRCM_CPU_RESET_SOURCE_REG;
    g_b0_boot_flag = PRCM_CPUA_BOOT_FLAG_REG & 0xF;
    g_b0_boot_arg  = PRCM_CPUA_BOOT_ARG_REG;
    g_b0_wdg_cfg   = WDG_CFG_REG;
#else
    hokku_rollback_arm();                     /* repoint cfg to good slot + arm WDG */
#endif
    {
        uint32_t xip_off = hokku_xip_offset(boot_seq);   /* per-slot: booted slot's app_xip */
        HAL_Xip_Init(0, xip_off);            /* sets read mode + BIAS_ADDR0 */
        platform_cache_init();
        /* Belt-and-suspenders: force the bias in case HAL_Xip_Init bailed early
         * (e.g. flash chip not recognized) before programming the register. */
        OPI_MEM_CTRL_BIAS_ADDR0 = XIP_BIAS_ENABLE | xip_off;
    }
}

/*
 * WiFi credential persistence.
 *
 * The SDK's `net sta config` only sets the runtime wpa_supplicant config — it is
 * NOT written to flash, so a cold boot comes up with no network. sysinfo (fdcm at
 * PRJCONF_SYSINFO_ADDR, see prj_config.h) is the persistent store: wlan_sta_param
 * holds ssid/psk and sysinfo_save() writes it to flash. Nothing in the SDK auto-
 * connects from it, so we do both halves here:
 *   - hokku_wifi_provision(): save creds to sysinfo + connect now  (`wifi` command)
 *   - hokku_wifi_connect_saved(): at boot, connect from saved sysinfo creds
 *
 * WLAN_STA_CONF_FLAG_WPA3 advertises WPA3 support but negotiates down to WPA2-PSK,
 * which is what a WPA2/WPA3-mixed AP actually associates with.
 */
static int hokku_wifi_connect(const uint8_t *ssid, uint8_t ssid_len, const uint8_t *psk)
{
    if (wlan_sta_config((uint8_t *)ssid, ssid_len, (uint8_t *)psk,
                        WLAN_STA_CONF_FLAG_WPA3) != 0) {
        hlog("hokku: wlan_sta_config failed\n");
        return -1;
    }
    return wlan_sta_enable();
}

/* Persist creds to sysinfo and connect. `psk` must be NUL-terminated. */
int hokku_wifi_provision(const char *ssid, const char *psk)
{
    struct sysinfo *si = sysinfo_get();
    size_t slen = strlen(ssid);
    size_t plen = strlen(psk);

    if (si == NULL) {
        hlog("hokku: sysinfo unavailable — cannot persist WiFi\n");
        return -1;
    }
    if (slen == 0 || slen > SYSINFO_SSID_LEN_MAX || plen >= SYSINFO_PSK_LEN_MAX) {
        hlog("hokku: bad ssid (%u) / psk (%u) length\n",
               (unsigned)slen, (unsigned)plen);
        return -1;
    }

    memset(si->wlan_sta_param.ssid, 0, sizeof(si->wlan_sta_param.ssid));
    memcpy(si->wlan_sta_param.ssid, ssid, slen);
    si->wlan_sta_param.ssid_len = (uint8_t)slen;
    memset(si->wlan_sta_param.psk, 0, sizeof(si->wlan_sta_param.psk));
    memcpy(si->wlan_sta_param.psk, psk, plen);
    si->wlan_mode = WLAN_MODE_STA;

    if (sysinfo_save() != 0)
        hlog("hokku: WARNING sysinfo_save failed — creds NOT persisted\n");
    else
        hlog("hokku: WiFi creds saved to sysinfo (ssid '%s')\n", ssid);

    return hokku_wifi_connect(si->wlan_sta_param.ssid, si->wlan_sta_param.ssid_len,
                              si->wlan_sta_param.psk);
}

/* Connect using creds previously saved in sysinfo. No-op if none saved. */
static void hokku_wifi_connect_saved(void)
{
    struct sysinfo *si = sysinfo_get();

    if (si == NULL || si->wlan_sta_param.ssid_len == 0) {
        hlog("hokku: no saved WiFi — provision once with: wifi <ssid> <password>\n");
        return;
    }
    hlog("hokku: auto-connecting to saved SSID '%.*s'\n",
           si->wlan_sta_param.ssid_len, si->wlan_sta_param.ssid);
    hokku_wifi_connect(si->wlan_sta_param.ssid, si->wlan_sta_param.ssid_len,
                       si->wlan_sta_param.psk);
}

int main(void)
{
    /* Create the OTA/refresh lock before platform_init() (which brings up the
     * console) so a `ota` command can never reference an uninitialised mutex. */
    OS_MutexCreate(&g_ota_lock);

    platform_init();

    printf("\nhokku bigme-f7 firmware\n");

#if HOKKU_B0_WDGTEST
    hokku_b0_wdgtest();   /* hangs on cold boot (WDG fires); reports+returns on WDG boot */
    return 0;
#else
    observer_base *net_ob;

    /* Boot-critical path (XIP + SDK init + console) survived: adopt this image
     * and disarm the try-boot watchdog. Must run before the WiFi/EPD work, which
     * can exceed the 16 s rollback window. No-op on a normally-flashed (non-A/B)
     * unit where the cfg was never repointed. */
    hokku_rollback_commit();

    /* Load persistent app config (server URL, screen name, static IP, ...) from
     * flash, or compile-time defaults on first boot. Must precede WiFi/refresh. */
    hokku_config_load();

    /* Capture why we booted (timer = returned from hibernation) for frame-state. */
    hokku_capture_wake();

    printf("WiFi: 'wifi <ssid> <password>' to provision, 'cfg' to configure\n\n");

    net_ob = sys_callback_observer_create(CTRL_MSG_TYPE_NETWORK,
                                          NET_CTRL_MSG_ALL,
                                          net_cb,
                                          NULL);
    if (net_ob == NULL)
        return -1;
    if (sys_ctrl_attach(net_ob) != 0)
        return -1;

    /* Auto-connect from saved creds. The observer is attached first so the
     * connect/network-up events reach net_cb (which sets the static IP and
     * starts the refresh thread). */
    hokku_wifi_connect_saved();

    return 0;
#endif
}
