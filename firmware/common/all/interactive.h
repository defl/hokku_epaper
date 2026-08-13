/* USB-interactive mode: "a human is driving this screen over USB — stop
 * deciding things for yourself."
 *
 * Without it, host-driven work over the console is a race. A screen's normal job
 * is to wake on a schedule, fetch a picture and go back to sleep, and every one
 * of those steps takes the console away: the F7's console window closes when it
 * hibernates, and the ESP32 boards reboot for each refresh, which drops the USB
 * device entirely. A host can only poll harder and hope to land between them,
 * which makes the race less likely and never absent. Colour calibration needs
 * hundreds of consecutive uploads, so "unlikely" is not good enough.
 *
 * The fix is to let the host say so explicitly. While engaged, a screen does not
 * schedule refreshes, does not poll its server, and does not deep-sleep.
 *
 * Two properties make this safe to hand to a host:
 *
 * ANY RESET CLEARS IT. The flag lives in plain RAM, deliberately — not NVS, not
 * RTC-retained memory. A screen cannot be left permanently mute by a host that
 * crashed, wandered off, or forgot to turn it back off; power-cycling is always
 * the way out, and that is a thing anyone can do without a tool.
 *
 * IT ONLY ENGAGES ON USB. `hokku_interactive_engaged()` takes the caller's own
 * USB-present reading and ANDs it in. Suppressing sleep on battery would flatten
 * a screen that got unplugged while a host still had the mode set — the one
 * failure this feature could plausibly cause. On USB the panel is charging
 * anyway, which is what makes staying awake free.
 *
 * The request and the USB check are split because every board detects USB
 * differently (GPIO level here, an LED rail there) while the policy is identical
 * everywhere. Boards pass their own reading; the rule lives in one place.
 */
#pragma once

#include <stdbool.h>

/* Set or clear the request. Logged by the caller, not here — this stays free of
 * any logging dependency so it is compilable and testable on a host. */
void hokku_interactive_set(bool on);

/* The raw request, ignoring USB state. For status reporting only; gates must use
 * hokku_interactive_engaged() so they cannot forget the USB condition. */
bool hokku_interactive_requested(void);

/* True when the screen should suspend its own scheduling: the host asked AND a
 * USB host is actually present. This is the one to branch on. */
bool hokku_interactive_engaged(bool usb_present);
