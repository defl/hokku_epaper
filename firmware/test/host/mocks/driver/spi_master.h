#pragma once
#include <stdint.h>
#include <string.h>

typedef void *spi_device_handle_t;
typedef int   spi_host_device_t;

#define SPI2_HOST          1
#define SPI_DMA_CH_AUTO    0
#define SPI_DEVICE_3WIRE       (1u << 2)
#define SPI_DEVICE_HALFDUPLEX  (1u << 3)
#define SPI_TRANS_VARIABLE_CMD (1u << 2)

typedef struct {
    int mosi_io_num, miso_io_num, sclk_io_num, quadwp_io_num, quadhd_io_num;
    int max_transfer_sz;
} spi_bus_config_t;

typedef struct {
    int command_bits, mode;
    int clock_speed_hz, spics_io_num;
    uint32_t flags;
    int queue_size;
} spi_device_interface_config_t;

typedef struct {
    uint16_t  cmd;
    size_t    length;
    size_t    rxlength;
    uint32_t  flags;
    const void *tx_buffer;
    void      *rx_buffer;
} spi_transaction_t;

typedef struct {
    spi_transaction_t base;
    uint8_t command_bits;
} spi_transaction_ext_t;

static inline int spi_bus_initialize(spi_host_device_t h, const spi_bus_config_t *b, int d) {
    (void)h; (void)b; (void)d; return 0;
}
static inline int spi_bus_add_device(spi_host_device_t h, const spi_device_interface_config_t *c,
                                     spi_device_handle_t *dev) {
    (void)h; (void)c; (void)dev; return 0;
}
static inline int spi_device_polling_transmit(spi_device_handle_t dev, spi_transaction_t *t) {
    (void)dev; (void)t; return 0;
}
static inline int spi_bus_remove_device(spi_device_handle_t dev) { (void)dev; return 0; }
static inline int spi_bus_free(spi_host_device_t h) { (void)h; return 0; }
