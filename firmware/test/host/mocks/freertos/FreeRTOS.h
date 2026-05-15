#pragma once
#include <stdint.h>
#include <stdbool.h>

/* ── ESP-IDF base types (needed throughout, defined here as the first include) ── */
typedef int32_t esp_err_t;
#define ESP_OK    ((esp_err_t)0)
#define ESP_FAIL  ((esp_err_t)-1)
#define ESP_ERR_INVALID_ARG    ((esp_err_t)0x102)
#define ESP_ERR_INVALID_STATE  ((esp_err_t)0x103)
#define ESP_ERR_NOT_FOUND      ((esp_err_t)0x105)
#define ESP_ERR_TIMEOUT        ((esp_err_t)0x107)

static inline const char *esp_err_to_name(esp_err_t e) { (void)e; return "ERR"; }

#define ESP_ERROR_CHECK(x) do { (void)(x); } while(0)

/* RTC attributes — no-op on host. */
#define RTC_NOINIT_ATTR
#define RTC_DATA_ATTR

static inline void esp_restart(void) {}

/* POSIX strcasecmp — not available on MSVC, provide a shim. */
#ifdef _MSC_VER
#include <string.h>
static inline int strcasecmp(const char *a, const char *b)  { return _stricmp(a, b); }
static inline int strncasecmp(const char *a, const char *b, size_t n) { return _strnicmp(a, b, n); }
#endif

/* ── FreeRTOS base types ─────────────────────────────────────────────── */
typedef int      BaseType_t;
typedef uint32_t UBaseType_t;
typedef uint32_t TickType_t;

#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
#define pdTRUE  ((BaseType_t)1)
#define pdFALSE ((BaseType_t)0)
#define portMAX_DELAY ((TickType_t)0xFFFFFFFFU)
#define BIT0 (1u << 0)
#define BIT1 (1u << 1)

#define configASSERT(x) ((void)(x))
