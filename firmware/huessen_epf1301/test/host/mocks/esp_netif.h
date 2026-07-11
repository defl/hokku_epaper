#pragma once
#include <stdint.h>

typedef void *esp_netif_t;

typedef struct { uint32_t addr; } esp_ip4_addr_t;
typedef struct { esp_ip4_addr_t ip, netmask, gw; } esp_netif_ip_info_t;
typedef struct { esp_netif_ip_info_t ip_info; } ip_event_got_ip_t;

#define IPSTR     "%d.%d.%d.%d"
#define IP2STR(a) ((int)(((a)->addr) & 0xFF)), ((int)((((a)->addr) >> 8) & 0xFF)), \
                  ((int)((((a)->addr) >> 16) & 0xFF)), ((int)((((a)->addr) >> 24) & 0xFF))

static inline int  esp_netif_init(void) { return 0; }
static inline esp_netif_t *esp_netif_create_default_wifi_sta(void) { return NULL; }
static inline int  esp_netif_get_ip_info(esp_netif_t *n, esp_netif_ip_info_t *i) {
    (void)n; (void)i; return 0;
}
