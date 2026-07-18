#pragma once
#include <stdint.h>
#include <stdlib.h>

#define MALLOC_CAP_SPIRAM   (1u << 3)
#define MALLOC_CAP_DEFAULT  (1u << 12)

static inline void *heap_caps_malloc(size_t size, uint32_t caps) {
    (void)caps; return malloc(size);
}
static inline void heap_caps_free(void *ptr) { free(ptr); }
static inline size_t esp_get_free_heap_size(void) { return 256 * 1024; }
