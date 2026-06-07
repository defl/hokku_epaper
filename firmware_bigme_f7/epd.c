/*
 * EK79655 EPD driver for Bigme F7 (XR872AT)
 *
 * 9-bit bit-bang SPI: first bit = D/C flag, next 8 bits = data, MSB first.
 * GPIO mapping confirmed from disassembly:
 *   PA9  = BUSY  (input,  HIGH = ready)
 *   PA19 = MOSI/DC (output, combined)
 *   PA21 = SCLK  (output, idle low)
 *   PA22 = CS    (output, active low)
 *
 * Init sequence and refresh sequence are byte-for-byte matches with
 * Waveshare EPD_7in3f / EK79655 open-source driver.
 */

#include "epd.h"
#include "driver/chip/hal_gpio.h"
#include "kernel/os/os.h"

#define EPD_PORT        GPIO_PORT_A
#define EPD_PIN_BUSY    GPIO_PIN_9
#define EPD_PIN_MOSI    GPIO_PIN_19
#define EPD_PIN_CLK     GPIO_PIN_21
#define EPD_PIN_CS      GPIO_PIN_22

static inline void pin_set(GPIO_Pin pin, uint8_t high)
{
    HAL_GPIO_WritePin(EPD_PORT, pin, high ? GPIO_PIN_HIGH : GPIO_PIN_LOW);
}

static inline uint8_t pin_get(GPIO_Pin pin)
{
    return (uint8_t)HAL_GPIO_ReadPin(EPD_PORT, pin);
}

/* One clock pulse: MOSI must already be set before calling */
static inline void clk_pulse(void)
{
    pin_set(EPD_PIN_CLK, 1);
    pin_set(EPD_PIN_CLK, 0);
}

void epd_wait_busy(void)
{
    while (!pin_get(EPD_PIN_BUSY))
        ;
}

void epd_send_cmd(uint8_t cmd)
{
    pin_set(EPD_PIN_CS, 0);
    pin_set(EPD_PIN_MOSI, 0);   /* D/C = 0 (command) */
    clk_pulse();
    for (int i = 7; i >= 0; i--) {
        pin_set(EPD_PIN_MOSI, (cmd >> i) & 1);
        clk_pulse();
    }
    pin_set(EPD_PIN_MOSI, 0);
    pin_set(EPD_PIN_CS, 1);
}

void epd_send_data(uint8_t data)
{
    pin_set(EPD_PIN_CS, 0);
    pin_set(EPD_PIN_MOSI, 1);   /* D/C = 1 (data) */
    clk_pulse();
    for (int i = 7; i >= 0; i--) {
        pin_set(EPD_PIN_MOSI, (data >> i) & 1);
        clk_pulse();
    }
    pin_set(EPD_PIN_MOSI, 0);
    pin_set(EPD_PIN_CS, 1);
}

static void gpio_out(GPIO_Pin pin)
{
    GPIO_InitParam p;
    p.driving = GPIO_DRIVING_LEVEL_1;
    p.mode    = GPIOx_Pn_F1_OUTPUT;
    p.pull    = GPIO_PULL_NONE;
    HAL_GPIO_Init(EPD_PORT, pin, &p);
}

static void gpio_in(GPIO_Pin pin)
{
    GPIO_InitParam p;
    p.driving = GPIO_DRIVING_LEVEL_1;
    p.mode    = GPIOx_Pn_F0_INPUT;
    p.pull    = GPIO_PULL_NONE;
    HAL_GPIO_Init(EPD_PORT, pin, &p);
}

static void epd_hw_init(void)
{
    gpio_out(EPD_PIN_CS);
    gpio_out(EPD_PIN_CLK);
    gpio_out(EPD_PIN_MOSI);
    gpio_in(EPD_PIN_BUSY);

    pin_set(EPD_PIN_CS, 1);
    pin_set(EPD_PIN_CLK, 0);
    pin_set(EPD_PIN_MOSI, 0);
}

/*
 * Init sequence: byte-for-byte match with Waveshare EPD_7in3f
 * (verified against disassembly of original Bigme F7 boot partition).
 */
static void epd_run_init_sequence(void)
{
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

void epd_init(void)
{
    epd_hw_init();
    epd_run_init_sequence();
}

/*
 * Complete display refresh after 192000 data bytes have been clocked in via
 * CMD 0x10 + epd_send_data() calls.
 *
 * PON → wait → BTST(pre-refresh) → DRF → wait ~30s → POF → wait
 */
void epd_refresh(void)
{
    epd_send_cmd(0x04);                             /* PON: power on */
    epd_wait_busy();

    epd_send_cmd(0x06);                             /* BTST: pre-refresh booster */
    epd_send_data(0x6F); epd_send_data(0x1F);
    epd_send_data(0x17); epd_send_data(0x49);

    epd_send_cmd(0x12);                             /* DRF: display refresh */
    epd_send_data(0x00);
    epd_wait_busy();                                /* ~20-30 s */

    epd_send_cmd(0x02);                             /* POF: power off */
    epd_send_data(0x00);
    epd_wait_busy();
}
