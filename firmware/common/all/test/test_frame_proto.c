/*
 * frame_proto must agree with the host byte-for-byte, or a frame that arrived
 * fine gets rejected (or worse, a corrupted one gets displayed and silently
 * poisons a colour measurement). Two things are pinned here:
 *
 *   1. the CRC32 is really IEEE/zlib, so Python's zlib.crc32 matches — checked
 *      against published vectors AND against values computed by zlib itself;
 *   2. the chunk arithmetic covers the frame exactly, including the short final
 *      chunk, so device and host never disagree on how many bytes are in flight.
 */
#include <string.h>

#include "frame_proto.c"
#include "test_harness.h"

/* Values produced by Python: zlib.crc32(b"...") — the exact call the host tool
 * makes. If these ever disagree the two ends have drifted. */
static void test_crc32_matches_zlib(void)
{
    CHECK(frame_proto_crc32(0, (const uint8_t *)"", 0) == 0x00000000u, "empty");
    CHECK(frame_proto_crc32(0, (const uint8_t *)"a", 1) == 0xE8B7BE43u, "a");
    CHECK(frame_proto_crc32(0, (const uint8_t *)"abc", 3) == 0x352441C2u, "abc");
    CHECK(frame_proto_crc32(0, (const uint8_t *)"123456789", 9) == 0xCBF43926u,
          "check vector 123456789");
    CHECK(frame_proto_crc32(0, (const uint8_t *)"The quick brown fox jumps over the lazy dog", 43)
              == 0x414FA339u,
          "quick brown fox");
}

/* The device CRCs chunk by chunk as bytes arrive; the host CRCs the whole
 * buffer in one call. Those must be the same number. */
static void test_crc32_chains_across_chunks(void)
{
    uint8_t  buf[1000];
    uint32_t whole, chained;
    int      i;

    for (i = 0; i < 1000; i++)
        buf[i] = (uint8_t)(i * 7 + 13);

    whole = frame_proto_crc32(0, buf, 1000);

    chained = 0;
    chained = frame_proto_crc32(chained, buf, 1);
    chained = frame_proto_crc32(chained, buf + 1, 255);
    chained = frame_proto_crc32(chained, buf + 256, 744);

    CHECK(whole == chained, "chunked CRC must equal whole-buffer CRC");
}

static void test_crc32_null_is_identity(void)
{
    CHECK(frame_proto_crc32(0x12345678u, 0, 10) == 0x12345678u, "NULL data passes crc through");
}

/* 192000 / 4096 = 46 remainder 3584, so a real F7 frame is 47 chunks with a
 * short tail — the case most likely to be got wrong on one side only. */
static void test_chunking_of_a_real_frame(void)
{
    const uint32_t total = 192000u;
    const uint32_t chunk = FRAME_PROTO_CHUNK_BYTES;
    uint32_t count = frame_proto_chunk_count(total, chunk);
    uint32_t sum = 0;
    uint32_t i;

    CHECK(count == 47u, "192000 B in 4096 B chunks is 47 chunks");
    for (i = 0; i < count; i++)
        sum += frame_proto_chunk_size(total, chunk, i);
    CHECK(sum == total, "chunk sizes must sum to exactly the frame size");
    CHECK(frame_proto_chunk_size(total, chunk, count - 1) == 3584u, "short final chunk");
    CHECK(frame_proto_chunk_size(total, chunk, 0) == chunk, "first chunk is full");
    CHECK(frame_proto_chunk_size(total, chunk, count) == 0u, "past the end is 0");
}

static void test_chunking_exact_multiple(void)
{
    CHECK(frame_proto_chunk_count(8192u, 4096u) == 2u, "exact multiple: 2 chunks");
    CHECK(frame_proto_chunk_size(8192u, 4096u, 1) == 4096u, "last chunk is full, not 0");
    CHECK(frame_proto_chunk_size(8192u, 4096u, 2) == 0u, "past the end is 0");
}

static void test_chunking_degenerate(void)
{
    CHECK(frame_proto_chunk_count(0u, 4096u) == 0u, "no bytes, no chunks");
    CHECK(frame_proto_chunk_count(100u, 0u) == 0u, "zero chunk size is refused");
    CHECK(frame_proto_chunk_size(100u, 0u, 0) == 0u, "zero chunk size is refused");
    CHECK(frame_proto_chunk_count(1u, 4096u) == 1u, "one byte still needs a chunk");
    CHECK(frame_proto_chunk_size(1u, 4096u, 0) == 1u, "and that chunk is 1 byte");
}

int main(void)
{
    test_crc32_matches_zlib();
    test_crc32_chains_across_chunks();
    test_crc32_null_is_identity();
    test_chunking_of_a_real_frame();
    test_chunking_exact_multiple();
    test_chunking_degenerate();
    TEST_MAIN_END();
}
