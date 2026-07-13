#pragma once
#include <stdint.h>

typedef enum { OTA_STATUS_OK = 0, OTA_STATUS_ERROR = -1 } ota_status_t;
typedef enum { OTA_PROTOCOL_HTTP = 1 } ota_protocol_t;
typedef enum { OTA_VERIFY_NONE = 0 } ota_verify_t;

/* ── Controllable mock state ──────────────────────────────────────────── */
static int _mock_ota_init_called;
static int _mock_ota_get_image_called;
static int _mock_ota_verify_image_called;
static int _mock_ota_reboot_called;
static ota_status_t _mock_ota_init_result;
static ota_status_t _mock_ota_get_image_result;
static ota_status_t _mock_ota_verify_image_result;

static inline ota_status_t ota_init(void) { _mock_ota_init_called++; return _mock_ota_init_result; }
static inline ota_status_t ota_get_image(ota_protocol_t proto, void *url)
{ (void)proto; (void)url; _mock_ota_get_image_called++; return _mock_ota_get_image_result; }
static inline ota_status_t ota_verify_image(ota_verify_t v, uint32_t *value)
{ (void)v; (void)value; _mock_ota_verify_image_called++; return _mock_ota_verify_image_result; }
static inline void ota_reboot(void) { _mock_ota_reboot_called++; }
