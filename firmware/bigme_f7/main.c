/*
 * Hokku EPaper firmware for Bigme F7 (XR872AT + EK79655 7-color EPD)
 *
 * Flow:
 *   platform_init(); the boot cfg + watchdog handling is in "A/B try-boot
 *   rollback" below. main() loads config + state, connects WiFi from the saved
 *   sysinfo credentials and starts the refresh thread, which loops:
 *     wait for the network -> POST /hokku/screen/ -> stream the 192000-byte
 *     4bpp body into the EPD -> act on the reply exactly like every other
 *     screen (common/all: fetch_outcome, schedule, messages, ota_confirm) ->
 *     hibernate (battery) or wait (USB) until the server's next fetch time.
 *
 * WiFi provisioning via UART console:
 *   wifi <ssid> <password>
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
#include "lwip/netifapi.h"
#include "lwip/ip_addr.h"

#include "image/image.h"
#include "ota/ota.h"
#include "console/console.h"
#include "driver/chip/hal_uart.h"
#include "driver/chip/hal_wdg.h"
#include "driver/chip/hal_adc.h"
#include "driver/chip/hal_wakeup.h"
#include "net/wlan/wlan.h"
#include "common/framework/sysinfo.h"
#include "pm/pm.h"

#include "epd.h"
#include "led.h"
#include "hokku_config.h"

/* SoC-agnostic shared code (firmware/common/all — pure C, no SDK headers). */
#include "firmware_url.h"
#include "frame_state.h"
#include "fetch_outcome.h"
#include "schedule.h"
#include "messages.h"
#include "ota_confirm.h"
#include "http_headers.h"
#include "logbuf.h"
#include "frame_proto.h"
#include "interactive.h"
#include "screen_ident.h"

/* SoC-shared XR872 code (firmware/common/xr872 — usable by any XR872/XR872AT
 * screen): activity log, software clock, HTTP-header helpers, hibernation,
 * persistent runtime state. */
#include "log.h"
#include "clock.h"
#include "http_util.h"
#include "pm.h"
#include "state.h"

#define SCREEN_NAME             "bigme-f7"
#define SCREEN_MODEL            "bigme_f7"
#define FIRMWARE_VERSION        "1.2.17"

#define HTTP_TIMEOUT_S          90       /* covers 192KB DL + EPD streaming time */

/* How long a fetch waits for the network before it counts as "WiFi connect
 * failed". Covers WLAN bring-up after a boot, association and DHCP. */
#define HOKKU_WIFI_WAIT_S       30

/* With no WiFi saved at boot, how long to leave the console alone (a flasher
 * provisions WiFi over it right after the first boot) before putting the
 * config message on the glass. */
#define HOKKU_NO_WIFI_GRACE_S   15

/* How a person asks this screen to retry right now (common/all/messages.h).
 * The button is a hardware power latch the firmware cannot see
 * (docs/screens/bigme_f7/hardware_facts.md), so: a power cycle. */
#define HOKKU_RETRY_HINT        "Turn the screen off\nand on again to\ntry again now."

#define REFRESH_THREAD_STACK    (8 * 1024)
#define REFRESH_THREAD_PRIO     OS_THREAD_PRIO_APP

/* Config schema version reported to the server (frame-state cfg_ver). */
#define HOKKU_CFG_VER           1
/* Firmware build stamp (X-Firmware-Build), passed in by gcc/Makefile in the
 * ESP32 boards' format; the fallback only serves the host tests. */
#ifndef HOKKU_BUILD_TS
#define HOKKU_BUILD_TS          (__DATE__ " " __TIME__)
#endif

static OS_Thread_t g_refresh_thread;
static int         g_epd_ready = 0;

/* NETWORK_UP seen and not followed by NETWORK_DOWN (set by net_cb). */
static volatile int g_net_up;

/*
 * Serializes the network+flash critical section so an OTA (flash erase/write via a
 * second HTTP session) can never run concurrently with a periodic refresh or a
 * second OTA. The refresh thread holds it around each fetch; the console `ota`
 * command TRY-locks it and refuses if the refresh thread is busy. hokku_do_ota()
 * itself does NOT lock — its callers already hold the lock (no recursive lock).
 */
static OS_Mutex_t  g_ota_lock;

/*
 * Wakes the awake-mode refresh wait early. The server can hand out a sleep of
 * many hours (overnight), and the refresh thread used to OS_MSleep() through it,
 * so a `wifi` switch (or any reconnect) was not followed by a check-in until that
 * sleep ended — it looked like the unit had stopped checking in. net_cb releases
 * this on NETWORK_UP, so the unit checks in on the new network straight away.
 * Binary: repeated releases collapse into one wake.
 */
static OS_Semaphore_t g_refresh_kick;

/* --------------------------------------------------------------------------
 * Reporting: wake reason, battery, and frame-state telemetry. The activity log
 * (hlog) and the software wall-clock (hokku_clock_*) are now shared XR872 code
 * in firmware/common/xr872 (log.h / clock.h).
 * ------------------------------------------------------------------------ */

/* SDK SRAM heap span (same accessor the `heap` console command uses). */
extern void heap_get_space(uint8_t **start, uint8_t **end, uint8_t **current);

/* Wake reason captured once at boot (frame-state "wake"): "timer" = hibernation wake. */
static const char *g_wake = "first_boot";
static int         g_timer_wake;   /* this boot is the end of an armed hibernation */

