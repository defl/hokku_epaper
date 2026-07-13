#pragma once
#include <stdint.h>
#include <string.h>

typedef struct fdcm_handle { uint32_t flash, addr, size; } fdcm_handle_t;

/* ── Controllable mock state ──────────────────────────────────────────── */
static int      _mock_fdcm_open_fail;
static uint8_t  _mock_fdcm_read_buf[512];
static uint32_t _mock_fdcm_read_size;   /* bytes "available" to hand back */
static uint8_t  _mock_fdcm_write_buf[512];
static uint32_t _mock_fdcm_write_size;  /* bytes actually written, recorded */
static int      _mock_fdcm_write_call_count;

static inline fdcm_handle_t *fdcm_open(uint32_t flash, uint32_t addr, uint32_t size)
{
    static fdcm_handle_t h;
    if (_mock_fdcm_open_fail)
        return NULL;
    h.flash = flash; h.addr = addr; h.size = size;
    return &h;
}
static inline uint32_t fdcm_read(fdcm_handle_t *hdl, void *data, uint16_t data_size)
{
    (void)hdl;
    uint32_t n = _mock_fdcm_read_size < data_size ? _mock_fdcm_read_size : data_size;
    memcpy(data, _mock_fdcm_read_buf, n);
    return _mock_fdcm_read_size; /* real fdcm_read returns bytes actually read */
}
static inline uint32_t fdcm_write(fdcm_handle_t *hdl, const void *data, uint16_t data_size)
{
    (void)hdl;
    uint32_t n = data_size < sizeof(_mock_fdcm_write_buf) ? data_size : sizeof(_mock_fdcm_write_buf);
    memcpy(_mock_fdcm_write_buf, data, n);
    _mock_fdcm_write_size = data_size;
    _mock_fdcm_write_call_count++;
    return data_size;
}
static inline int fdcm_erase(fdcm_handle_t *hdl) { (void)hdl; return 0; }
static inline void fdcm_close(fdcm_handle_t *hdl) { (void)hdl; }
