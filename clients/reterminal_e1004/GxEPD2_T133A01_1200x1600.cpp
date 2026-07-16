// GxEPD2 driver for the T133A01 (13.3", 1200 x 1600, 7-color) dual-chip panel
// on the Seeed reTerminal E1004.  See header for design notes.

#include "GxEPD2_T133A01_1200x1600.h"

#if defined(ESP32)
#include <esp_heap_caps.h>
#endif

#define DBG Serial0

// T133A01 register constants and init data (from Seeed_GFX T133A01_Defines.h)
static const uint8_t PSR_V[]   = {0xDF, 0x69};
static const uint8_t PWR_V[]   = {0x0F, 0x00, 0x28, 0x2C, 0x28, 0x38};
static const uint8_t POF_V[]   = {0x00};
static const uint8_t DRF_V[]   = {0x01};
static const uint8_t CDI_V[]   = {0x37};
static const uint8_t TRES_V[]  = {0x04, 0xB0, 0x03, 0x20};
static const uint8_t AMV_V[]   = {0x01, 0x00};
static const uint8_t CCSET_CUR[] = {0x01};
static const uint8_t PWS_V[]  = {0x22};
static const uint8_t DCDC_V[] = {0x44, 0x54, 0x00};
static const uint8_t BTST_P_V[] = {0xE0, 0x20};
static const uint8_t BTST_N_V[] = {0xE0, 0x20};
static const uint8_t r74[] = {0x00, 0x0C, 0x0C, 0xD9, 0xDD, 0xDD, 0x15, 0x15, 0x55};
static const uint8_t rf0[] = {0x49, 0x55, 0x13, 0x5D, 0x05, 0x10};
static const uint8_t r60[] = {0x03, 0x03};
static const uint8_t r86[] = {0x10};
static const uint8_t rb6[] = {0x07};
static const uint8_t rb7[] = {0x01};
static const uint8_t rb0[] = {0x01};
static const uint8_t rb1[] = {0x02};

// Register addresses
#define R00_PSR   0x00
#define R01_PWR   0x01
#define R02_POF   0x02
#define R04_PON   0x04
#define R05_BTST_N 0x05
#define R06_BTST_P 0x06
#define R10_DTM   0x10
#define R12_DRF   0x12
#define R50_CDI   0x50
#define R61_TRES  0x61
#define RA5_DCDC  0xA5
#define RE0_CCSET 0xE0
#define RE3_PWS   0xE3

// GxEPD2 color encoding → T133A01 native encoding
//
// T133A01 native palette (Spectra 6 — Black/White/Red/Green/Blue/Yellow):
//   0x00 = Black, 0x01 = White, 0x02 = Yellow, 0x03 = Red,
//   0x05 = Blue,  0x06 = Green       (0x04, 0x07 unused on this panel)
//
// GxEPD2 color7() palette (drawing API supports 7 entries):
//   0=Black 1=White 2=Green 3=Blue 4=Red 5=Yellow 6=Orange 7=unused
//
// Spectra 6 has no orange; GxEPD_ORANGE is therefore aliased to yellow
// (closest visual match).  Application code should normally avoid
// GxEPD_ORANGE on this board.
static const uint8_t _native_map[8] = {
  0x00, // 0: Black  -> 0x00
  0x01, // 1: White  -> 0x01
  0x06, // 2: Green  -> 0x06
  0x05, // 3: Blue   -> 0x05
  0x03, // 4: Red    -> 0x03
  0x02, // 5: Yellow -> 0x02
  0x02, // 6: Orange -> 0x02 (no native orange; alias to yellow)
  0x01, // 7: unused -> 0x01 (white fallback)
};

GxEPD2_T133A01_1200x1600::GxEPD2_T133A01_1200x1600(
    int16_t cs, int16_t dc, int16_t rst, int16_t busy,
    int16_t cs1, int16_t enable)
  : GxEPD2_EPD(cs, dc, rst, busy, LOW, 60000, WIDTH, HEIGHT, panel, hasColor,
                hasPartialUpdate, hasFastPartialUpdate),
    _cs1(cs1), _enable(enable), _frame_buf(nullptr), _paged(false)
{
}

