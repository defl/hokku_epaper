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

#include "kernel/os/os.h"
#include "common/framework/platform_init.h"
#include "common/framework/net_ctrl.h"
#include "net/HTTPClient/HTTPCUsr_api.h"
#include "net/HTTPClient/API/HTTPClient.h"
#include "net/HTTPClient/API/HTTPClientCommon.h"
#include "lwip/netif.h"
#include "lwip/dhcp.h"

#include "image/image.h"
#include "driver/chip/hal_wdg.h"
#include "net/wlan/wlan.h"
#include "common/framework/sysinfo.h"

#include "epd.h"

/* Static IP config — used when DHCP is unavailable on the network */
#define STATIC_IP_ADDR   "192.168.6.199"
#define STATIC_GW_ADDR   "192.168.6.254"
#define STATIC_NM_ADDR   "255.255.255.0"

#define HOKKU_SERVER_URL        "http://192.168.6.111:8080/hokku/screen/"
#define SCREEN_NAME             "bigme-f7"
#define SCREEN_MODEL            "bigme_f7"
#define FIRMWARE_VERSION        "1.0.0"

#define EPD_IMAGE_BYTES         192000U  /* 800 x 480 x 4bpp / 8 */
#define DEFAULT_SLEEP_SECONDS   300
#define HTTP_TIMEOUT_S          90       /* covers 192KB DL + EPD streaming time */

#define REFRESH_THREAD_STACK    (8 * 1024)
#define REFRESH_THREAD_PRIO     OS_THREAD_PRIO_APP

static OS_Thread_t g_refresh_thread;
static int         g_epd_ready = 0;

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
 * Fetch one image from the server and stream it byte-by-byte to the EPD.
 * Returns the number of seconds to sleep before the next refresh, or -1 on
 * any error (caller should retry after a short backoff).
 */
static int do_refresh(void)
{
    HTTPParameters params;
    HTTP_CLIENT    info;
    char           hdr_val[32];
    UINT32         hdr_len;
    char           buf[512];
    UINT32         bytes_streamed = 0;
    int            sleep_sec = DEFAULT_SLEEP_SECONDS;
    int            ret;

    memset(&params, 0, sizeof(params));
    strncpy(params.Uri, HOKKU_SERVER_URL, sizeof(params.Uri) - 1);
    params.HttpVerb  = VerbGet;
    params.nTimeout  = HTTP_TIMEOUT_S;

    printf("hokku: GET %s\n", HOKKU_SERVER_URL);
    ret = HTTPC_open(&params);
    if (ret != HTTP_CLIENT_SUCCESS) {
        printf("hokku: HTTP open failed (%d)\n", ret);
        return -1;
    }

    /* Request headers — server uses these for logging / OTA checks */
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Screen-Name",     SCREEN_NAME,      1);
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Screen-Model",    SCREEN_MODEL,     1);
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Firmware-Version", FIRMWARE_VERSION, 1);
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Frame-State",     "{\"ver\":1}",     1);

    ret = HTTPC_request(&params, NULL);
    if (ret != HTTP_CLIENT_SUCCESS) {
        printf("hokku: HTTP request failed (%d)\n", ret);
        HTTPC_close(&params);
        return -1;
    }

    /* Check HTTP status code */
    if (HTTPC_get_request_info(&params, &info) != HTTP_CLIENT_SUCCESS) {
        HTTPC_close(&params);
        return -1;
    }
    if (info.HTTPStatusCode != 200) {
        printf("hokku: server returned %u\n", (unsigned)info.HTTPStatusCode);
        HTTPC_close(&params);
        /* For 503/404 (no image ready), use a short retry */
        return (info.HTTPStatusCode == 503 || info.HTTPStatusCode == 404) ? 30 : -1;
    }

    /* Capture sleep time from response header before consuming body */
    hdr_len = sizeof(hdr_val);
    if (HTTPClientFindFirstHeader(params.pHTTP, "X-Sleep-Seconds",
                                  hdr_val, &hdr_len) == HTTP_CLIENT_SUCCESS) {
        int s = atoi(hdr_val);
        if (s > 0)
            sleep_sec = s;
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
        printf("hokku: short image: %u / %u bytes\n",
               (unsigned)bytes_streamed, (unsigned)EPD_IMAGE_BYTES);
        return -1;
    }

    printf("hokku: image received, refreshing display...\n");
    epd_refresh();  /* ~30 s */
    printf("hokku: refresh done, sleeping %d s\n", sleep_sec);

    return sleep_sec;
}

static void refresh_thread_fn(void *arg)
{
    (void)arg;
    printf("hokku: refresh thread started\n");

    if (!g_epd_ready) {
        printf("hokku: EPD init\n");
        epd_init();
        g_epd_ready = 1;
    }

    while (1) {
        int sleep_sec = do_refresh();
        if (sleep_sec < 0) {
            /* Error — short backoff before retry */
            OS_MSleep(30 * 1000);
        } else {
            OS_MSleep((uint32_t)sleep_sec * 1000);
        }
    }
}

