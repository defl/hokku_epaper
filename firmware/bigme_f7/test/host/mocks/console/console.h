#pragma once
#include <stdint.h>

#include "driver/chip/hal_uart.h"

/* Mock of the XR872 SDK's console, for the host build only.
 *
 * hokku_frame_receive() borrows the UART from the console for the duration of a
 * frame upload: console_disable() hands it over, console_write() is the only way
 * to send the per-chunk ACK while the console is down, and console_enable() is
 * the single restore point. The counters below exist so a test can assert that
 * the handover is balanced — an unbalanced one leaves the device with no
 * console, which on real hardware means no way in short of a replug. */

static int      _mock_console_disable_called;
static int      _mock_console_enable_called;
static uint8_t  _mock_console_written[8192];
static uint32_t _mock_console_written_len;

static inline UART_ID console_get_uart_id(void) { return UART0_ID; }

static inline void console_disable(void) { _mock_console_disable_called++; }
static inline void console_enable(void) { _mock_console_enable_called++; }

static inline void console_write(const uint8_t *buf, uint32_t len)
{
    uint32_t i;

    for (i = 0; i < len; i++) {
        if (_mock_console_written_len < sizeof(_mock_console_written))
            _mock_console_written[_mock_console_written_len++] = buf[i];
    }
}
