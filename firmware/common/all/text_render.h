// 5x7 bitmap text for on-glass messages, into the 4bpp panel format every hokku
// screen shares (two pixels per byte, high nibble = left pixel). Pure C (see
// README.md).
//
// Two ways in, one layout: draw_string() writes into a whole framebuffer (the
// ESP32 boards have PSRAM for one); text_render_row() produces one row at a
// time for a board that streams rows straight to its panel controller (the
// Bigme F7 has no room for a 192 KB framebuffer). Both lay text out identically.
#pragma once
#include <stdint.h>

/* Draw a single character at (x, y) in a 4bpp framebuffer of fb_w × fb_h
 * pixels. color is a 4-bit nibble. scale multiplies every font pixel into a
 * scale×scale block of display pixels. Characters outside the printable ASCII
 * range (32–126) are replaced with '?'. */
void draw_char(uint8_t *fb, int fb_w, int fb_h, int x, int y,
               char ch, uint8_t color, int scale);

/* Draw a null-terminated string starting at (x, y), advancing by char_w
 * (6*scale) per character and wrapping when cx + char_w > fb_w. '\n' forces
 * an immediate line break. Stops when the next row would exceed fb_h. */
void draw_string(uint8_t *fb, int fb_w, int fb_h, int x, int y,
                 const char *str, uint8_t color, int scale);

/* Row y (0-based) of a fb_w × fb_h image that is background `bg` with str
 * drawn by draw_string(fb, fb_w, fb_h, x, y0, str, color, scale): fills
 * row[0 .. fb_w/2). fb_w must be even. */
void text_render_row(uint8_t *row, int fb_w, int fb_h, int y, int x, int y0,
                     const char *str, uint8_t color, uint8_t bg, int scale);
