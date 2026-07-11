#pragma once

#include <stdint.h>

void epd_init(void);
void epd_send_cmd(uint8_t cmd);
void epd_send_data(uint8_t data);
void epd_wait_busy(void);
void epd_refresh(void);