static void hokku_capture_wake(void)
{
    uint32_t ev = HAL_Wakeup_GetEvent();
    if (ev & PM_WAKEUP_SRC_WKTIMER) {
        g_wake = "timer";
        g_timer_wake = 1;
    } else if (ev != 0) {                      /* 0 == cold power-on */
        g_wake = "wake_io";
    }
}

/*
 * Battery read on ADC channel 4 (pin PA14) — the pack-sense line the OEM firmware
 * uses (NOT ADC_CHANNEL_VBAT, which reads the SoC's regulated internal rail and was
 * the source of the bogus steady ~2.58 V). Returns mV, or 0 if the ADC failed; the
 * shared frame-state builder drops an implausible reading, like on every board.
 * Channel + scaling confirmed by disassembling the OEM firmware.
 * HAL_ADC_Conv_Polling auto-configures the PA14->CH4 pinmux.
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
    return mv;
}

static int hokku_should_sleep(void);

/* Build the compact X-Frame-State telemetry JSON (server parses ota/bat_mv/clk_now). */
static void build_frame_state(char *buf, size_t sz)
{
    const hokku_state_t *st = hokku_state_get();
    wlan_sta_ap_t   ap;
    int             rssi = 0;
    uint8_t        *hs, *he, *hc;
    uint32_t        bat = hokku_battery_mv();

    if (wlan_sta_ap_info(&ap) == 0)
        rssi = (int)(int8_t)ap.rssi;         /* stored as signed dBm in a u8 */

    heap_get_space(&hs, &he, &hc);           /* free ~= end - current watermark */

    /* Gather XR872-specific values, then hand off to the shared (SoC-agnostic)
     * builder in common/all so the F7 reports the same schema as the ESP32
     * boards. spurious and wifi_cached are ESP32 mechanics (spurious EXT1
     * wakes, a BSSID fast-reconnect cache) this board does not have. */
    frame_state_t fs = {
        .fw       = FIRMWARE_VERSION,
        .boot     = (unsigned)st->boot_count,
        .wake     = g_wake,
        .regime   = hokku_should_sleep() ? "battery_idle" : "usb_awake",
        .uptime_s = (long long)(unsigned)OS_GetTime(),
        .bat_mv   = (int)bat,
        .usb      = led_usb_present() ? "host" : "none",
        .last_sleep = g_timer_wake ? "timer_wake" : "none",
        .rssi     = rssi,
        .heap_kb  = (unsigned)((he - hc) / 1024),
        .spurious = 0,
        .cfg_ver  = (unsigned)HOKKU_CFG_VER,
        .clk_now  = (long long)(unsigned)hokku_clock_now(),
        .wifi_cached     = false,
    };
    frame_state_set_schedule(&fs, &st->sched);
    frame_state_build(buf, sz, &fs);
}
/* This device's WiFi MAC as X-Screen-Mac ("" if unknown). sysinfo derives it
 * from the chip ID at every boot (PRJCONF_MAC_ADDR_SOURCE), so it is stable per
 * unit and is what the network sees. The server keys the screen by it, so a
 * rename or reflash keeps the screen's history. */
static void hokku_screen_mac_str(char *out, size_t len)
{
    const struct sysinfo *si = sysinfo_get();
    if (si == NULL) {
        if (len) out[0] = '\0';
        return;
    }
    hokku_mac_format(si->mac_addr, out, len);
}

/*
 * A/B try-boot rollback state.
 *
 * g_boot_seq is the image sequence the bootloader launched US from (captured in
 * platform_init_level0 before we touch the OTA cfg). g_rollback_armed is set once
 * we have (a) repointed the OTA cfg at the OTHER (known-good) slot and (b) started
 * the watchdog; it stays set until the cfg points back at our own slot.
 *
 * The semantics, as the bootloader sees them. The OTA cfg (fdcm at 0x180000,
 * image_cfg_t {seq, state}) names the slot the bootloader launches on EVERY
 * reset — power-on, watchdog, HAL_WDG_Reboot, and every hibernation wake, which
 * on this SoC is a full reboot through the bootloader. Each boot:
 *   level0  cfg -> the other slot, watchdog (16 s, the hardware max) started.
 *           Skipped when the other slot fails image_check_sections().
 *   main()  boot milestone (XIP, SDK init, console up): watchdog stopped
 *           (hokku_rollback_boot_ok). A normally booted image points the cfg
 *           back at itself here (hokku_rollback_commit): it is confirmed.
 * So a reset of a confirmed image reboots it; a reset while the cfg still points
 * at the other slot boots the other slot.
 *
 * An image an OTA just installed is NOT confirmed at the boot milestone: its cfg
 * stays on the previous slot until the first fetch that reaches the server
 * (common/all/ota_confirm.h). The state record (common/xr872/state.h) carries
 * the marker hokku_do_ota() set for it ("slot N, not confirmed"). Consequences:
 *   - the confirm-or-rollback decision completes in this first boot, before any
 *     hibernation: a hibernation wake while unconfirmed IS a rollback;
 *   - out of attempts -> HAL_WDG_Reboot() -> the previous slot boots;
 *   - a crash, a power cycle or power loss while unconfirmed -> the previous
 *     slot boots (the marker no longer matches the running slot and is dropped);
 *   - a crash before the milestone -> the watchdog, exactly as before.
 * The watchdog is stopped at the milestone either way: the confirm attempts run
 * minutes (WiFi, a 90 s HTTP timeout, a 30 s panel refresh), far past its 16 s.
 * A hang after the milestone therefore waits for a power cycle, which then boots
 * the previous slot (the ESP32 boards behave the same: their task watchdog does
 * not reset either). An image flashed over USB carries no marker and confirms at
 * the milestone, as before.
 *
 * On our target unit the candidate lives in slot 0 and the live OEM
 * firmware in slot 1, so g_boot_seq==0 and the rollback target is seq 1. The logic
 * is unit-agnostic: the good slot is always (g_boot_seq + 1) % IMAGE_SEQ_NUM.
 */