// ---- helpers ----

uint8_t GxEPD2_T133A01_1200x1600::_convert_to_native(uint8_t data)
{
  uint8_t hi = (data >> 4) & 0x07;
  uint8_t lo = data & 0x07;
  return (_native_map[hi] << 4) | _native_map[lo];
}

// Send a command + data sequence to BOTH chips simultaneously.
// CS toggling happens OUTSIDE the SPI transaction to mirror Seeed_GFX timing.
void GxEPD2_T133A01_1200x1600::_sendCmdDataBoth(uint8_t cmd, const uint8_t* data, uint16_t len)
{
  digitalWrite(_cs1, LOW);                  // slave selected
  if (_cs >= 0) digitalWrite(_cs, LOW);     // master selected
  _pSPIx->beginTransaction(_spi_settings);
  if (_dc >= 0) digitalWrite(_dc, LOW);
  _pSPIx->transfer(cmd);
  if (_dc >= 0) digitalWrite(_dc, HIGH);
  for (uint16_t i = 0; i < len; i++) _pSPIx->transfer(data[i]);
  _pSPIx->endTransaction();
  if (_cs >= 0) digitalWrite(_cs, HIGH);    // master deselected
  digitalWrite(_cs1, HIGH);                 // slave deselected
}

// Send a command + data sequence to the MASTER chip only.
void GxEPD2_T133A01_1200x1600::_sendCmdDataCS0(uint8_t cmd, const uint8_t* data, uint16_t len)
{
  digitalWrite(_cs1, HIGH);                 // ensure slave NOT selected
  if (_cs >= 0) digitalWrite(_cs, LOW);     // master selected
  _pSPIx->beginTransaction(_spi_settings);
  if (_dc >= 0) digitalWrite(_dc, LOW);
  _pSPIx->transfer(cmd);
  if (_dc >= 0) digitalWrite(_dc, HIGH);
  for (uint16_t i = 0; i < len; i++) _pSPIx->transfer(data[i]);
  _pSPIx->endTransaction();
  if (_cs >= 0) digitalWrite(_cs, HIGH);    // master deselected
}

// Strict port of Seeed_GFX CHECK_BUSY() — always delay first, then poll.
// This avoids the classic "BUSY hasn't gone low yet" race that the upstream
// GxEPD2_EPD::_waitWhileBusy can fall into (it polls after only 1 ms).
void GxEPD2_T133A01_1200x1600::_checkBusy(const char* tag, uint32_t timeout_ms)
{
  if (_busy < 0) {
    delay(timeout_ms < 1000 ? timeout_ms : 100);
    return;
  }
  unsigned long t_start = millis();
  uint32_t polls = 0;
  while (true) {
    delay(10);
    polls++;
    if (digitalRead(_busy) == HIGH) break;     // ready
    if (millis() - t_start > timeout_ms) {
      if (_diag_enabled) {
        DBG.print(F("[T133A01] BUSY timeout @ "));
        DBG.print(tag);
        DBG.print(F(" after "));
        DBG.print(millis() - t_start);
        DBG.println(F(" ms"));
      }
      break;
    }
  }
  if (_diag_enabled) {
    unsigned long dt = millis() - t_start;
    DBG.print(F("[T133A01] busy["));
    DBG.print(tag);
    DBG.print(F("] ms="));
    DBG.print(dt);
    DBG.print(F(" polls="));
    DBG.println(polls);
  }
}

void GxEPD2_T133A01_1200x1600::_allocFrameBuffer()
{
  if (_frame_buf) return;
#if defined(ESP32)
  _frame_buf = (uint8_t*)heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
#else
  _frame_buf = (uint8_t*)malloc(FRAME_BYTES);
#endif
  if (!_frame_buf) {
    DBG.println(F("[T133A01] FATAL: PSRAM alloc failed (960 KB needed)"));
    while (true) delay(1000);
  }
}

