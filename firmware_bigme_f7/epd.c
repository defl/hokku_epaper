/*
 * EK79655 EPD driver for Bigme F7 (XR872AT)
 *
 * Pin assignments confirmed by exhaustive disassembly of original firmware
 * (01_boot_payload.bin, functions EpaperIO_Init/epd_step1/RST_pulse/EPD_step2):
 *
 *   PA8  = static LOW output  (purpose unknown, never toggled)
 *   PA9  = BUSY               (input, pull-up, HIGH = ready)
 *   PA13 = RST                (output, active low — double pulse on init)
 *   PA15 = static LOW output  (purpose unknown, never toggled)
 *   PA16 = static HIGH output (purpose unknown, never toggled)
 *   PA19 = MOSI/DC            (output, 9-bit SPI: first bit = D/C flag)
 *   PA21 = SCLK               (output, idle LOW)
 *   PA22 = CS                 (output, active low)
 *   PB17 = POWER_EN           (output, active high — on during entire EPD session)
 *
 * RST double-pulse sequence (from RST_pulse fn at 0x002071E8 in original firmware):
 *   PA13=LOW → 100ms → PA13=HIGH → 100ms → PA13=LOW → 100ms → PA13=HIGH
 *
 * 9-bit SPI: CS low per byte, first bit = D/C, then 8 data bits MSB first,
 * SCLK idles LOW, data sampled on rising edge.
 */

#include "epd.h"
#include "driver/chip/hal_gpio.h"
#include "kernel/os/os.h"

/* Port A pins */
#define EPD_PIN_PA8     GPIO_PIN_8
#define EPD_PIN_BUSY    GPIO_PIN_9
#define EPD_PIN_RST     GPIO_PIN_13
#define EPD_PIN_PA15    GPIO_PIN_15
#define EPD_PIN_PA16    GPIO_PIN_16
#define EPD_PIN_MOSI    GPIO_PIN_19
#define EPD_PIN_CLK     GPIO_PIN_21
#define EPD_PIN_CS      GPIO_PIN_22

/* Port B pins */
#define EPD_PIN_POWER   GPIO_PIN_17  /* PB17 */

/* -------------------------------------------------------------------------
 * Low-level GPIO helpers
 * ---------------------------------------------------------------------- */

static inline void pa_set(GPIO_Pin pin, uint8_t high)
{
    HAL_GPIO_WritePin(GPIO_PORT_A, pin, high ? GPIO_PIN_HIGH : GPIO_PIN_LOW);
}

static inline uint8_t pa_get(GPIO_Pin pin)
{
    return (uint8_t)HAL_GPIO_ReadPin(GPIO_PORT_A, pin);
}

static void pa_out(GPIO_Pin pin)
{
    GPIO_InitParam p;
    p.mode    = GPIOx_Pn_F1_OUTPUT;
    p.driving = GPIO_DRIVING_LEVEL_1;
    p.pull    = GPIO_PULL_NONE;
    HAL_GPIO_Init(GPIO_PORT_A, pin, &p);
}

static void pa_in_pullup(GPIO_Pin pin)
{
    GPIO_InitParam p;
    p.mode    = GPIOx_Pn_F0_INPUT;
    p.driving = GPIO_DRIVING_LEVEL_1;
    p.pull    = GPIO_PULL_UP;
    HAL_GPIO_Init(GPIO_PORT_A, pin, &p);
}

static void pb17_out(void)
{
    GPIO_InitParam p;
    p.mode    = GPIOx_Pn_F1_OUTPUT;
    p.driving = GPIO_DRIVING_LEVEL_3;  /* max drive, matches original firmware */
    p.pull    = GPIO_PULL_UP;
    HAL_GPIO_Init(GPIO_PORT_B, EPD_PIN_POWER, &p);
}

/* -------------------------------------------------------------------------
 * SPI primitives
 * ---------------------------------------------------------------------- */

static inline void clk_pulse(void)
{
    pa_set(EPD_PIN_CLK, 1);
    pa_set(EPD_PIN_CLK, 0);
}

void epd_wait_busy(void)
{
    while (!pa_get(EPD_PIN_BUSY))
        ;
}

void epd_send_cmd(uint8_t cmd)
{
    pa_set(EPD_PIN_CS, 0);
    pa_set(EPD_PIN_MOSI, 0);    /* D/C = 0 → command */
    clk_pulse();
    for (int i = 7; i >= 0; i--) {
        pa_set(EPD_PIN_MOSI, (cmd >> i) & 1);
        clk_pulse();
    }
    pa_set(EPD_PIN_MOSI, 0);
    pa_set(EPD_PIN_CS, 1);
}

void epd_send_data(uint8_t data)
{
    pa_set(EPD_PIN_CS, 0);
    pa_set(EPD_PIN_MOSI, 1);    /* D/C = 1 → data */
    clk_pulse();
    for (int i = 7; i >= 0; i--) {
        pa_set(EPD_PIN_MOSI, (data >> i) & 1);
        clk_pulse();
    }
    pa_set(EPD_PIN_MOSI, 0);
    pa_set(EPD_PIN_CS, 1);
}

/* -------------------------------------------------------------------------
 * Hardware init — matches EpaperIO_Init + epd_step1 in original firmware
 * ---------------------------------------------------------------------- */