static image_seq_t g_boot_seq;
static int         g_rollback_armed = 0;
/* This boot runs an OTA'd image that has not reached the server yet. */
static int         g_ota_pending = 0;

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
 * Derive the firmware.bin URL from the configured screen server_url, e.g.
 *   "http://host:port/hokku/screen/" -> "http://host:port/hokku/firmware.bin?model=bigme_f7"
 * by keeping everything up to and including "/hokku/" and appending the OTA path.
 */
/* Model-tagged firmware endpoint from the server base URL. Delegates to the
 * shared (SoC-agnostic) derivation in common/all — same algorithm this used to
 * inline (anchor on /hokku/, raw fallback). */
static void hokku_build_firmware_url(char *out, size_t outsz)
{
    firmware_url_build(out, outsz, hokku_config_get()->server_url,
                       "firmware.bin?model=" SCREEN_MODEL);
}

/* Message text buffer. File scope rather than on the stack: the refresh thread
 * also runs the SDK's OTA download, and its 8 KB stack has better uses. Only used
 * under g_ota_lock (refresh thread, or the console `ota` which try-locks it). */
static char g_msg[HOKKU_MSG_MAX];

/* Put a message on the glass, once the panel is up (common/all/messages.h). */
static void hokku_show(const char *msg)
{
    if (g_epd_ready)
        epd_show_text(msg);
}

static void hokku_ota_show_failed(const char *stage)
{
    hokku_msg_ota_failed(g_msg, sizeof(g_msg), stage);
    hokku_show(g_msg);
}

/*
 * Run an A/B OTA update: stream the server's firmware image into the INACTIVE
 * slot, flip the boot cfg to it, and reboot into it. On success this does NOT
 * return — it reboots, and the new image confirms itself or rolls back under the
 * shared policy (see "A/B try-boot rollback"). On ANY failure it returns with the
 * current image still active and the boot cfg unchanged (the half-written slot
 * is never booted).
 *
 * Safe because: (1) the rollback WDG was already stopped at the boot milestone,
 * so the multi-second download won't trip it; (2) ota_get_image validates the
 * written section chain before we flip anything; (3) the cfg flip is the last
 * step; (4) it refuses while this image is itself unconfirmed: the other slot is
 * its rollback.
 */
