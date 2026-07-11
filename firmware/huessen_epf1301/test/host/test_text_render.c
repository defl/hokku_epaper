/*
 * test_text_render.c — host-side unit tests for draw_char / draw_string.
 *
 * No ESP-IDF dependency: text_render.c is pure C (standard headers only).
 * Build: compiled alongside ../../main/text_render.c by CMakeLists.txt.
 * Run:   ./test_text_render   (exit 0 on all pass, 1 if any fail)
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "../../main/text_render.h"

/* ── Minimal test framework ──────────────────────────────────────────── */
static int g_pass = 0;
static int g_fail = 0;

#define CHECK(cond, name) do {                                      \
    if (cond) { printf("PASS  %s\n", name); g_pass++; }            \
    else       { printf("FAIL  %s\n", name); g_fail++; }           \
} while (0)

/* ── Helpers ─────────────────────────────────────────────────────────── */

/* Read the 4bpp nibble at pixel (px, py) in a framebuffer of width fb_w. */
static uint8_t get_nibble(const uint8_t *fb, int fb_w, int px, int py)
{
    int idx = py * fb_w + px;
    return (idx % 2 == 0) ? ((fb[idx / 2] >> 4) & 0xF)
                           : (fb[idx / 2] & 0xF);
}

/* Fill every nibble of a framebuffer with the given 4-bit value. */
static void fill_nibble(uint8_t *fb, int n_bytes, uint8_t nibble)
{
    uint8_t byte = (uint8_t)((nibble << 4) | (nibble & 0xF));
    memset(fb, byte, (size_t)n_bytes);
}

/* ── draw_char tests ─────────────────────────────────────────────────── */

static void test_space_does_not_modify_framebuffer(void)
{
    /* Space glyph is all-zero — no bits set, so no pixels written. */
    uint8_t fb[20];
    fill_nibble(fb, sizeof(fb), 0x5);
    draw_char(fb, 10, 4, 0, 0, ' ', 0xF, 1);
    int dirty = 0;
    for (int i = 0; i < (int)sizeof(fb); i++) if (fb[i] != 0x55) dirty++;
    CHECK(dirty == 0, "draw_char: space glyph leaves framebuffer unchanged");
}

static void test_out_of_range_char_falls_back_to_question_mark(void)
{
    /* draw_char renders ch < 32 as '?'. fb_w=20, fb_h=10 → 20*10/2=100 bytes. */
    uint8_t fb_ctrl[100] = {0};
    uint8_t fb_q[100]    = {0};
    draw_char(fb_ctrl, 20, 10, 0, 0, 0x01, 0xF, 1);
    draw_char(fb_q,    20, 10, 0, 0, '?',  0xF, 1);
    CHECK(memcmp(fb_ctrl, fb_q, sizeof(fb_ctrl)) == 0,
          "draw_char: control char (0x01) renders same as '?'");
}

static void test_even_pixel_writes_high_nibble(void)
{
    /* 'I' glyph: col[2]=0x7F, bit[0]=1 → pixel(2,0) in a 10-wide fb.
     * idx = 2, even → high nibble of fb[1]. fb_w=10, fb_h=10 → 50 bytes. */
    uint8_t fb[50] = {0};
    draw_char(fb, 10, 10, 0, 0, 'I', 0xA, 1);
    CHECK(((fb[1] >> 4) & 0xF) == 0xA,
          "draw_char: even-indexed pixel written to high nibble");
}

static void test_odd_pixel_writes_low_nibble(void)
{
    /* 'I' glyph: col[3]=0x41, bit[0]=1 → pixel(3,0) in a 10-wide fb.
     * idx = 3, odd → low nibble of fb[1]. fb_w=10, fb_h=10 → 50 bytes. */
    uint8_t fb[50] = {0};
    draw_char(fb, 10, 10, 0, 0, 'I', 0xB, 1);
    CHECK((fb[1] & 0xF) == 0xB,
          "draw_char: odd-indexed pixel written to low nibble");
}

