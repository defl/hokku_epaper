#pragma once
#include <stdint.h>
#include <string.h>

#include "freertos/FreeRTOS.h"

/* Mock of the ESP-IDF USB Serial/JTAG driver, for host builds only.
 *
 * The `frame` upload borrows this peripheral from the console and drives it
 * directly, so the host tests need to be able to feed it bytes and to see what
 * the device wrote back.
 *
 * The RX stream is generated rather than copied from a buffer: a full frame is
 * 960000 bytes, which is a lot of .bss for a test that never inspects the
 * content. `_mock_usb_avail` is how many bytes the host still has to give before
 * it goes silent, which makes a truncated transfer — the branch that must leave
 * the panel untouched — a one-line setup. */

typedef struct {
    uint32_t tx_buffer_size;
    uint32_t rx_buffer_size;
} usb_serial_jtag_driver_config_t;

#define USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT() \
    (usb_serial_jtag_driver_config_t) { .tx_buffer_size = 256, .rx_buffer_size = 256 }

/* ── Controllable mock state ─────────────────────────────────────────────── */
static uint32_t _mock_usb_avail;        /* bytes the host still has to give */
static uint32_t _mock_usb_delivered;    /* bytes actually handed over */
static int      _mock_usb_reads;        /* read_bytes call count */
static int      _mock_usb_install_calls;
static esp_err_t _mock_usb_install_result;

/* Literal bytes served BEFORE the synthetic sequential pattern kicks in — a
 * real command line ("frame\r\n") ahead of image payload, so a test can drive
 * a genuinely wire-realistic byte stream through the real byte-at-a-time
 * reader instead of only through handle_line() (which every other test in this
 * file uses, and which cannot see a bug in how bytes get grouped into lines in
 * the first place — see console_process_byte in console.c). */
static uint8_t  _mock_usb_prefix[64];
static uint32_t _mock_usb_prefix_len;
static uint32_t _mock_usb_prefix_pos;

/* Everything the device wrote, so a test can assert on ACKs and control lines. */
static uint8_t  _mock_usb_tx[4096];
static uint32_t _mock_usb_tx_len;
static uint32_t _mock_usb_tx_dropped;   /* bytes past the capture buffer */

static inline esp_err_t usb_serial_jtag_driver_install(usb_serial_jtag_driver_config_t *cfg)
{
    (void)cfg;
    _mock_usb_install_calls++;
    return _mock_usb_install_result;
}

static inline int usb_serial_jtag_read_bytes(void *buf, uint32_t len, TickType_t ticks)
{
    uint8_t *out = (uint8_t *)buf;
    uint32_t n;
    uint32_t i;

    (void)ticks;
    _mock_usb_reads++;
    if (len == 0)
        return 0;

    if (_mock_usb_prefix_pos < _mock_usb_prefix_len) {
        uint32_t avail = _mock_usb_prefix_len - _mock_usb_prefix_pos;
        uint32_t p = len < avail ? len : avail;
        memcpy(out, _mock_usb_prefix + _mock_usb_prefix_pos, p);
        _mock_usb_prefix_pos += p;
        return (int)p;      /* short reads are normal; caller loops */
    }

    n = len < _mock_usb_avail ? len : _mock_usb_avail;
    for (i = 0; i < n; i++)
        out[i] = (uint8_t)(_mock_usb_delivered + i);
    _mock_usb_avail -= n;
    _mock_usb_delivered += n;
    return (int)n;
}

static inline int usb_serial_jtag_write_bytes(const void *buf, size_t len, TickType_t ticks)
{
    const uint8_t *src = (const uint8_t *)buf;
    size_t i;

    (void)ticks;
    for (i = 0; i < len; i++) {
        if (_mock_usb_tx_len < sizeof(_mock_usb_tx))
            _mock_usb_tx[_mock_usb_tx_len++] = src[i];
        else
            _mock_usb_tx_dropped++;
    }
    return (int)len;
}