static void hokku_do_ota(const char *server_ver)
{
    char url[192];
    hokku_state_t *st = hokku_state_get();
    const image_ota_param_t *iop = image_get_ota_param();
    image_seq_t upd = (image_seq_t)((image_get_running_seq() + 1) % IMAGE_SEQ_NUM);
    uint32_t wr_start = iop ? iop->addr[upd] : 0;
    uint32_t wr_end   = iop ? wr_start + IMAGE_AREA_SIZE(iop->img_max_size) : 0;

    if (g_ota_pending) {
        hlog("hokku: OTA refused — this image is not confirmed yet (slot %d is its rollback)\n",
             (int)upd);
        return;
    }

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
    hokku_show(HOKKU_MSG_OTA_START);

    if (ota_init() != OTA_STATUS_OK) {
        hlog("hokku: OTA ota_init failed\n");
        hokku_ota_show_failed("download");
        return;
    }
    if (ota_get_image(OTA_PROTOCOL_HTTP, url) != OTA_STATUS_OK) {
        hlog("hokku: OTA download/write failed (slot unchanged)\n");
        hokku_ota_show_failed("download");
        return;
    }
    /* Mark the new slot "not confirmed" BEFORE the cfg flips to it: from the
     * flip on, its first boot must find the marker. If the flip then fails the
     * marker is dropped again (it would not match the running slot anyway). */
    st->ota_pending = (uint8_t)(upd + 1);
    if (hokku_state_save() != 0)
        hlog("hokku: OTA could not mark the new image unconfirmed — it will confirm at boot\n");
    /* No verify trailer in our image (built without mkimage -O); the per-section
     * checksum walk inside ota_get_image already ran. VERIFY_NONE still commits
     * the boot cfg to the freshly written slot. */
    if (ota_verify_image(OTA_VERIFY_NONE, NULL) != OTA_STATUS_OK) {
        hlog("hokku: OTA verify/commit failed (slot unchanged)\n");
        st->ota_pending = 0;
        hokku_state_save();
        hokku_ota_show_failed("commit");
        return;
    }
    hlog("hokku: OTA OK — rebooting into new image\n");
    hokku_show(HOKKU_MSG_OTA_DONE);
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
 * Console-triggered raw frame upload (`frame` command). Receives one ready-made
 * panel buffer over the console UART and streams it to the EPD — no server, no
 * WiFi, no render pipeline. Used for bring-up and for colour measurement, where
 * an exact known raster has to reach the glass on demand.
 *
 * The device stays a dumb pipe: the host decides what to send, so new test
 * images never need a rebuild. See firmware/common/all/frame_proto.h for the
 * wire exchange and tools/f7_send_frame.py for the host side.
 *
 * Console recovery is the load-bearing property here. Taking the UART means
 * detaching the console's RX callback, so EVERY exit path must put it back or
 * the unit loses its command interface (and with it the `upgrade` route into
 * BROM). Hence: one exit, no early returns, and a bounded per-chunk timeout so a
 * host that dies mid-transfer cannot wedge us. Belt and braces, console_disable
 * state is pure RAM — any reboot restores it, and the mask-BROM replug+press
 * catch works regardless of what the app is doing.
 *
 * The rollback watchdog is already stopped by hokku_rollback_commit() before any
 * console command can run, so the ~17 s transfer cannot trigger a reset.
 */
static uint8_t g_frame_buf[FRAME_PROTO_CHUNK_BYTES];

int hokku_frame_receive(void)
{
    UART_ID  uart;
    uint32_t received = 0;
    uint32_t crc = 0;
    uint8_t  ack = FRAME_PROTO_ACK;
    int      ok = 1;

    if (!OS_MutexIsValid(&g_ota_lock) || OS_MutexLock(&g_ota_lock, 0) != OS_OK) {
        hlog("hokku: frame busy (refresh/OTA in progress) — retry shortly\n");
        return -1;
    }

    uart = console_get_uart_id();
    printf("%s %u %u\r\n", FRAME_PROTO_READY,
           (unsigned)EPD_IMAGE_BYTES, (unsigned)FRAME_PROTO_CHUNK_BYTES);

    /* From here the console does not own the UART. Do not return early. */
    console_disable();

    epd_send_cmd(0x10);                     /* DTM: data start transmission */

    while (received < EPD_IMAGE_BYTES) {
        uint32_t want = frame_proto_chunk_size(EPD_IMAGE_BYTES,
                                               FRAME_PROTO_CHUNK_BYTES,
                                               received / FRAME_PROTO_CHUNK_BYTES);
        int32_t  n = HAL_UART_Receive_Poll(uart, g_frame_buf, (int32_t)want,
                                           FRAME_PROTO_RX_TIMEOUT_MS);
        uint32_t i;

        if (n != (int32_t)want) {           /* timeout or short read: host gone */
            ok = 0;
            break;
        }
        crc = frame_proto_crc32(crc, g_frame_buf, want);
        for (i = 0; i < want; i++)
            epd_send_data(g_frame_buf[i]);
        received += want;
        console_write(&ack, 1);             /* flow control: host may send more */
    }

    console_enable();                       /* single restore point */

    if (!ok) {
        /* The panel is left mid-DTM; a later `frame` re-issues 0x10 and starts
         * over. Deliberately NOT refreshing — showing a half-received picture
         * during colour measurement is worse than showing nothing. */
        hlog("hokku: frame ABORTED at %u/%u bytes — not refreshing\n",
             (unsigned)received, (unsigned)EPD_IMAGE_BYTES);
        OS_MutexUnlock(&g_ota_lock);
        return -1;
    }

    printf("%s %08x\r\n", FRAME_PROTO_DONE, (unsigned)crc);
    hlog("hokku: frame received (%u B, crc %08x) — refreshing\n",
         (unsigned)received, (unsigned)crc);
    epd_refresh();                          /* ~30 s */
    printf("%s\r\n", FRAME_PROTO_REFRESHED);

    OS_MutexUnlock(&g_ota_lock);
    return 0;
}

/*
 * Fetch one image from the server, transport only: fills *res with what the
 * reply carried. The body is streamed byte-by-byte into the EPD's frame memory
 * as it arrives; refresh_once() decides whether the panel then refreshes, so a
 * "keep your picture" reply or a broken download leaves the glass untouched.
 * A server-signalled OTA runs from here (and reboots when it succeeds).
 */
static void do_refresh(hokku_fetch_result_t *res)
{
    hokku_config_t *cfg = hokku_config_get();
    HTTPParameters  params;
    HTTP_CLIENT     info;
    char            buf[512];
    char            frame_state[384];
    UINT32          received = 0;
    uint32_t        log_sent;                 /* bytes snapshotted for the POST body */
    int             ret;

    build_frame_state(frame_state, sizeof(frame_state));

    /* Snapshot the circular log into a contiguous body for the POST. Log the
     * POST line first so it rides along in this upload. */
    hlog("hokku: POST %s (log %u B)\n", cfg->server_url, (unsigned)hlog_len());
    const char *log_body = hlog_snapshot(&log_sent);
    memset(&params, 0, sizeof(params));
    strncpy(params.Uri, cfg->server_url, sizeof(params.Uri) - 1);
    params.HttpVerb  = VerbPost;              /* POST so the log rides as the body */
    params.nTimeout  = HTTP_TIMEOUT_S;
    params.pData     = (void *)log_body;      /* request body = accumulated activity log */
    params.pLength   = log_sent;              /* dropped only after a 200 */
    ret = HTTPC_open(&params);
    if (ret != HTTP_CLIENT_SUCCESS) {
        hlog("hokku: HTTP open failed (%d)\n", ret);
        return;
    }

    /* Request headers (common/all/http_headers.h) */
    char mac_str[HOKKU_MAC_STR_LEN];
    hokku_screen_mac_str(mac_str, sizeof(mac_str));

    if (cfg->screen_name[0])
        HTTPClientAddRequestHeaders(params.pHTTP, HOKKU_HDR_SCREEN_NAME, cfg->screen_name, 1);
    HTTPClientAddRequestHeaders(params.pHTTP, HOKKU_HDR_SCREEN_MODEL, SCREEN_MODEL, 1);
    if (mac_str[0])
        HTTPClientAddRequestHeaders(params.pHTTP, HOKKU_HDR_SCREEN_MAC, mac_str, 1);
    HTTPClientAddRequestHeaders(params.pHTTP, HOKKU_HDR_FW_VERSION, FIRMWARE_VERSION, 1);
    HTTPClientAddRequestHeaders(params.pHTTP, HOKKU_HDR_FW_BUILD, HOKKU_BUILD_TS, 1);
    HTTPClientAddRequestHeaders(params.pHTTP, HOKKU_HDR_FRAME_STATE, frame_state, 1);
    if (log_sent)
        HTTPClientAddRequestHeaders(params.pHTTP, "Content-Type", "text/plain", 1);

    ret = HTTPC_request(&params, NULL);
    if (ret != HTTP_CLIENT_SUCCESS) {
        hlog("hokku: HTTP request failed (%d)\n", ret);
        HTTPC_close(&params);
        return;
    }

    if (HTTPC_get_request_info(&params, &info) != HTTP_CLIENT_SUCCESS) {
        HTTPC_close(&params);
        return;
    }

    /* The server answered. X-Sleep-Seconds, the server time and the drift seed
     * ride on every reply, including the no-image ones (503 converting, 404 no
     * label match / empty library); a reply carrying the time sets the clock. */
    res->http_status = (int)info.HTTPStatusCode;
    {
        char v[24], n[16];
        if (read_resp_header_str(params.pHTTP, HOKKU_HDR_SLEEP_SECONDS, v, sizeof(v)))
            res->sleep_s = hokku_sleep_seconds_parse(v);
        if (read_resp_header_str(params.pHTTP, HOKKU_HDR_SERVER_TIME, v, sizeof(v)))
            res->server_epoch = hokku_server_epoch_parse(v);
        read_resp_header_str(params.pHTTP, HOKKU_HDR_SLEEP_CAL_PPM, v, sizeof(v));
        read_resp_header_str(params.pHTTP, HOKKU_HDR_SLEEP_CAL_N, n, sizeof(n));
        if (!hokku_cal_seed_parse(v, n, &res->cal_seed_ppm, &res->cal_seed_n))
            res->cal_seed_n = 0;
    }
    if (res->server_epoch > 0)
        hokku_clock_set((uint32_t)res->server_epoch);

    if (info.HTTPStatusCode != 200) {
        hlog("hokku: server returned %u\n", (unsigned)info.HTTPStatusCode);
        HTTPC_close(&params);
        return;
    }

    /* The POST body (log) reached the server in a 200 — clear the buffer so the
     * next cycle starts fresh. (A handful of lines logged during this exchange
     * are dropped too; the F7 uploads every cycle so little accumulates.) */
    hlog_reset();

    char fw_update[48] = "";
    read_resp_header_str(params.pHTTP, HOKKU_HDR_FW_UPDATE, fw_update, sizeof(fw_update));
    {
        /* The server owns the name of a screen it knows (set in its web UI):
         * adopt it when it differs, so the next request carries it. One byte
         * beyond the max so an over-long value is refused rather than
         * truncated into a valid one. */
        char name[HOKKU_SCREEN_NAME_MAX + 2];
        if (read_resp_header_str(params.pHTTP, HOKKU_HDR_SCREEN_NAME, name, sizeof(name)) &&
            strcmp(name, cfg->screen_name) != 0) {
            if (hokku_config_set_screen_name(name) == 0)
                hlog("hokku: renamed to '%s'\n", cfg->screen_name);
            else
                hlog("hokku: rename refused\n");
        }
    }

    /* OTA takes priority over display: the server told us to update. Discard the
     * image body, close the socket, and run the A/B update (reboots on success;
     * returns only on failure, in which case we keep the current image). */
    if (fw_update[0]) {
        hlog("hokku: firmware update signalled -> %s\n", fw_update);
        HTTPC_close(&params);
        hokku_do_ota(fw_update);
        res->ota_failed = true;
        return;
    }

    /* Stream the body into the EPD frame memory. Read until the stream ends so
     * an over-long body is seen (hokku_image_size_ok wants it exact); stop as
     * soon as it is past the panel size. */
    epd_send_cmd(0x10);  /* DTM: data start transmission */
    do {
        UINT32 n = 0;
        ret = HTTPC_read(&params, buf, (UINT32)sizeof(buf), &n);
        for (UINT32 i = 0; i < n && received + i < EPD_IMAGE_BYTES; i++)
            epd_send_data((uint8_t)buf[i]);
        received += n;
    } while (ret == HTTP_CLIENT_SUCCESS && received <= EPD_IMAGE_BYTES);

    HTTPC_close(&params);

    res->image_ok = hokku_image_size_ok(received, EPD_IMAGE_BYTES);
    if (!res->image_ok)
        hlog("hokku: image size %u, expected %u\n",
             (unsigned)received, (unsigned)EPD_IMAGE_BYTES);
}

/* Wait up to timeout_s for the network to be up. */
static int hokku_wait_network(uint32_t timeout_s)
{
    uint32_t waited_ms = 0;
    while (!g_net_up && waited_ms < timeout_s * 1000U) {
        OS_MSleep(100);
        waited_ms += 100;
    }
    return g_net_up;
}

/*
 * One fetch, acted on like every other screen does: the shared decision, next
 * fetch time and drift calibration (hokku_sched_apply_fetch), then the image or
 * the shared error message on the glass. Returns the outcome's action.
 */
static hokku_fetch_action_t refresh_once(void)
{
    hokku_state_t *st = hokku_state_get();
    hokku_fetch_result_t res = { .http_status = 0 };   /* 0 = no response */

    if (!hokku_wait_network(HOKKU_WIFI_WAIT_S)) {
        hlog("hokku: WiFi connect failed (no network after %u s)\n",
             (unsigned)HOKKU_WIFI_WAIT_S);
        res.wifi_failed = true;
    } else {
        do_refresh(&res);
    }

    uint32_t up = OS_GetTime();
    hokku_sched_now_t now = {
        .now_epoch  = (int64_t)hokku_clock_now(),
        .mono_us    = (int64_t)up * 1000000LL,
        .awake_s    = (int64_t)up,
        .timer_wake = g_timer_wake != 0,
    };
    hokku_fetch_outcome_t o = hokku_sched_apply_fetch(&st->sched, &res, &now);
    hlog("hokku: fetch status=%d -> %s, next in %d s (cal %d ppm, %u samples)\n",
         res.http_status, o.reason, (int)o.sleep_s,
         (int)st->sched.cal_ppm, (unsigned)st->sched.cal_samples);

    if (o.action == HOKKU_FETCH_DISPLAY) {
        hlog("hokku: image received, refreshing display...\n");
        epd_refresh();  /* ~30 s */
        hlog("hokku: refresh done\n");
    } else if (hokku_msg_fetch_error(g_msg, sizeof(g_msg), &res, &o,
                                     hokku_config_get()->server_url, HOKKU_RETRY_HINT)) {
        hokku_show(g_msg);
    }
    return o.action;
}

/* ── New-firmware confirmation (common/all/ota_confirm.h), F7 mechanics ── */

void hokku_rollback_commit(void);

/* Keep this image: point the boot cfg at our own slot and drop the marker. */
static void hokku_ota_confirm(void)
{
    hokku_state_t *st = hokku_state_get();

    hokku_rollback_commit();
    if (g_rollback_armed) {
        /* Could not repoint the cfg: the next reset rolls back. Better than
         * adopting an image we could not confirm. */
        hlog("hokku: new firmware reached the server but could not be confirmed\n");
        return;
    }
    g_ota_pending = 0;
    st->ota_pending = 0;
    hokku_state_save();
    hlog("hokku: new firmware reached the server: confirmed (seq %d)\n", (int)g_boot_seq);
}

static hokku_fetch_action_t ota_fetch(void *c)  { (void)c; return refresh_once(); }
static void ota_wait_s(void *c, unsigned s)
{
    (void)c;
    hlog("hokku: new firmware: server not reached, trying again in %u s\n", s);
    OS_MSleep(s * 1000U);
}
static void ota_confirm(void *c)  { (void)c; hokku_ota_confirm(); }
static void ota_rollback(void *c)
{
    (void)c;
    hlog("hokku: new firmware never reached the server: rolling back to seq %d\n",
         (g_boot_seq + 1) % IMAGE_SEQ_NUM);
    OS_MSleep(200);          /* flush the log over UART */
    HAL_WDG_Reboot();        /* the cfg still names the previous slot */
}

/* A boot's first fetch: under the confirm policy when this image is pending. */
static hokku_fetch_action_t hokku_first_fetch(void)
{
    hokku_ota_first_fetch_t ops = {
        .pending  = g_ota_pending != 0,
        .fetch    = ota_fetch,
        .wait_s   = ota_wait_s,
        .confirm  = ota_confirm,
        .rollback = ota_rollback,
        .ctx      = NULL,
    };
    return hokku_ota_first_fetch(&ops);
}


/* hokku_hibernate() is now shared XR872 code in firmware/common/xr872/pm.h. */

/* True when the device should deep-sleep (vs stay awake) between refreshes. */
static int hokku_should_sleep(void)
{
    /* A host driving this screen over USB needs the console to still be there
     * next time it looks. Hibernating closes it, so interactive mode outranks
     * the configured power mode — including an explicit `power sleep`, which is
     * a standing preference rather than an instruction about right now.
     *
     * Gated on USB inside hokku_interactive_engaged(): if the cable is pulled
     * while the mode is set, this falls straight back to the configured
     * behaviour instead of sitting awake until the battery is flat. */
    if (hokku_interactive_engaged(led_usb_present()))
        return 0;

    switch (hokku_config_get()->power_mode) {
    case HOKKU_PWR_SLEEP: return 1;
    case HOKKU_PWR_AWAKE: return 0;
    default:              return !led_usb_present();   /* AUTO: sleep only on battery */
    }
}

/* Awake-mode wait between refreshes: `ms`, or less if net_cb kicks it. */
static void hokku_refresh_wait(uint32_t ms)
{
    if (!OS_SemaphoreIsValid(&g_refresh_kick)) {
        OS_MSleep(ms);
        return;
    }
    if (OS_SemaphoreWait(&g_refresh_kick, ms) == OS_OK)
        hlog("hokku: network came (back) up — refreshing now\n");
}

static int hokku_wifi_saved(void)
{
    const struct sysinfo *si = sysinfo_get();
    return si != NULL && si->wlan_sta_param.ssid_len > 0;
}

/*
 * No WiFi saved: this screen cannot fetch, so it says so on the glass (the
 * shared "cannot read config" message) and waits for `wifi` on the console.
 * Unlike the ESP32 boards it stays awake rather than sleeping: the console is
 * how this board is provisioned, and hibernation would close it. The grace
 * period leaves a flasher's provisioning, right after the first boot, alone.
 */
static void hokku_wait_for_wifi_config(void)
{
    uint32_t s;

    for (s = 0; !hokku_wifi_saved() && s < HOKKU_NO_WIFI_GRACE_S; s++)
        OS_MSleep(1000);
    if (hokku_wifi_saved())
        return;
    hlog("hokku: no WiFi saved — provision with: wifi <ssid> <password>\n");
    if (!hokku_interactive_engaged(led_usb_present())) {   /* a host owns the glass */
        OS_MutexLock(&g_ota_lock, OS_WAIT_FOREVER);
        hokku_show(HOKKU_MSG_CONFIG_MISSING);
        OS_MutexUnlock(&g_ota_lock);
    }
    while (!hokku_wifi_saved())
        OS_MSleep(1000);
}

static void refresh_thread_fn(void *arg)
{
    (void)arg;
    hlog("hokku: refresh thread started (wake=%s%s)\n", g_wake,
         g_ota_pending ? ", new firmware" : "");

    if (!g_epd_ready) {
        hlog("hokku: EPD init\n");
        epd_init();
        g_epd_ready = 1;
    }

    hokku_wait_for_wifi_config();

    int first = 1;
    while (1) {
        /* USB-interactive mode: a host owns the screen, so do not fetch and do
         * not repaint. Checked before taking the lock — a fetch holds it for the
         * length of a network round trip, and a `frame` upload waiting behind
         * that would stall for seconds with the host already mid-protocol.
         * Polled rather than event-driven because the thread has to keep
         * re-testing anyway: the mode is plain RAM and the USB cable can move.
         * Except for the first fetch of a new firmware image: it decides there,
         * in this boot, whether it stays (common/all/ota_confirm.h). */
        if (!(first && g_ota_pending) && hokku_interactive_engaged(led_usb_present())) {
            OS_MSleep(500);
            continue;
        }

        /* Hold the OTA/flash lock across the fetch (which may itself run an OTA
         * on X-Firmware-Update) so a console `ota` can't run concurrently. */
        OS_MutexLock(&g_ota_lock, OS_WAIT_FOREVER);
        if (first)
            hokku_first_fetch();     /* may confirm, or roll back (reboot) */
        else
            refresh_once();          /* may reboot via OTA */
        OS_MutexUnlock(&g_ota_lock);
        first = 0;

        hokku_state_t *st = hokku_state_get();
        int64_t now  = (int64_t)hokku_clock_now();
        int64_t mono = (int64_t)OS_GetTime() * 1000000LL;

        if (hokku_should_sleep()) {
            /* Battery: hibernate until the server's next fetch time, the timer
             * corrected for the learned drift and capped at the wake timer's
             * range. The record goes to flash first: hibernation is a reboot. */
            uint32_t secs = (uint32_t)hokku_sched_arm_sleep(&st->sched, now, mono,
                                                            HOKKU_FALLBACK_SLEEP_S,
                                                            HOKKU_HIBERNATE_MAX_S);
            hokku_state_save();
            led_park_for_sleep();                   /* nothing driving the LEDs while asleep */
            hokku_hibernate(secs);                  /* restarts on wake */
        } else {
            /* USB/awake: wait out the schedule on the running clock (no drift
             * correction while awake), or less if the network comes back up. A
             * kick from before this wait (the boot's own NETWORK_UP) is stale. */
            hokku_state_save();
            if (OS_SemaphoreIsValid(&g_refresh_kick))
                OS_SemaphoreWait(&g_refresh_kick, 0);
            hokku_refresh_wait((uint32_t)hokku_sched_remaining_s(&st->sched, now, mono,
                                                                 HOKKU_FALLBACK_SLEEP_S) * 1000U);
        }
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
        g_net_up = 1;
        /* Reconnect / network switch: check in now, don't sleep it out. */
        if (OS_ThreadIsValid(&g_refresh_thread) && OS_SemaphoreIsValid(&g_refresh_kick))
            OS_SemaphoreRelease(&g_refresh_kick);
        break;
    }
    case NET_CTRL_MSG_NETWORK_DOWN:
        hlog("hokku: network down\n");
        g_net_up = 0;
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
 * Confirm this image: re-point the OTA cfg back at our own slot and stop the
 * watchdog. A normally booted image does this at the boot milestone
 * (hokku_rollback_boot_ok); an image an OTA just installed does it once a fetch
 * has reached the server (hokku_ota_confirm). Reaching the milestone means the
 * brick class (no-boot) did not occur, which is exactly what the watchdog
 * guards against; functional faults past here are recoverable normally.
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

/*
 * The boot milestone: called from main() right after platform_init() — XIP, the
 * SDK init and the console are up — and before the WiFi/EPD work, which can
 * exceed the 16 s rollback window. A normally booted image confirms itself here.
 * An image an OTA just installed (pending) only stops the watchdog: its cfg keeps
 * naming the previous slot until a fetch reaches the server. No-op on a unit
 * whose cfg was never repointed.
 */
static void hokku_rollback_boot_ok(int pending)
{
    if (!g_rollback_armed)
        return;
    if (!pending) {
        hokku_rollback_commit();
        return;
    }
    HAL_WDG_Stop();
    hlog("hokku: new firmware in seq %d; seq %d stays selected until a fetch "
         "reaches the server\n", (int)g_boot_seq, (g_boot_seq + 1) % IMAGE_SEQ_NUM);
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
 *
 * The station is disabled BEFORE it is reconfigured, matching every SDK path that
 * re-points a live station (at_demo join, sc_assistant_port.c). Without it, a
 * `wifi` switch while associated replaced the supplicant config under a running
 * connection, and wlan_sta_enable() on an already-enabled station is a no-op: the
 * unit never left the old AP and stopped checking in until a reboot (issue #44).
 * Disabling an idle station at boot is harmless (the SDK paths do it unconditionally).
 *
 * The IPv4 address is dropped first, as the SDK's net_switch_mode() does. On a
 * plain disconnect the SDK keeps a BOUND lease (roaming), so on reconnect it
 * logs "netif is already up", never restarts DHCP and never sends NETWORK_UP:
 * a unit moved to another subnet would keep a stale address, and net_cb would
 * never kick the refresh thread. net_config(nif, 0) releases the DHCP lease; the
 * explicit clear also covers a static address (lwIP's release leaves those in
 * place). With no address, the reconnect runs DHCP (or net_cb's static set) and
 * the address change fires NETWORK_UP. Seen on hardware 2026-09-29 (1.2.14 test).
 */
static int hokku_wifi_connect(const uint8_t *ssid, uint8_t ssid_len, const uint8_t *psk)
{
    struct netif *nif = g_wlan_netif;

    if (nif != NULL && NET_IS_IP4_VALID(nif)) {
        net_config(nif, 0);                                /* release the lease */
        netifapi_netif_set_addr(nif, NULL, NULL, NULL);   /* and any static address */
    }
    wlan_sta_disable();
    if (wlan_sta_config((uint8_t *)ssid, ssid_len, (uint8_t *)psk,
                        WLAN_STA_CONF_FLAG_WPA3) != 0) {
        hlog("hokku: wlan_sta_config failed\n");
        wlan_sta_enable();   /* don't leave the radio off: retry whatever config remains */
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
    OS_SemaphoreCreateBinary(&g_refresh_kick);   /* before net_cb can release it */

    platform_init();

    /* Immediately after platform_init(), which enables XIP and with it the flash
     * pinmux that parks PB3 as FLASH_HOLD, driven HIGH. Claiming the pins here
     * takes them back and leaves both dark. (Whether that pinmux is what lights
     * the green LED is unconfirmed — see led.c.) */
    led_init();

    printf("\nhokku bigme-f7 firmware\n");

#if HOKKU_B0_WDGTEST
    hokku_b0_wdgtest();   /* hangs on cold boot (WDG fires); reports+returns on WDG boot */
    return 0;
#else
    observer_base *net_ob;

    /* Runtime state (schedule, drift calibration, boot count, OTA marker). */
    hokku_state_load();
    hokku_state_t *st = hokku_state_get();
    st->boot_count++;

    /* Is this the first boot of an image an OTA just installed? Only when the
     * marker names the slot we booted and there is a previous image to fall
     * back to. Any other marker is stale (that image confirmed, rolled back or
     * was reflashed) and is dropped. */
    if (st->ota_pending) {
        if (g_rollback_armed && st->ota_pending == (uint8_t)(g_boot_seq + 1))
            g_ota_pending = 1;
        else
            st->ota_pending = 0;
    }

    /* Boot-critical path (XIP + SDK init + console) survived: stop the try-boot
     * watchdog, and adopt this image unless it still has to prove itself. Must
     * run before the WiFi/EPD work, which can exceed the 16 s rollback window.
     * No-op on a normally-flashed (non-A/B) unit where the cfg was never
     * repointed. */
    hokku_rollback_boot_ok(g_ota_pending);

    /* Load persistent app config (server URL, screen name, static IP, ...) from
     * flash, or compile-time defaults on first boot. Must precede WiFi/refresh. */
    hokku_config_load();

    /* Capture why we booted (timer = returned from hibernation) for frame-state. */
    hokku_capture_wake();

    /* Hibernation stops the clock. After a timer wake, carry it on from the
     * armed sleep (corrected for the learned drift) until a reply sets it, as
     * the ESP32's RTC clock does through deep sleep. Any other boot is a power
     * cycle or a reset, which on the ESP32 boards clears the RTC state: start
     * the schedule and outage streak afresh the same way, keeping only the
     * calibration (which the ESP32 boards keep in NVS). */
    if (g_timer_wake) {
        int64_t est = hokku_sched_wake_estimate(&st->sched);
        if (est > 0)
            hokku_clock_set((uint32_t)(est + (int64_t)OS_GetTime()));
    } else {
        int32_t  ppm = st->sched.cal_ppm;
        uint16_t n   = st->sched.cal_samples;
        hokku_sched_init(&st->sched);
        st->sched.cal_ppm     = ppm;
        st->sched.cal_samples = n;
    }

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
     * marks the network up). */
    hokku_wifi_connect_saved();

    /* The refresh thread waits for the network itself, so an unreachable
     * network ends in the same outage handling as on every other screen. */
    OS_ThreadCreate(&g_refresh_thread, "hokku_refresh", refresh_thread_fn, NULL,
                    REFRESH_THREAD_PRIO, REFRESH_THREAD_STACK);

    return 0;
#endif
}
