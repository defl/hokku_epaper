/*
 * LED driver for Bigme F7 (XR872AT)
 *
 * PA12 = RED  LED (active HIGH)
 * PB3  = GREEN LED (active HIGH)
 * PA20 = USB/charge detect input (HIGH = USB present; confirm polarity on first flash)
 *
 * Pin identities derived from disassembly of original firmware boot partition
 * (01_boot_payload.bin). The 8-case switch at 0x002FF0 operates exclusively on
 * PA12 and PB3. Init at 0x002FA0 configures both as outputs and drives them LOW.
 * PA20 is read in a polled loop at 0x002CD0 with a compare-to-zero branch.
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
    p.driving = GPIO_DRIVING_LEVEL_2;
    p.pull    = GPIO_PULL_NONE;
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
    return HAL_GPIO_ReadPin(LED_USB_PORT, LED_USB_PIN) == GPIO_PIN_HIGH;
}
