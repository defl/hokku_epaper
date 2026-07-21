#pragma once
#include <stdint.h>

/* Draw a single character at (x, y) in a 4bpp framebuffer of fb_w × fb_h
 * pixels. color is a 4-bit nibble (0=black … 0xF=white). scale multiplies
 * every font pixel into a scale×scale block of display pixels. Characters
 * outside the printable ASCII range (32–126) are replaced with '?'. */
void draw_char(uint8_t *fb, int fb_w, int fb_h, int x, int y,
               char ch, uint8_t color, int scale);

/* Draw a null-terminated string starting at (x, y), advancing by char_w
 * (6*scale) per character and wrapping when cx + char_w > fb_w. '\n' forces
 * an immediate line break. Stops when the next row would exceed fb_h. */
void draw_string(uint8_t *fb, int fb_w, int fb_h, int x, int y,
                 const char *str, uint8_t color, int scale);
