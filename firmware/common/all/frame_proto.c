#include "frame_proto.h"

/* Reflected IEEE polynomial (0x04C11DB7 bit-reversed). */
#define FRAME_PROTO_CRC32_POLY  0xEDB88320u

uint32_t frame_proto_crc32(uint32_t crc, const uint8_t *data, uint32_t len)
{
    uint32_t c;
    uint32_t i;
    int      bit;

    if (data == 0)
        return crc;

    /* zlib semantics: the running value is stored inverted, so seeding with 0
     * and chaining across calls gives the same answer as one call over the
     * concatenation. */
    c = ~crc;
    for (i = 0; i < len; i++) {
        c ^= data[i];
        for (bit = 0; bit < 8; bit++) {
            /* Branchless: -(c & 1) is 0 or 0xFFFFFFFF. */
            c = (c >> 1) ^ (FRAME_PROTO_CRC32_POLY & (uint32_t)(-(int32_t)(c & 1u)));
        }
    }
    return ~c;
}

uint32_t frame_proto_chunk_count(uint32_t total, uint32_t chunk)
{
    if (chunk == 0u || total == 0u)
        return 0u;
    return (total + chunk - 1u) / chunk;
}

uint32_t frame_proto_chunk_size(uint32_t total, uint32_t chunk, uint32_t index)
{
    uint32_t count = frame_proto_chunk_count(total, chunk);
    uint32_t consumed;

    if (index >= count)
        return 0u;

    consumed = index * chunk;
    if (total - consumed < chunk)
        return total - consumed;   /* short final chunk */
    return chunk;
}
