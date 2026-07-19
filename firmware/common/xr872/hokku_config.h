/*
 * Persistent app configuration for the Hokku Bigme F7 firmware.
 *
 * Stored as a single blob in a dedicated FDCM area (the same primitive sysinfo
 * uses) at a free flash sector ABOVE the OEM config partition, so it survives
 * both a slot-0 reflash and an A/B OTA (neither touches this region). WiFi creds
 * stay in sysinfo (see hokku_wifi_* in main.c); everything else lives here.
 */
#ifndef HOKKU_CONFIG_H
#define HOKKU_CONFIG_H

#include <stdint.h>

#define HOKKU_CFG_MAGIC     0x484B4347U   /* 'HKCG' */
/* v2: added power_mode (shifts struct layout) — bump so a v1 blob is rejected and
 * defaults load cleanly rather than being read misaligned. Always bump on a layout change. */
#define HOKKU_CFG_VERSION   2U

#define HOKKU_URL_MAX       128
#define HOKKU_NAME_MAX      64
#define HOKKU_IP_MAX        16            /* "255.255.255.255" + NUL */

/* power_mode: how the device idles between refreshes. */
#define HOKKU_PWR_AUTO      0            /* awake on USB, hibernate on battery (default) */
#define HOKKU_PWR_SLEEP     1            /* always deep-sleep between refreshes */
#define HOKKU_PWR_AWAKE     2            /* never sleep (always-on loop) */

typedef struct hokku_config {
    uint32_t magic;
    uint32_t version;
    char     server_url[HOKKU_URL_MAX];   /* full URL, e.g. http://host:port/hokku/screen/ */
    char     screen_name[HOKKU_NAME_MAX]; /* X-Screen-Name */
    uint8_t  use_dhcp;                     /* 0 = static IP below, 1 = DHCP */
    uint8_t  power_mode;                   /* HOKKU_PWR_* */
    char     ip[HOKKU_IP_MAX];             /* static IP / gateway / netmask */
    char     gw[HOKKU_IP_MAX];
    char     nm[HOKKU_IP_MAX];
    uint32_t default_sleep_s;              /* fallback sleep when no X-Sleep-Seconds */
} hokku_config_t;

/* Load config from flash (or compile-time defaults). Call once at boot. */
void            hokku_config_load(void);
/* Live, mutable config. */
hokku_config_t *hokku_config_get(void);
/* Persist the current config to flash. Returns 0 on success. */
int             hokku_config_save(void);

#endif /* HOKKU_CONFIG_H */