// Fill with GxEPD2-encoded value (0x11 = white, 0x00 = black)
void GxEPD2_T133A01_1200x1600::_fillFrameBuffer(uint8_t value)
{
  _allocFrameBuffer();
  memset(_frame_buf, value, FRAME_BYTES);
}

// ---- init ----

void GxEPD2_T133A01_1200x1600::init(uint32_t serial_diag_bitrate)
{
  init(serial_diag_bitrate, true, 10, false);
}

void GxEPD2_T133A01_1200x1600::init(uint32_t serial_diag_bitrate,
                                      bool initial, uint16_t reset_duration,
                                      bool pulldown_rst_mode)
{
  _initial_write = initial;
  _initial_refresh = initial;
  _pulldown_rst_mode = pulldown_rst_mode;
  _power_is_on = false;
  _using_partial_mode = false;
  _hibernating = false;
  _init_display_done = false;
  _reset_duration = reset_duration;

  // Force diagnostics on while bringing the panel up.  Comment this out
  // once everything works to silence per-refresh log lines.
  _diag_enabled = true;
  if (serial_diag_bitrate > 0) {
    _diag_enabled = true;
  }

  // Pin setup (from T133A01_Init.h)
  if (_cs >= 0) { pinMode(_cs, OUTPUT); digitalWrite(_cs, HIGH); }
  if (_dc >= 0) { pinMode(_dc, OUTPUT); digitalWrite(_dc, HIGH); }
  if (_rst >= 0) { pinMode(_rst, OUTPUT); digitalWrite(_rst, HIGH); }
  if (_busy >= 0) { pinMode(_busy, INPUT); }
  pinMode(_cs1, OUTPUT); digitalWrite(_cs1, HIGH);
  if (_enable >= 0) { pinMode(_enable, OUTPUT); digitalWrite(_enable, HIGH); }

  _allocFrameBuffer();
  _fillFrameBuffer(0x11); // white in GxEPD2 encoding

  _InitDisplay();

  if (_diag_enabled) {
    DBG.println(F("[T133A01] init done: 1200x1600 7-color, dual-chip"));
  }
}

void GxEPD2_T133A01_1200x1600::_InitDisplay()
{
  // Hardware reset
  if (_rst >= 0) {
    digitalWrite(_rst, LOW); delay(20);
    digitalWrite(_rst, HIGH); delay(20);
  }
  _waitWhileBusy("reset", 5000);

  // 0x74: undocumented config → main CS only
  _sendCmdDataCS0(0x74, r74, sizeof(r74));
  delay(10);

  // 0xF0: undocumented config → both chips
  _sendCmdDataBoth(0xF0, rf0, sizeof(rf0));
  delay(10);

  // PSR (Panel Setting) → both
  _sendCmdDataBoth(R00_PSR, PSR_V, sizeof(PSR_V));
  delay(10);

  // DCDC → main CS only
  _sendCmdDataCS0(RA5_DCDC, DCDC_V, sizeof(DCDC_V));
  delay(10);

  // CDI (VCOM and data interval) → both
  _sendCmdDataBoth(R50_CDI, CDI_V, sizeof(CDI_V));
  delay(10);

  // 0x60 (Gate/Source timing) → both
  _sendCmdDataBoth(0x60, r60, sizeof(r60));
  delay(10);

  // 0x86 → both
  _sendCmdDataBoth(0x86, r86, sizeof(r86));
  delay(10);

  // PWS (Power Saving) → both
  _sendCmdDataBoth(RE3_PWS, PWS_V, sizeof(PWS_V));
  delay(10);

  // TRES (Resolution) → both
  _sendCmdDataBoth(R61_TRES, TRES_V, sizeof(TRES_V));
  delay(10);

  // PWR (Power Setting) → main CS only
  _sendCmdDataCS0(R01_PWR, PWR_V, sizeof(PWR_V));
  delay(10);

  // Boost timing parameters → main CS only
  _sendCmdDataCS0(0xB6, rb6, sizeof(rb6));
  delay(10);
  _sendCmdDataCS0(R06_BTST_P, BTST_P_V, sizeof(BTST_P_V));
  delay(10);
  _sendCmdDataCS0(0xB7, rb7, sizeof(rb7));
  delay(10);
  _sendCmdDataCS0(R05_BTST_N, BTST_N_V, sizeof(BTST_N_V));
  delay(10);
  _sendCmdDataCS0(0xB0, rb0, sizeof(rb0));
  delay(10);
  _sendCmdDataCS0(0xB1, rb1, sizeof(rb1));
  delay(10);

  _init_display_done = true;
  _initial_write = false;
}