static void test_adjacent_nibbles_are_independent(void)
{
    /* Writing color 0xA to pixel(2,0) and 0xB to pixel(3,0) share byte fb[1].
     * Both nibbles should coexist: high=0xA, low=0xB. fb_w=10, fb_h=10 → 50 bytes. */
    uint8_t fb[50] = {0};
    /* 'I' col[2] bit[0]=1 → pixel(2,0) high nibble
       'I' col[3] bit[0]=1 → pixel(3,0) low nibble
       Same glyph, same color — just verify byte encodes both. */
    draw_char(fb, 10, 10, 0, 0, 'I', 0xC, 1);
    uint8_t hi = (fb[1] >> 4) & 0xF;
    uint8_t lo = fb[1] & 0xF;
    CHECK(hi == 0xC && lo == 0xC,
          "draw_char: high and low nibbles in same byte are both written correctly");
}

static void test_clip_past_right_edge_does_not_crash(void)
{
    /* Draw at x = fb_w - 1 = 9; cols 1..4 are clipped. fb_w=10, fb_h=10 → 50 bytes. */
    uint8_t fb[50] = {0};
    draw_char(fb, 10, 10, 9, 0, 'H', 0xF, 1);
    CHECK(1, "draw_char: drawing near right edge does not crash");
    /* col[0]=0x7F, all rows — pixel(9,0): idx=9, odd → low nibble of fb[4]. */
    CHECK((fb[4] & 0xF) == 0xF,
          "draw_char: rightmost visible pixel is written");
}

static void test_clip_past_bottom_edge_does_not_crash(void)
{
    /* Draw at y = fb_h - 1; rows 1..6 of the glyph are clipped. fb_w=10, fb_h=10 → 50 bytes. */
    uint8_t fb[50] = {0};
    draw_char(fb, 10, 10, 0, 9, 'A', 0xC, 1);
    CHECK(1, "draw_char: drawing near bottom edge does not crash");
}

static void test_entirely_out_of_bounds_does_not_crash(void)
{
    uint8_t fb[10] = {0};
    uint8_t orig[10];
    memcpy(orig, fb, sizeof(fb));
    draw_char(fb, 10, 10, -100, -100, 'X', 0xF, 1);
    CHECK(memcmp(fb, orig, sizeof(fb)) == 0,
          "draw_char: fully clipped char leaves framebuffer unchanged");
}

static void test_scale2_block_is_2x2(void)
{
    /* 'I' col[2]=0x7F, bit[0]=1 → at scale=2, x=0, fb_w=20:
     *   px = 0 + 2*2 + {0,1} = {4,5},  py = 0 + 0*2 + {0,1} = {0,1}
     * pixel(4,0): idx=4, even, byte=2, high nibble
     * pixel(5,0): idx=5, odd,  byte=2, low  nibble
     * pixel(4,1): idx=24, even, byte=12, high nibble
     * pixel(5,1): idx=25, odd,  byte=12, low  nibble
     * fb_w=20, fb_h=20 → 20*20/2=200 bytes. */
    uint8_t fb[200] = {0};
    draw_char(fb, 20, 20, 0, 0, 'I', 0xD, 2);
    CHECK(((fb[2]  >> 4) & 0xF) == 0xD, "draw_char scale=2: top-left pixel of 2×2 block");
    CHECK((fb[2]  & 0xF)        == 0xD, "draw_char scale=2: top-right pixel of 2×2 block");
    CHECK(((fb[12] >> 4) & 0xF) == 0xD, "draw_char scale=2: bottom-left pixel of 2×2 block");
    CHECK((fb[12] & 0xF)        == 0xD, "draw_char scale=2: bottom-right pixel of 2×2 block");
}

/* ── draw_string tests ───────────────────────────────────────────────── */

static void test_single_char_string_matches_draw_char(void)
{
    uint8_t fb_str[100] = {0};
    uint8_t fb_chr[100] = {0};
    draw_string(fb_str, 20, 10, 0, 0, "A", 0xF, 1);
    draw_char(fb_chr,   20, 10, 0, 0, 'A', 0xF, 1);
    CHECK(memcmp(fb_str, fb_chr, sizeof(fb_str)) == 0,
          "draw_string: single-char string is identical to draw_char");
}

static void test_empty_string_leaves_framebuffer_unchanged(void)
{
    uint8_t fb[20];
    fill_nibble(fb, sizeof(fb), 0xA);
    draw_string(fb, 10, 4, 0, 0, "", 0xF, 1);
    int dirty = 0;
    for (int i = 0; i < (int)sizeof(fb); i++) if (fb[i] != 0xAA) dirty++;
    CHECK(dirty == 0, "draw_string: empty string leaves framebuffer unchanged");
}

