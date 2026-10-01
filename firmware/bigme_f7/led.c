/*
 * LED driver for Bigme F7 (XR872AT)
 *
 * PA12 = RED  LED (active HIGH)
 * PB3  = GREEN LED (active HIGH)
 * PA20 = USB/charge detect input (ACTIVE-LOW: LOW = USB-C present; confirmed on bench)
 *
 * Pin identities derived from disassembly of original firmware boot partition
 * (01_boot_payload.bin, SRAM base 0x00201000). The OEM registers an SDK
 * power-management device `dev_leds`/`drv_leds` whose hooks are:
 *   0x00203FA0 leds_resume  — HAL_GPIO_Init(PB3 / PA12, OUTPUT, drv 1, PULL_DOWN)
 *                             then WritePin LOW on both
 *   0x00203FD2 leds_suspend — re-init BOTH as INPUT + PULL_DOWN before sleep
 *   0x00203FF0 leds_set(state) — 8-case tbb switch touching only PA12 and PB3
 * PA20 is read in a polled loop at 0x002CD0 with a compare-to-zero branch. (The
 * OEM's own charge status comes off PA6 + PB2 instead, both INPUT + PULL_UP —
 * we keep PA20, which is bench-verified for USB-present on this board.)
 *
 * PB3 must be claimed AFTER the flash controller comes up: the stock
 * `xr872_evb_ai` board config maps PB3 to GPIOB_P3_F5_FLASH_HOLD (pull-up,
 * driving level 3) whenever BOARD_SWD_EN == 0, and HAL_Flashc_Xip_Enable()
 * applies that pinmux at XIP enable, leaving the pin driven HIGH. The Bigme
 * board does not wire PB3 to the flash (the OEM drives it as an LED and the
 * flash runs FLASH_READ_DUAL_O_MODE, which never uses HOLD#), so led_init()
 * takes the pin back.
 *
 * That was the suspected cause of the always-on green LED, but it is NOT
 * confirmed: claiming the pins here changed nothing observable on hardware —
 * the green LED is lit for the whole awake window and dark in hibernation, both
 * before and after. Either PB3 is not the green LED, or it is not active HIGH,
 * or the LED is not MCU-driven. See docs/screens/bigme_f7/hardware_facts.md.
 */

#include "led.h"
#include "driver/chip/hal_gpio.h"

#define LED_RED_PORT    GPIO_PORT_A
#define LED_RED_PIN     GPIO_PIN_12

#define LED_GREEN_PORT  GPIO_PORT_B
#define LED_GREEN_PIN   GPIO_PIN_3

#define LED_USB_PORT    GPIO_PORT_A
#define LED_USB_PIN     GPIO_PIN_20

static void led_out(GPIO_Port port, GPIO_Pin pin)
{
    GPIO_InitParam p;
    p.mode    = GPIOx_Pn_F1_OUTPUT;
    p.driving = GPIO_DRIVING_LEVEL_1;
    p.pull    = GPIO_PULL_DOWN;     /* as the OEM: holds the LED dark, never lit */
    HAL_GPIO_Init(port, pin, &p);
}

static void led_in_pulldown(GPIO_Port port, GPIO_Pin pin)
{
    GPIO_InitParam p;
    p.mode    = GPIOx_Pn_F0_INPUT;
    p.driving = GPIO_DRIVING_LEVEL_1;
    p.pull    = GPIO_PULL_DOWN;
    HAL_GPIO_Init(port, pin, &p);
}

static void led_in_pullup(GPIO_Port port, GPIO_Pin pin)
{
    GPIO_InitParam p;
    p.mode    = GPIOx_Pn_F0_INPUT;
    p.driving = GPIO_DRIVING_LEVEL_1;
    p.pull    = GPIO_PULL_UP;
    HAL_GPIO_Init(port, pin, &p);
}

void led_init(void)
{
    led_out(LED_RED_PORT,   LED_RED_PIN);
    led_out(LED_GREEN_PORT, LED_GREEN_PIN);
    led_in_pullup(LED_USB_PORT, LED_USB_PIN);

    HAL_GPIO_WritePin(LED_RED_PORT,   LED_RED_PIN,   GPIO_PIN_LOW);
    HAL_GPIO_WritePin(LED_GREEN_PORT, LED_GREEN_PIN, GPIO_PIN_LOW);
}

void led_park_for_sleep(void)
{
    /* Mirrors the OEM's leds_suspend (0x00203FD2): both LED pins to input with a
     * pull-down, so nothing drives them while the chip is in hibernation. */
    led_in_pulldown(LED_RED_PORT,   LED_RED_PIN);
    led_in_pulldown(LED_GREEN_PORT, LED_GREEN_PIN);
}

void led_set_off(void)
{
    HAL_GPIO_WritePin(LED_RED_PORT,   LED_RED_PIN,   GPIO_PIN_LOW);
    HAL_GPIO_WritePin(LED_GREEN_PORT, LED_GREEN_PIN, GPIO_PIN_LOW);
}

void led_set_red(void)
{
    HAL_GPIO_WritePin(LED_GREEN_PORT, LED_GREEN_PIN, GPIO_PIN_LOW);
    HAL_GPIO_WritePin(LED_RED_PORT,   LED_RED_PIN,   GPIO_PIN_HIGH);
}

void led_set_green(void)
{
    HAL_GPIO_WritePin(LED_RED_PORT,   LED_RED_PIN,   GPIO_PIN_LOW);
    HAL_GPIO_WritePin(LED_GREEN_PORT, LED_GREEN_PIN, GPIO_PIN_HIGH);
}

int led_usb_present(void)
{
    /* PA20 is ACTIVE-LOW: it reads LOW when USB-C is connected, so USB-present is
     * (pin == LOW). Verified on the bench 2026-07-05: `cfg show` reports
     * usb_present=1 on USB-C. The input pull-up biases the line HIGH (= no USB)
     * when it floats on battery. */
    return HAL_GPIO_ReadPin(LED_USB_PORT, LED_USB_PIN) == GPIO_PIN_LOW;
}
