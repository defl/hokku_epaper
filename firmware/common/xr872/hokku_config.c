#include "hokku_config.h"

#include <stdio.h>
#include <string.h>

#include "image/fdcm.h"

/*
 * Flash location for the config blob. 0x340000 is 64 KB-aligned and sits in the
 * large free region (0x332000-0x3ff000, all-0xFF on the OEM units) ABOVE the OEM
 * config partition. The slot-0 flasher (writes 0x8000.. + OTA cfg 0x180000) and
 * an A/B OTA (writes the inactive slot) both leave this untouched.
 */
#define HOKKU_CFG_FLASH   0
#define HOKKU_CFG_ADDR    0x340000U
#define HOKKU_CFG_SIZE    0x1000U

/*
 * Static address that early firmware wrote as its compiled-in default — the
 * developer's own LAN, never something a user chose. On any other network a unit
 * carrying it joins WiFi and then reaches nothing (issue #44), so a saved config
 * that still holds exactly this pair is treated as "never configured" and falls
 * back to DHCP. Keep in sync with LEGACY_DEFAULT_STATIC in
 * python/hokku/screens/bigme_f7/config.py.
 */
#define HOKKU_LEGACY_DEFAULT_IP  "192.168.6.199"
#define HOKKU_LEGACY_DEFAULT_GW  "192.168.6.254"

static hokku_config_t g_cfg;
static fdcm_handle_t *g_cfg_fdcm;

static void hokku_config_defaults(void)
{
    memset(&g_cfg, 0, sizeof(g_cfg));
    g_cfg.magic   = HOKKU_CFG_MAGIC;
    g_cfg.version = HOKKU_CFG_VERSION;
    /* mDNS name of the Hokku server/appliance (lwIP 2.x resolves .local). */
    strncpy(g_cfg.server_url, "http://hokku.local:8080/hokku/screen/", HOKKU_URL_MAX - 1);
    strncpy(g_cfg.screen_name, "bigme-f7", HOKKU_NAME_MAX - 1);
    /* DHCP by default; `cfg ip <ip> <gw> <nm>` opts into a static address. */
    g_cfg.use_dhcp = 1;
    /* AUTO: stay awake on USB, deep-sleep on battery. Verified on hardware
     * 2026-07-05 — PA20 USB-detect polarity correct (usb_present=1 on USB) and
     * the hibernation timer-wake cycle (180 s, WiFi-off-first) is clean. */
    g_cfg.power_mode = HOKKU_PWR_AUTO;
    strncpy(g_cfg.nm, "255.255.255.0", HOKKU_IP_MAX - 1);
    g_cfg.default_sleep_s = 300;
}

/* True for a config still carrying the legacy compiled-in static address. */
static int hokku_config_is_legacy_static(const hokku_config_t *c)
{
    return !c->use_dhcp &&
           strcmp(c->ip, HOKKU_LEGACY_DEFAULT_IP) == 0 &&
           strcmp(c->gw, HOKKU_LEGACY_DEFAULT_GW) == 0;
}

void hokku_config_load(void)
{
    g_cfg_fdcm = fdcm_open(HOKKU_CFG_FLASH, HOKKU_CFG_ADDR, HOKKU_CFG_SIZE);
    if (g_cfg_fdcm == NULL) {
        printf("hokku: cfg fdcm_open failed — using defaults\n");
        hokku_config_defaults();
        return;
    }
    if (fdcm_read(g_cfg_fdcm, &g_cfg, sizeof(g_cfg)) != sizeof(g_cfg) ||
        g_cfg.magic != HOKKU_CFG_MAGIC || g_cfg.version != HOKKU_CFG_VERSION) {
        printf("hokku: no valid saved config — using defaults\n");
        hokku_config_defaults();
    } else {
        printf("hokku: config loaded (name '%s' url '%s')\n",
               g_cfg.screen_name, g_cfg.server_url);
        if (hokku_config_is_legacy_static(&g_cfg)) {
            /* In memory only; the next `cfg save` persists it. */
            printf("hokku: legacy default static IP %s in config — using DHCP\n", g_cfg.ip);
            g_cfg.use_dhcp = 1;
        }
    }
}

hokku_config_t *hokku_config_get(void)
{
    return &g_cfg;
}

int hokku_config_save(void)
{
    if (g_cfg_fdcm == NULL) {
        g_cfg_fdcm = fdcm_open(HOKKU_CFG_FLASH, HOKKU_CFG_ADDR, HOKKU_CFG_SIZE);
        if (g_cfg_fdcm == NULL)
            return -1;
    }
    g_cfg.magic   = HOKKU_CFG_MAGIC;
    g_cfg.version = HOKKU_CFG_VERSION;
    if (fdcm_write(g_cfg_fdcm, &g_cfg, (uint16_t)sizeof(g_cfg)) != sizeof(g_cfg))
        return -1;
    return 0;
}
