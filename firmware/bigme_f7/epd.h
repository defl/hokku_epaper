#pragma once

#include <stdint.h>

#define EPD_WIDTH        800
#define EPD_HEIGHT       480
#define EPD_IMAGE_BYTES  192000U  /* 800 x 480 x 4bpp / 8 */

void epd_init(void);
void epd_send_cmd(uint8_t cmd);
void epd_send_data(uint8_t data);
void epd_wait_busy(void);
void epd_refresh(void);
/* Draw msg (common/all/messages.h layout) on a white screen and refresh. */
void epd_show_text(const char *msg);