// ---- Power control (both chips) ----

void GxEPD2_T133A01_1200x1600::_PowerOnBoth()
{
  if (_power_is_on) return;
  _sendCmdDataBoth(R04_PON, nullptr, 0);
  _checkBusy("PON(PowerOnBoth)", 5000);
  _power_is_on = true;
}

void GxEPD2_T133A01_1200x1600::_PowerOffBoth()
{
  if (!_power_is_on) return;
  _sendCmdDataBoth(R02_POF, POF_V, sizeof(POF_V));
  _checkBusy("POF(PowerOffBoth)", 5000);
  _power_is_on = false;
}

// ---- Frame buffer ----

void GxEPD2_T133A01_1200x1600::clearScreen(uint8_t value)
{
  _fillFrameBuffer(0xFF == value ? 0x11 : 0x00);
  refresh();
}

void GxEPD2_T133A01_1200x1600::writeScreenBuffer(uint8_t value)
{
  writeScreenBuffer(value, 0xFF);
}

void GxEPD2_T133A01_1200x1600::writeScreenBuffer(uint8_t black_value, uint8_t color_value)
{
  _allocFrameBuffer();
  _fillFrameBuffer(0xFF == black_value ? 0x11 : 0x00);
}

void GxEPD2_T133A01_1200x1600::setPaged()
{
  _paged = true;
}

// ---- writeNative: copy 4bpp GxEPD2 page data into PSRAM framebuffer ----

void GxEPD2_T133A01_1200x1600::writeNative(const uint8_t* data1, const uint8_t* data2,
    int16_t x, int16_t y, int16_t w, int16_t h, bool invert, bool mirror_y, bool pgm)
{
  if (!data1) return;
  _allocFrameBuffer();
  uint32_t wb = (w + 1) / 2;
  for (int16_t row = 0; row < h; row++) {
    int16_t dst_y = y + row;
    if (dst_y < 0 || dst_y >= HEIGHT) continue;
    for (int16_t col = 0; col < (int16_t)wb; col++) {
      int16_t dst_x2 = x / 2 + col;
      if (dst_x2 < 0 || dst_x2 >= (int16_t)BYTES_PER_ROW) continue;
      uint32_t src_idx = mirror_y ? col + uint32_t(h - 1 - row) * wb
                                  : col + uint32_t(row) * wb;
      uint8_t data;
      if (pgm) {
#if defined(__AVR) || defined(ESP8266) || defined(ESP32)
        data = pgm_read_byte(&data1[src_idx]);
#else
        data = data1[src_idx];
#endif
      } else {
        data = data1[src_idx];
      }
      _frame_buf[uint32_t(dst_y) * BYTES_PER_ROW + dst_x2] = data;
    }
  }
}