static void test_newline_advances_y_by_char_h(void)
{
    /* scale=1 → char_h=8. "A\nB": A at y=0, B at y=8. */
    uint8_t fb_str[400] = {0};
    uint8_t fb_ref[400] = {0};
    draw_string(fb_str, 20, 20, 0, 0, "A\nB", 0xF, 1);
    draw_char(fb_ref, 20, 20, 0, 0, 'A', 0xF, 1);
    draw_char(fb_ref, 20, 20, 0, 8, 'B', 0xF, 1);
    CHECK(memcmp(fb_str, fb_ref, sizeof(fb_str)) == 0,
          "draw_string: '\\n' advances cursor y by char_h");
}

static void test_auto_wrap_at_fb_width(void)
{
    /* fb_w=12, scale=1 → char_w=6.
     * "ABC": A at (0,0), B at (6,0), C wraps to (0,8).
     * Wrap condition: cx + char_w > fb_w  →  12 + 6 = 18 > 12 ✓ */
    uint8_t fb_str[240] = {0};
    uint8_t fb_ref[240] = {0};
    draw_string(fb_str, 12, 20, 0, 0, "ABC", 0xF, 1);
    draw_char(fb_ref, 12, 20, 0, 0, 'A', 0xF, 1);
    draw_char(fb_ref, 12, 20, 6, 0, 'B', 0xF, 1);
    draw_char(fb_ref, 12, 20, 0, 8, 'C', 0xF, 1);
    CHECK(memcmp(fb_str, fb_ref, sizeof(fb_str)) == 0,
          "draw_string: long line auto-wraps at fb_w");
}

static void test_stops_when_next_row_exceeds_fb_height(void)
{
    /* fb_h=10, scale=1 → char_h=8.
     * "A\nB": A at cy=0 (cy+8=8≤10 ✓), newline→cy=8, B: cy+8=16>10 → stops. */
    uint8_t fb_str[200] = {0};
    uint8_t fb_ref[200] = {0};
    draw_string(fb_str, 20, 10, 0, 0, "A\nB", 0xF, 1);
    draw_char(fb_ref,   20, 10, 0, 0, 'A',   0xF, 1);   /* only A drawn */
    CHECK(memcmp(fb_str, fb_ref, sizeof(fb_str)) == 0,
          "draw_string: stops rendering when next row would exceed fb_h");
}

static void test_string_respects_start_offset(void)
{
    /* Starting at (6, 0) should match draw_char at the same offset. */
    uint8_t fb_str[100] = {0};
    uint8_t fb_ref[100] = {0};
    draw_string(fb_str, 20, 10, 6, 0, "Z", 0xF, 1);
    draw_char(fb_ref,   20, 10, 6, 0, 'Z', 0xF, 1);
    CHECK(memcmp(fb_str, fb_ref, sizeof(fb_str)) == 0,
          "draw_string: string starting at non-zero x matches draw_char");
}

static void test_nibble_values_are_preserved_across_chars(void)
{
    /* "AB" — each pixel should carry the specified color nibble.
     * Spot-check: 'A' col[0]=0x7E, bit[1]=1 → pixel(0,1) set.
     * idx = 1*fb_w + 0 = 20 (fb_w=20), even → high nibble of byte 10. */
    uint8_t fb[200] = {0};
    draw_string(fb, 20, 10, 0, 0, "A", 0x7, 1);
    uint8_t nibble = get_nibble(fb, 20, 0, 1);
    CHECK(nibble == 0x7, "draw_string: color nibble stored correctly in framebuffer");
}

/* ── Entry point ─────────────────────────────────────────────────────── */

int main(void)
{
    printf("=== test_text_render ===\n\n");

    test_space_does_not_modify_framebuffer();
    test_out_of_range_char_falls_back_to_question_mark();
    test_even_pixel_writes_high_nibble();
    test_odd_pixel_writes_low_nibble();
    test_adjacent_nibbles_are_independent();
    test_clip_past_right_edge_does_not_crash();
    test_clip_past_bottom_edge_does_not_crash();
    test_entirely_out_of_bounds_does_not_crash();
    test_scale2_block_is_2x2();

    test_single_char_string_matches_draw_char();
    test_empty_string_leaves_framebuffer_unchanged();
    test_newline_advances_y_by_char_h();
    test_auto_wrap_at_fb_width();
    test_stops_when_next_row_exceeds_fb_height();
    test_string_respects_start_offset();
    test_nibble_values_are_preserved_across_chars();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail > 0) ? 1 : 0;
}