static void net_cb(uint32_t event, uint32_t data, void *arg)
{
    uint16_t type = EVENT_SUBTYPE(event);

    printf("hokku: net event 0x%04x data=0x%08x\n", (unsigned)type, (unsigned)data);

    switch (type) {
    case NET_CTRL_MSG_WLAN_CONNECTED: {
        /* DHCP is unreliable on this network — set static IP immediately.
         * netif_set_addr() triggers netif_status_callback() which fires
         * NET_CTRL_MSG_NETWORK_UP, so the refresh thread starts cleanly. */
        ip_addr_t ip, gw, nm;
        IP4_ADDR(&ip, 192, 168, 6, 199);
        IP4_ADDR(&gw, 192, 168, 6, 254);
        IP4_ADDR(&nm, 255, 255, 255, 0);
        struct netif *nif = netif_list;
        if (nif) {
            dhcp_stop(nif);
            /* Set addresses while the interface is down to avoid a spurious
             * NETWORK_DOWN callback with IP=0 from netif_set_up().
             * Setting ipaddr while down does not call netif_status_callback.
             * Then netif_set_up() calls the callback once with a valid IP,
             * triggering NET_CTRL_MSG_NETWORK_UP cleanly. */
            nif->ip_addr = ip;
            nif->netmask = nm;
            nif->gw      = gw;
            netif_set_up(nif);
            printf("hokku: static IP set  %s  gw=%s\n",
                   STATIC_IP_ADDR, STATIC_GW_ADDR);
        } else {
            printf("hokku: WLAN connected but no netif yet\n");
        }
        break;
    }
    case NET_CTRL_MSG_NETWORK_UP: {
        struct netif *nif2 = netif_list;
        if (nif2)
            printf("hokku: network up  ip=%s\n", ipaddr_ntoa(&nif2->ip_addr));
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
        printf("hokku: network down\n");
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
#if HOKKU_B_BREAK_XIP
#define XIP_FLASH_DATA_OFFSET     0x18C040U  /* WRONG for slot 0 -> XIP fault */
#else
#define XIP_FLASH_DATA_OFFSET     0x13040U   /* app_xip data: header 0x13000 + 0x40 */
#endif

extern void pm_start(void);
extern int  HAL_Flash_Init(uint32_t flash);
extern int  image_init(uint32_t flash, uint32_t addr, uint32_t max_size);
extern int  HAL_Xip_Init(uint32_t flash, uint32_t xaddr);
extern void platform_cache_init(void);

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
        printf("hokku: rollback COMMIT FAILED (set_cfg) — will roll back to seq %d\n",
               (g_boot_seq + 1) % IMAGE_SEQ_NUM);
        return;
    }
    HAL_WDG_Stop();
    g_rollback_armed = 0;
    printf("hokku: boot confirmed healthy; running seq %d, rollback disarmed\n",
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
    pm_start();
    HAL_Flash_Init(0);
    image_init(0, 0, 0);
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
    HAL_Xip_Init(0, XIP_FLASH_DATA_OFFSET);  /* sets read mode + BIAS_ADDR0 */
    platform_cache_init();
    /* Belt-and-suspenders: force the bias in case HAL_Xip_Init bailed early
     * (e.g. flash chip not recognized) before programming the register. */
    OPI_MEM_CTRL_BIAS_ADDR0 = XIP_BIAS_ENABLE | XIP_FLASH_DATA_OFFSET;
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
        printf("hokku: wlan_sta_config failed\n");
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
        printf("hokku: sysinfo unavailable — cannot persist WiFi\n");
        return -1;
    }
    if (slen == 0 || slen > SYSINFO_SSID_LEN_MAX || plen >= SYSINFO_PSK_LEN_MAX) {
        printf("hokku: bad ssid (%u) / psk (%u) length\n",
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
        printf("hokku: WARNING sysinfo_save failed — creds NOT persisted\n");
    else
        printf("hokku: WiFi creds saved to sysinfo (ssid '%s')\n", ssid);

    return hokku_wifi_connect(si->wlan_sta_param.ssid, si->wlan_sta_param.ssid_len,
                              si->wlan_sta_param.psk);
}

/* Connect using creds previously saved in sysinfo. No-op if none saved. */
static void hokku_wifi_connect_saved(void)
{
    struct sysinfo *si = sysinfo_get();

    if (si == NULL || si->wlan_sta_param.ssid_len == 0) {
        printf("hokku: no saved WiFi — provision once with: wifi <ssid> <password>\n");
        return;
    }
    printf("hokku: auto-connecting to saved SSID '%.*s'\n",
           si->wlan_sta_param.ssid_len, si->wlan_sta_param.ssid);
    hokku_wifi_connect(si->wlan_sta_param.ssid, si->wlan_sta_param.ssid_len,
                       si->wlan_sta_param.psk);
}

int main(void)
{
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

    printf("WiFi: 'wifi <ssid> <password>' to provision (persists across reboots)\n\n");

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