void GxEPD2_T133A01_1200x1600::writeNativePart(
    const uint8_t* data1, const uint8_t* data2,
    int16_t x_part, int16_t y_part, int16_t w_bitmap, int16_t h_bitmap,
    int16_t x, int16_t y, int16_t w, int16_t h,
    bool invert, bool mirror_y, bool pgm)
{
  if (!data1) return;
  _allocFrameBuffer();
  if ((w_bitmap < 0) || (h_bitmap < 0) || (w < 0) || (h < 0)) return;
  if ((x_part < 0) || (x_part >= w_bitmap)) return;
  if ((y_part < 0) || (y_part >= h_bitmap)) return;
  uint32_t wb_bitmap = (w_bitmap + 1) / 2;
  w = w_bitmap - x_part < w ? w_bitmap - x_part : w;
  h = h_bitmap - y_part < h ? h_bitmap - y_part : h;
  uint32_t wb = (w + 1) / 2;
  for (int16_t row = 0; row < h; row++) {
    int16_t dst_y = y + row;
    if (dst_y < 0 || dst_y >= HEIGHT) continue;
    for (int16_t col = 0; col < (int16_t)wb; col++) {
      int16_t dst_x2 = x / 2 + col;
      if (dst_x2 < 0 || dst_x2 >= (int16_t)BYTES_PER_ROW) continue;
      uint32_t src_idx = mirror_y
        ? x_part / 2 + col + uint32_t(h_bitmap - 1 - (y_part + row)) * wb_bitmap
        : x_part / 2 + col + uint32_t(y_part + row) * wb_bitmap;
      uint8_t data;
      if (pgm) {
#if defined(__AVR) || defined(ESP8266) || defined(ESP32)
        data = pgm_read_byte(&data1[src_idx]);
#else
        data = data1[src_idx];
#endif
      } else {
        data = data1[src_idx];
      }
      _frame_buf[uint32_t(dst_y) * BYTES_PER_ROW + dst_x2] = data;
    }
  }
}

// ---- writeImage: convert 1bpp B&W bitmap to 4bpp ----

void GxEPD2_T133A01_1200x1600::writeImage(const uint8_t bitmap[], int16_t x, int16_t y,
    int16_t w, int16_t h, bool invert, bool mirror_y, bool pgm)
{
  if (!bitmap) return;
  _allocFrameBuffer();
  int16_t wb = (w + 7) / 8;
  for (int16_t row = 0; row < h; row++) {
    int16_t dst_y = y + row;
    if (dst_y < 0 || dst_y >= HEIGHT) continue;
    for (int16_t col = 0; col < w; col++) {
      int16_t dst_x = x + col;
      if (dst_x < 0 || dst_x >= WIDTH) continue;
      uint32_t src_idx = mirror_y ? col / 8 + uint32_t(h - 1 - row) * wb
                                  : col / 8 + uint32_t(row) * wb;
      uint8_t byte_val;
      if (pgm) {
#if defined(__AVR) || defined(ESP8266) || defined(ESP32)
        byte_val = pgm_read_byte(&bitmap[src_idx]);
#else
        byte_val = bitmap[src_idx];
#endif
      } else {
        byte_val = bitmap[src_idx];
      }
      if (invert) byte_val = ~byte_val;
      bool white = byte_val & (0x80 >> (col & 7));
      uint8_t color7 = white ? 0x01 : 0x00;
      uint32_t fb_idx = uint32_t(dst_y) * BYTES_PER_ROW + dst_x / 2;
      if (dst_x & 1) _frame_buf[fb_idx] = (_frame_buf[fb_idx] & 0xF0) | color7;
      else           _frame_buf[fb_idx] = (_frame_buf[fb_idx] & 0x0F) | (color7 << 4);
    }
  }
}

void GxEPD2_T133A01_1200x1600::writeImagePart(const uint8_t bitmap[],
    int16_t x_part, int16_t y_part, int16_t w_bitmap, int16_t h_bitmap,
    int16_t x, int16_t y, int16_t w, int16_t h,
    bool invert, bool mirror_y, bool pgm)
{
  writeImage(bitmap, x, y, w, h, invert, mirror_y, pgm);
}

