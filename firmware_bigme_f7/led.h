/*
 * LED driver for Bigme F7 (XR872AT)
 *
 * Two LED output pins identified by disassembly of original firmware
 * (01_boot_payload.bin, switch-case handler at 0x002FF0, init at 0x002FA0):
 *   PA12 = RED  LED output (active HIGH)
 *   PB3  = GREEN LED output (active HIGH)
 *
 * USB/charge detect pin (polled loop with CMP at 0x002CD0):
 *   PA20 = USB / charge detect input (HIGH = USB present — confirm polarity by test)
 *
 * Color assignment (red/green swapped in PA12/PB3 at compile time if hardware
 * turns out opposite; set LED_RED_IS_PA12 to 0 to swap):
 */

#ifndef LED_H
#define LED_H

void led_init(void);

/* Turn both LEDs off (default after init). */
void led_set_off(void);

/* Red = actively fetching image + driving EPD. */
void led_set_red(void);

/* Green = USB connected and battery fully charged. */
void led_set_green(void);

/* Returns 1 if USB is detected on PA20 (HIGH), 0 otherwise. */
int led_usb_present(void);

#endif /* LED_H */
