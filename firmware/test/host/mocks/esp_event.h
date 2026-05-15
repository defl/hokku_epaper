#pragma once
#include <stdint.h>

typedef const char *esp_event_base_t;
typedef void       *esp_event_handler_instance_t;
typedef void (*esp_event_handler_t)(void *, esp_event_base_t, int32_t, void *);

#define ESP_EVENT_ANY_ID (-1)

/* Event bases — defined as string literals so pointer comparisons work. */
static const char *WIFI_EVENT = "WIFI_EVENT";
static const char *IP_EVENT   = "IP_EVENT";

#define WIFI_EVENT_STA_START        0
#define WIFI_EVENT_STA_CONNECTED    1
#define WIFI_EVENT_STA_DISCONNECTED 2
#define IP_EVENT_STA_GOT_IP         0

static inline int esp_event_loop_create_default(void) { return 0; }
static inline int esp_event_handler_instance_register(esp_event_base_t b, int32_t id,
                                                       esp_event_handler_t h, void *a,
                                                       esp_event_handler_instance_t *i) {
    (void)b; (void)id; (void)h; (void)a; (void)i; return 0;
}