void GxEPD2_T133A01_1200x1600::writeImage(const uint8_t* black, const uint8_t* color,
    int16_t x, int16_t y, int16_t w, int16_t h,
    bool invert, bool mirror_y, bool pgm)
{
  if (black) writeImage(black, x, y, w, h, invert, mirror_y, pgm);
}

void GxEPD2_T133A01_1200x1600::writeImagePart(const uint8_t* black, const uint8_t* color,
    int16_t x_part, int16_t y_part, int16_t w_bitmap, int16_t h_bitmap,
    int16_t x, int16_t y, int16_t w, int16_t h,
    bool invert, bool mirror_y, bool pgm)
{
  if (black) writeImagePart(black, x_part, y_part, w_bitmap, h_bitmap, x, y, w, h, invert, mirror_y, pgm);
}

// ---- draw variants (write + refresh) ----

void GxEPD2_T133A01_1200x1600::drawImage(const uint8_t bitmap[], int16_t x, int16_t y,
    int16_t w, int16_t h, bool invert, bool mirror_y, bool pgm)
{
  writeImage(bitmap, x, y, w, h, invert, mirror_y, pgm);
  refresh(x, y, w, h);
}

void GxEPD2_T133A01_1200x1600::drawImagePart(const uint8_t bitmap[],
    int16_t x_part, int16_t y_part, int16_t w_bitmap, int16_t h_bitmap,
    int16_t x, int16_t y, int16_t w, int16_t h,
    bool invert, bool mirror_y, bool pgm)
{
  writeImagePart(bitmap, x_part, y_part, w_bitmap, h_bitmap, x, y, w, h, invert, mirror_y, pgm);
  refresh(x, y, w, h);
}

void GxEPD2_T133A01_1200x1600::drawImage(const uint8_t* black, const uint8_t* color,
    int16_t x, int16_t y, int16_t w, int16_t h,
    bool invert, bool mirror_y, bool pgm)
{
  writeImage(black, color, x, y, w, h, invert, mirror_y, pgm);
  refresh(x, y, w, h);
}

void GxEPD2_T133A01_1200x1600::drawImagePart(const uint8_t* black, const uint8_t* color,
    int16_t x_part, int16_t y_part, int16_t w_bitmap, int16_t h_bitmap,
    int16_t x, int16_t y, int16_t w, int16_t h,
    bool invert, bool mirror_y, bool pgm)
{
  writeImagePart(black, color, x_part, y_part, w_bitmap, h_bitmap, x, y, w, h, invert, mirror_y, pgm);
  refresh(x, y, w, h);
}

void GxEPD2_T133A01_1200x1600::drawNative(const uint8_t* data1, const uint8_t* data2,
    int16_t x, int16_t y, int16_t w, int16_t h,
    bool invert, bool mirror_y, bool pgm)
{
  writeNative(data1, data2, x, y, w, h, invert, mirror_y, pgm);
  refresh(x, y, w, h);
}

// ---- Send framebuffer to both chips and trigger display refresh ----

