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

static hokku_config_t g_cfg;
static fdcm_handle_t *g_cfg_fdcm;

static void hokku_config_defaults(void)
{
    memset(&g_cfg, 0, sizeof(g_cfg));
    g_cfg.magic   = HOKKU_CFG_MAGIC;
    g_cfg.version = HOKKU_CFG_VERSION;
    strncpy(g_cfg.server_url, "http://192.168.6.111:8080/hokku/screen/", HOKKU_URL_MAX - 1);
    strncpy(g_cfg.screen_name, "bigme-f7", HOKKU_NAME_MAX - 1);
    g_cfg.use_dhcp = 0;
    /* AUTO: stay awake on USB, deep-sleep on battery. Verified on hardware
     * 2026-07-05 — PA20 USB-detect polarity correct (usb_present=1 on USB) and
     * the hibernation timer-wake cycle (180 s, WiFi-off-first) is clean. */
    g_cfg.power_mode = HOKKU_PWR_AUTO;
    strncpy(g_cfg.ip, "192.168.6.199", HOKKU_IP_MAX - 1);
    strncpy(g_cfg.gw, "192.168.6.254", HOKKU_IP_MAX - 1);
    strncpy(g_cfg.nm, "255.255.255.0", HOKKU_IP_MAX - 1);
    g_cfg.default_sleep_s = 300;
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
