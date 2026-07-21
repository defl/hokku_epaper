#pragma once
#include <stdint.h>

#define IMAGE_SEQ_NUM (2)
typedef uint8_t image_seq_t;

typedef enum { IMAGE_STATE_UNVERIFIED = 0, IMAGE_STATE_VERIFIED = 1, IMAGE_STATE_UNDEFINED = 2 } image_state_t;
typedef struct image_cfg { image_seq_t seq; image_state_t state; } image_cfg_t;
typedef enum { IMAGE_INVALID = 0, IMAGE_VALID = 1 } image_val_t;

typedef struct image_ota_param {
    uint32_t    ota_flash : 8;
    uint32_t    ota_size  : 24;
    uint32_t    ota_addr;
    uint16_t    img_max_size;
    uint16_t    img_xz_max_size;
    uint32_t    bl_size;
    image_seq_t running_seq;
    uint8_t     flash[IMAGE_SEQ_NUM];
    uint32_t    addr[IMAGE_SEQ_NUM];
} image_ota_param_t;

#define IMAGE_AREA_SIZE(size) ((size) * 1024)

/* ── Controllable mock state ──────────────────────────────────────────── */
static image_seq_t         _mock_image_running_seq;
static image_val_t         _mock_image_check_sections_result;
static int                 _mock_image_set_cfg_result;   /* 0 = success, -1 = failure */
static image_cfg_t         _mock_image_set_cfg_last;      /* what was last passed in */
static int                 _mock_image_set_cfg_call_count;
static const image_ota_param_t *_mock_image_ota_param;   /* NULL to test the !iop guard */

static inline int image_init(uint32_t flash, uint32_t addr, uint32_t max_size)
{ (void)flash; (void)addr; (void)max_size; return 0; }

static inline image_seq_t image_get_running_seq(void) { return _mock_image_running_seq; }

static inline image_val_t image_check_sections(image_seq_t seq)
{ (void)seq; return _mock_image_check_sections_result; }

static inline int image_set_cfg(image_cfg_t *cfg)
{
    _mock_image_set_cfg_last = *cfg;
    _mock_image_set_cfg_call_count++;
    return _mock_image_set_cfg_result;
}

static inline const image_ota_param_t *image_get_ota_param(void) { return _mock_image_ota_param; }
