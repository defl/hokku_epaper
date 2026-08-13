/* Serial console over the ESP32-S3's built-in USB Serial/JTAG.
 *
 * The Huessen firmware had no command surface at all: it woke, fetched a picture
 * over HTTP and slept. That is the wrong tool for colour measurement, which needs
 * an exact, known raster on the glass on demand — no network, no render pipeline,
 * no server config in the loop. This adds the smallest console that makes the
 * shared `frame` upload protocol reachable.
 *
 * It runs ONLY in the USB_AWAKE regime. On battery the device is asleep or racing
 * back to sleep, and a reader task there would be a wakeup source that buys
 * nothing — calibration only ever happens on USB.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Start the console reader task. Idempotent: USB_AWAKE can be entered more than
 * once per boot (battery window -> USB plugged), and starting a second reader on
 * the same peripheral would give two tasks racing for the same bytes. */
void hokku_console_start(void);

/* True while a frame upload is in flight.
 *
 * The regime loops MUST consult this before acting on a scheduled refresh or a
 * button press. Both call esp_restart(), and a restart partway through a 960 KB
 * transfer would drop the host mid-stream and reboot into a half-painted panel —
 * during a measurement run whose whole point is knowing exactly what is on the
 * glass. */
bool hokku_console_busy(void);

/* Receive one frame and display it. Implemented in main.c, where the board's
 * image size and panel driver live; declared here so console.c can dispatch to
 * it without main.c having to know about the console's internals.
 *
 * Returns 0 on success, non-zero if the transfer failed (host vanished, CRC
 * mismatch, or no buffer). On failure NOTHING is displayed — showing a subtly
 * corrupt picture during colour measurement is worse than showing nothing. */
int hokku_frame_receive(void);

/* ── Transport, for the frame receiver in main.c ──────────────────────────── */

/* Read exactly `len` bytes, or fewer if `timeout_ms` expires. Returns the count
 * actually read; a short return means the host stopped sending. */
int hokku_console_read(uint8_t *buf, uint32_t len, uint32_t timeout_ms);

void hokku_console_write(const void *buf, uint32_t len);

/* Write one CRLF-terminated control line. The protocol's control lines are
 * ASCII and the payload is raw, so these never mix on the wire. */
void hokku_console_printf_line(const char *line);

/* Bracket a transfer. These MUST be balanced — `hokku_console_busy()` gates the
 * regime loops' restarts, so a begin without an end leaves the device unable to
 * ever refresh or reboot on schedule again. */
void hokku_console_frame_begin(void);
void hokku_console_frame_end(void);
