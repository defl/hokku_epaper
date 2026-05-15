#pragma once
#include <stdint.h>
#include <stddef.h>

typedef uint32_t nvs_handle_t;
typedef enum { NVS_READONLY = 0, NVS_READWRITE = 1 } nvs_open_mode_t;

#define ESP_ERR_NVS_NO_FREE_PAGES    0x1100
#define ESP_ERR_NVS_NEW_VERSION_FOUND 0x1101

static inline int nvs_flash_init(void)  { return 0; }
static inline int nvs_flash_erase(void) { return 0; }
static inline int nvs_open(const char *ns, nvs_open_mode_t m, nvs_handle_t *h) {
    (void)ns; (void)m; (void)h; return -1; /* not found — caller treats as unconfigured */
}
static inline int nvs_get_u8(nvs_handle_t h, const char *k, uint8_t *v) {
    (void)h; (void)k; (void)v; return -1;
}
static inline int nvs_get_str(nvs_handle_t h, const char *k, char *v, size_t *l) {
    (void)h; (void)k; (void)v; (void)l; return -1;
}
static inline void nvs_close(nvs_handle_t h) { (void)h; }
