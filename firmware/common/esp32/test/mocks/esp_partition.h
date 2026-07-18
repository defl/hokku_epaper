#pragma once
/* Minimal stub of esp_partition.h for host-side unit tests. */

#include <stddef.h>
#include <stdint.h>

typedef enum {
    ESP_PARTITION_TYPE_APP  = 0x00,
    ESP_PARTITION_TYPE_DATA = 0x01,
} esp_partition_type_t;

typedef enum {
    ESP_PARTITION_SUBTYPE_DATA_NVS        = 0x02,
    ESP_PARTITION_SUBTYPE_DATA_OTA        = 0x00,
    ESP_PARTITION_SUBTYPE_APP_OTA_0       = 0x10,
    ESP_PARTITION_SUBTYPE_APP_OTA_1       = 0x11,
    ESP_PARTITION_SUBTYPE_ANY             = 0xFF,
} esp_partition_subtype_t;

typedef struct {
    esp_partition_type_t    type;
    esp_partition_subtype_t subtype;
    uint32_t                address;
    uint32_t                size;
    char                    label[17];
    bool                    encrypted;
} esp_partition_t;

#ifndef ESP_OK
typedef int esp_err_t;
#define ESP_OK  ((esp_err_t)0)
#define ESP_FAIL ((esp_err_t)-1)
#endif

static inline const esp_partition_t *esp_partition_find_first(
        esp_partition_type_t type, esp_partition_subtype_t subtype, const char *label) {
    (void)type; (void)subtype; (void)label; return NULL;
}
static inline esp_err_t esp_partition_erase_range(const esp_partition_t *p, size_t offset, size_t size) {
    (void)p; (void)offset; (void)size; return ESP_OK;
}
static inline esp_err_t esp_partition_write(const esp_partition_t *p, size_t offset, const void *src, size_t size) {
    (void)p; (void)offset; (void)src; (void)size; return ESP_OK;
}
static inline esp_err_t esp_partition_read(const esp_partition_t *p, size_t offset, void *dst, size_t size) {
    (void)p; (void)offset; (void)dst; (void)size; return ESP_OK;
}