static void epd_hw_init(void)
{
    /* Configure all pins (order matches EpaperIO_Init at 0x00206F94) */
    pa_out(EPD_PIN_PA8);
    pa_out(EPD_PIN_RST);
    pa_out(EPD_PIN_CS);
    pa_out(EPD_PIN_CLK);
    pa_out(EPD_PIN_MOSI);
    pa_out(EPD_PIN_PA16);
    pa_out(EPD_PIN_PA15);
    pa_in_pullup(EPD_PIN_BUSY);
    pb17_out();

    /* Set initial states (matches epd_step1 at 0x00207024) */
    pa_set(EPD_PIN_CLK,  0);    /* SCLK idle LOW */
    pa_set(EPD_PIN_MOSI, 1);    /* MOSI HIGH */
    pa_set(EPD_PIN_CS,   1);    /* CS deasserted */
    pa_set(EPD_PIN_PA16, 1);    /* static HIGH */
    pa_set(EPD_PIN_PA15, 0);    /* static LOW */
    pa_set(EPD_PIN_PA8,  0);    /* static LOW */
    pa_set(EPD_PIN_RST,  1);    /* RST deasserted */
    HAL_GPIO_WritePin(GPIO_PORT_B, EPD_PIN_POWER, GPIO_PIN_HIGH);  /* power on */
}

/* -------------------------------------------------------------------------
 * RST pulse — matches RST_pulse fn at 0x002071E8 in original firmware
 *
 * Double low-pulse: LOW→100ms→HIGH→100ms→LOW→100ms→HIGH
 * Called at the very start of EPD_step2, before wait_busy.
 * ---------------------------------------------------------------------- */

static void epd_rst_pulse(void)
{
    pa_set(EPD_PIN_RST, 0);  OS_MSleep(100);
    pa_set(EPD_PIN_RST, 1);  OS_MSleep(100);
    pa_set(EPD_PIN_RST, 0);  OS_MSleep(100);
    pa_set(EPD_PIN_RST, 1);
    /* no trailing delay — original firmware goes straight to wait_busy */
}

/* -------------------------------------------------------------------------
 * EK79655 init sequence — byte-for-byte match with EPD_step2 at 0x00207228
 * ---------------------------------------------------------------------- */

static void epd_run_init_sequence(void)
{
    epd_rst_pulse();    /* must come first, before wait_busy */
    epd_wait_busy();

    epd_send_cmd(0xAA);
    epd_send_data(0x49); epd_send_data(0x55); epd_send_data(0x20);
    epd_send_data(0x08); epd_send_data(0x09); epd_send_data(0x18);

    epd_send_cmd(0x01);
    epd_send_data(0x3F);

    epd_send_cmd(0x00);
    epd_send_data(0x5F); epd_send_data(0x69);

    epd_send_cmd(0x05);
    epd_send_data(0x40); epd_send_data(0x1F);
    epd_send_data(0x1F); epd_send_data(0x2C);

    epd_send_cmd(0x08);
    epd_send_data(0x6F); epd_send_data(0x1F);
    epd_send_data(0x1F); epd_send_data(0x22);

    epd_send_cmd(0x06);
    epd_send_data(0x6F); epd_send_data(0x1F);
    epd_send_data(0x17); epd_send_data(0x17);

    epd_send_cmd(0x03);
    epd_send_data(0x00); epd_send_data(0x54);
    epd_send_data(0x00); epd_send_data(0x44);

    epd_send_cmd(0x60);
    epd_send_data(0x02); epd_send_data(0x00);

    epd_send_cmd(0x30);
    epd_send_data(0x08);

    epd_send_cmd(0x50);
    epd_send_data(0x3F);

    epd_send_cmd(0x61);
    epd_send_data(0x03); epd_send_data(0x20);
    epd_send_data(0x01); epd_send_data(0xE0);

    epd_send_cmd(0xE3);
    epd_send_data(0x2F);

    epd_send_cmd(0x84);
    epd_send_data(0x01);
}

/* -------------------------------------------------------------------------
 * Public API
 * ---------------------------------------------------------------------- */

void epd_init(void)
{
    epd_hw_init();
    epd_run_init_sequence();
}

/*
 * Complete display refresh. Call after streaming 192000 bytes via
 * epd_send_cmd(0x10) + epd_send_data() × 192000.
 *
 * Sequence from send_image_data fn at 0x0020736C in original firmware:
 *   PON → wait_busy → BTST(pre-refresh) → DRF → wait_busy(~30s) → POF → wait_busy
 */
void epd_refresh(void)
{
    epd_send_cmd(0x04);                              /* PON: power on */
    epd_wait_busy();

    epd_send_cmd(0x06);                              /* BTST: pre-refresh booster */
    epd_send_data(0x6F); epd_send_data(0x1F);
    epd_send_data(0x17); epd_send_data(0x49);

    epd_send_cmd(0x12);                              /* DRF: display refresh */
    epd_send_data(0x00);
    epd_wait_busy();                                 /* ~20-30 s */

    epd_send_cmd(0x02);                              /* POF: power off */
    epd_send_data(0x00);
    epd_wait_busy();
}