// Strict port of Seeed_GFX EPD_PUSH_NEW_COLORS:
//   - CS toggling happens OUTSIDE SPI transactions
//   - master CS and slave CS1 are kept mutually exclusive
//   - DC and SPI transaction order matches Seeed exactly
void GxEPD2_T133A01_1200x1600::_sendFrameToDisplay()
{
  if (!_init_display_done) _InitDisplay();

  unsigned long t0 = millis();
  uint32_t bytes_chip0 = 0;
  uint32_t bytes_chip1 = 0;

  // CCSET → both chips
  _sendCmdDataBoth(RE0_CCSET, CCSET_CUR, sizeof(CCSET_CUR));
  _checkBusy("CCSET", 1000);
  delay(10);

  // ------ Chip 0 (master, CS): LEFT half of each row ------
  // (CS toggling outside the SPI transaction, mirroring Seeed_GFX)
  digitalWrite(_cs1, HIGH);              // slave NOT selected
  if (_cs >= 0) digitalWrite(_cs, LOW);  // master selected
  _pSPIx->beginTransaction(_spi_settings);
  if (_dc >= 0) digitalWrite(_dc, LOW);
  _pSPIx->transfer(R10_DTM);
  if (_dc >= 0) digitalWrite(_dc, HIGH);
  for (uint16_t row = 0; row < HEIGHT; row++) {
    const uint8_t* rp = _frame_buf + uint32_t(row) * BYTES_PER_ROW;
    for (uint16_t col = 0; col < HALF_W_BYTES; col++) {
      _pSPIx->transfer(_convert_to_native(rp[col]));
      bytes_chip0++;
    }
    if ((row & 0x3F) == 0) yield();
  }
  _pSPIx->endTransaction();
  if (_cs >= 0) digitalWrite(_cs, HIGH); // master deselected

  if (_diag_enabled) {
    DBG.print(F("[T133A01] chip0 sent "));
    DBG.print(bytes_chip0);
    DBG.println(F(" bytes (left half)"));
  }

  delay(10); // small settle time before re-asserting the other CS

  // ------ Chip 1 (slave, CS1): RIGHT half of each row ------
  digitalWrite(_cs1, LOW);               // slave selected
  _pSPIx->beginTransaction(_spi_settings);
  if (_dc >= 0) digitalWrite(_dc, LOW);
  _pSPIx->transfer(R10_DTM);
  if (_dc >= 0) digitalWrite(_dc, HIGH);
  for (uint16_t row = 0; row < HEIGHT; row++) {
    const uint8_t* rp = _frame_buf + uint32_t(row) * BYTES_PER_ROW;
    for (uint16_t col = HALF_W_BYTES; col < BYTES_PER_ROW; col++) {
      _pSPIx->transfer(_convert_to_native(rp[col]));
      bytes_chip1++;
    }
    if ((row & 0x3F) == 0) yield();
  }
  _pSPIx->endTransaction();
  digitalWrite(_cs1, HIGH);              // slave deselected

  if (_diag_enabled) {
    DBG.print(F("[T133A01] chip1 sent "));
    DBG.print(bytes_chip1);
    DBG.println(F(" bytes (right half)"));
    unsigned long dt = millis() - t0;
    DBG.print(F("[T133A01] data xfer ms = "));
    DBG.println(dt);
  }
}

void GxEPD2_T133A01_1200x1600::refresh(bool partial_update_mode)
{
  _sendFrameToDisplay();

  unsigned long t0 = millis();

  // EPD_UPDATE sequence: PON → DRF → POF (both chips)
  // NOTE: a real full-screen 7-color refresh on this 13.3" panel takes
  //       roughly 30~40 seconds, so DRF needs a long timeout.
  _sendCmdDataBoth(R04_PON, nullptr, 0);
  _checkBusy("PON", 5000);            // power-on usually < 1 s
  delay(30);

  _sendCmdDataBoth(R12_DRF, DRF_V, sizeof(DRF_V));
  _checkBusy("DRF", 60000);           // 7-color refresh: 30~40 s, give 60 s
  delay(30);

  _sendCmdDataBoth(R02_POF, POF_V, sizeof(POF_V));
  _checkBusy("POF", 5000);
  delay(30);

  _power_is_on = false;

  if (_diag_enabled) {
    unsigned long dt = millis() - t0;
    DBG.print(F("[T133A01] refresh ms = "));
    DBG.println(dt);
  }
}

void GxEPD2_T133A01_1200x1600::refresh(int16_t x, int16_t y, int16_t w, int16_t h)
{
  refresh(false);
}

void GxEPD2_T133A01_1200x1600::powerOff()
{
  _PowerOffBoth();
}

void GxEPD2_T133A01_1200x1600::hibernate()
{
  _PowerOffBoth();
  if (_rst >= 0) {
    uint8_t sleep_val = 0xA5;
    _sendCmdDataBoth(0x07, &sleep_val, 1);
    _hibernating = true;
    _init_display_done = false;
  }
}
