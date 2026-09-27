#pragma once
#include <stdint.h>

#include "hal_def.h"

/* Mock of the XR872 SDK's UART HAL, for the host build only.
 *
 * hokku_frame_receive() takes the UART away from the console and polls it
 * directly, so the host test needs to be able to feed it bytes. The real header
 * declares a good deal more; only what main.c touches is modelled here, the
 * same way the other mocks in this tree do.
 *
 * The stream is generated rather than copied from a buffer: a whole frame is
 * 192000 bytes, which is a lot of .bss to carry around for a test that never
 * inspects the content. `_mock_uart_avail` is how many bytes the host will
 * still deliver before going silent, which makes a truncated transfer — the
 * branch that leaves the panel mid-DTM — a one-line setup. */

typedef enum { UART0_ID = 0, UART1_ID = 1 } UART_ID;

/* ── Controllable mock state ─────────────────────────────────────────────── */
static uint32_t _mock_uart_avail;     /* bytes the host still has to give */
static uint32_t _mock_uart_delivered; /* bytes actually handed over */
static int      _mock_uart_polls;     /* number of Receive_Poll calls */

static inline int32_t HAL_UART_Receive_Poll(UART_ID uart, uint8_t *buf, int32_t size,
                                            uint32_t msec)
{
    uint32_t n;
    uint32_t i;

    (void)uart;
    (void)msec;
    _mock_uart_polls++;
    if (size <= 0)
        return 0;

    n = (uint32_t)size < _mock_uart_avail ? (uint32_t)size : _mock_uart_avail;
    for (i = 0; i < n; i++)
        buf[i] = (uint8_t)(_mock_uart_delivered + i);
    _mock_uart_avail -= n;
    _mock_uart_delivered += n;
    return (int32_t)n;
}
