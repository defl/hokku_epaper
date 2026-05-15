#pragma once
#include <stdint.h>

typedef enum {
    GPIO_MODE_INPUT         = 1,
    GPIO_MODE_OUTPUT        = 2,
    GPIO_MODE_INPUT_OUTPUT  = 3,
    GPIO_MODE_OUTPUT_OD     = 4,
} gpio_mode_t;

typedef enum { GPIO_PULLUP_DISABLE = 0, GPIO_PULLUP_ENABLE = 1 }   gpio_pullup_t;
typedef enum { GPIO_PULLDOWN_DISABLE = 0, GPIO_PULLDOWN_ENABLE = 1 } gpio_pulldown_t;
typedef enum { GPIO_INTR_DISABLE = 0, GPIO_INTR_ANYEDGE = 3 }      gpio_int_type_t;
typedef enum { GPIO_DRIVE_CAP_DEFAULT = 2 }                         gpio_drive_cap_t;
typedef int gpio_num_t;
typedef int gpio_pull_mode_t;

typedef struct {
    uint64_t       pin_bit_mask;
    gpio_mode_t    mode;
    gpio_pullup_t  pull_up_en;
    gpio_pulldown_t pull_down_en;
    gpio_int_type_t intr_type;
} gpio_config_t;

/* Controllable mock state: indexed by GPIO pin number. */
static int _mock_gpio[50];

static inline int  gpio_get_level(gpio_num_t pin)              { return _mock_gpio[(int)pin]; }
static inline int  gpio_set_level(gpio_num_t pin, int level)   { _mock_gpio[(int)pin] = level; return 0; }
static inline int  gpio_config(const gpio_config_t *c)         { (void)c; return 0; }
static inline int  gpio_reset_pin(gpio_num_t pin)              { (void)pin; return 0; }
static inline int  gpio_set_direction(gpio_num_t pin, gpio_mode_t m) { (void)pin; (void)m; return 0; }
static inline int  gpio_set_pull_mode(gpio_num_t pin, gpio_pull_mode_t m) { (void)pin; (void)m; return 0; }
static inline int  gpio_pullup_en(gpio_num_t pin)              { (void)pin; return 0; }
static inline int  gpio_pulldown_en(gpio_num_t pin)            { (void)pin; return 0; }
static inline int  gpio_set_drive_capability(gpio_num_t pin, gpio_drive_cap_t s) { (void)pin; (void)s; return 0; }
