// GxEPD2 driver class for the T133A01 (13.3", 1200 x 1600, 7-color)
// panel as wired on the Seeed Studio reTerminal E1004 (BOARD_SCREEN_COMBO 523).
//
// The T133A01 uses a dual-chip architecture: two ePaper controllers
// share the display, each driving half the image columns.
//   - Chip 0 (CS, GPIO 10): left  600 pixels of every row
//   - Chip 1 (CS1, GPIO 2): right 600 pixels of every row
//
// GxEPD2 has no built-in driver for this panel.  This file provides
// an alternative driver in the example folder.  Move the .h/.cpp pair
// next to your sketch to reuse it elsewhere.
//
// Reference (working implementation):
//   Seeed_GFX/TFT_Drivers/T133A01_Defines.h   - registers, init, data transfer
//   Seeed_GFX/TFT_Drivers/T133A01_Init.h       - pin setup
//   Seeed_GFX/Extensions/EPaper.cpp             - update/pushColors flow
//
// Memory note: we keep a full-screen 4bpp framebuffer in PSRAM
//   (1200 * 1600 / 2 = 960 000 bytes, ~937 KB).
//   PSRAM is mandatory: select  Tools > PSRAM: OPI PSRAM  on XIAO_ESP32S3.

#ifndef _GxEPD2_T133A01_1200x1600_H_
#define _GxEPD2_T133A01_1200x1600_H_

#include <Arduino.h>
#include <GxEPD2_EPD.h>

class GxEPD2_T133A01_1200x1600 : public GxEPD2_EPD
{
  public:
    static const uint16_t WIDTH = 1200;
    static const uint16_t WIDTH_VISIBLE = WIDTH;
    static const uint16_t HEIGHT = 1600;
    static const GxEPD2::Panel panel = GxEPD2::GDEP073E01;
    static const bool hasColor = true;
    static const bool hasPartialUpdate = false;
    static const bool usePartialUpdate = false;
    static const bool hasFastPartialUpdate = false;
    static const uint16_t power_on_time = 300;
    static const uint16_t power_off_time = 200;
    static const uint16_t full_refresh_time = 30000;
    static const uint16_t partial_refresh_time = 30000;

    GxEPD2_T133A01_1200x1600(int16_t cs, int16_t dc, int16_t rst,
                               int16_t busy, int16_t cs1, int16_t enable);

    void init(uint32_t serial_diag_bitrate = 0) override;
    void init(uint32_t serial_diag_bitrate, bool initial,
              uint16_t reset_duration = 10, bool pulldown_rst_mode = false) override;

    void clearScreen(uint8_t value = 0xFF) override;
    void writeScreenBuffer(uint8_t value = 0xFF) override;
    void writeScreenBuffer(uint8_t black_value, uint8_t color_value);

    void writeImage(const uint8_t bitmap[], int16_t x, int16_t y, int16_t w, int16_t h,
                    bool invert = false, bool mirror_y = false, bool pgm = false) override;
    void writeImagePart(const uint8_t bitmap[], int16_t x_part, int16_t y_part,
                        int16_t w_bitmap, int16_t h_bitmap,
                        int16_t x, int16_t y, int16_t w, int16_t h,
                        bool invert = false, bool mirror_y = false, bool pgm = false) override;

    void writeImage(const uint8_t* black, const uint8_t* color,
                    int16_t x, int16_t y, int16_t w, int16_t h,
                    bool invert = false, bool mirror_y = false, bool pgm = false);
    void writeImagePart(const uint8_t* black, const uint8_t* color,
                        int16_t x_part, int16_t y_part, int16_t w_bitmap, int16_t h_bitmap,
                        int16_t x, int16_t y, int16_t w, int16_t h,
                        bool invert = false, bool mirror_y = false, bool pgm = false);

    void writeNative(const uint8_t* data1, const uint8_t* data2,
                     int16_t x, int16_t y, int16_t w, int16_t h,
                     bool invert = false, bool mirror_y = false, bool pgm = false);
    void writeNativePart(const uint8_t* data1, const uint8_t* data2,
                         int16_t x_part, int16_t y_part, int16_t w_bitmap, int16_t h_bitmap,
                         int16_t x, int16_t y, int16_t w, int16_t h,
                         bool invert = false, bool mirror_y = false, bool pgm = false);

    void drawImage(const uint8_t bitmap[], int16_t x, int16_t y, int16_t w, int16_t h,
                   bool invert = false, bool mirror_y = false, bool pgm = false);
    void drawImagePart(const uint8_t bitmap[], int16_t x_part, int16_t y_part,
                       int16_t w_bitmap, int16_t h_bitmap,
                       int16_t x, int16_t y, int16_t w, int16_t h,
                       bool invert = false, bool mirror_y = false, bool pgm = false);
    void drawImage(const uint8_t* black, const uint8_t* color,
                   int16_t x, int16_t y, int16_t w, int16_t h,
                   bool invert = false, bool mirror_y = false, bool pgm = false);
    void drawImagePart(const uint8_t* black, const uint8_t* color,
                       int16_t x_part, int16_t y_part, int16_t w_bitmap, int16_t h_bitmap,
                       int16_t x, int16_t y, int16_t w, int16_t h,
                       bool invert = false, bool mirror_y = false, bool pgm = false);
    void drawNative(const uint8_t* data1, const uint8_t* data2,
                    int16_t x, int16_t y, int16_t w, int16_t h,
                    bool invert = false, bool mirror_y = false, bool pgm = false);

    void refresh(bool partial_update_mode = false) override;
    void refresh(int16_t x, int16_t y, int16_t w, int16_t h) override;
    void powerOff() override;
    void hibernate() override;
    void setPaged() override;

  private:
    static constexpr uint32_t BYTES_PER_ROW = WIDTH / 2;
    static constexpr uint32_t FRAME_BYTES = uint32_t(BYTES_PER_ROW) * HEIGHT;
    static constexpr uint16_t HALF_W_BYTES = WIDTH / 4;

    int16_t _cs1;
    int16_t _enable;
    uint8_t* _frame_buf;
    bool _paged;

    void _allocFrameBuffer();
    void _fillFrameBuffer(uint8_t gxepd2_value);
    uint8_t _convert_to_native(uint8_t data);

    void _sendCmdDataBoth(uint8_t cmd, const uint8_t* data, uint16_t len);
    void _sendCmdDataCS0(uint8_t cmd, const uint8_t* data, uint16_t len);

    // Custom busy wait: matches Seeed_GFX CHECK_BUSY() exactly
    //   - 10 ms blocking delay BEFORE the first poll (gives the chip time
    //     to actually drive BUSY low after receiving a command),
    //   - poll the BUSY pin every 10 ms thereafter,
    //   - generous timeout so a real 7-color refresh has room to finish.
    void _checkBusy(const char* tag, uint32_t timeout_ms);

    void _InitDisplay();
    void _PowerOnBoth();
    void _PowerOffBoth();
    void _sendFrameToDisplay();
};

#endif
