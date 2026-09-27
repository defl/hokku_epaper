// SoC-agnostic serial frame-upload protocol, shared by ALL hokku firmwares.
// Pure C — no ESP-IDF or XR872 SDK headers, integer-only, fully host-testable.
//
// Normally a screen gets its picture from the server: it wakes, fetches, and
// displays whatever the render pipeline decided. That is the wrong tool for
// bring-up and for colour measurement, where you need an *exact*, known raster
// on the glass, on demand, with no network, no dithering and no server config in
// the loop. This protocol lets a host push a ready-made panel buffer straight
// down the console UART instead.
//
// The device stays a dumb pipe: it holds no patterns, no list and no state. The
// host decides what to display and uploads it, so new test images never require
// a rebuild or a reflash. Everything clever lives host-side, where it is already
// unit-tested (see hokku.screens.*.Display.indices_to_panel_bytes).
//
// Wire exchange (all control lines are ASCII, CRLF-terminated; payload is raw):
//
//     host -> "frame"
//     dev  <- "READY <total_bytes> <chunk_bytes>"
//     host -> <chunk_bytes of raw panel data>
//     dev  <- "K"                                  (one ACK per chunk)
//     ...  repeated for every chunk ...
//     dev  <- "DONE <crc32>"
//     dev  <- "REFRESHED"                          (after the ~30 s panel update)
//
// Chunking is not decoration. The UART RX FIFO is a few bytes deep and the panel
// is fed one byte at a time, so a host that blasts 192 KB unthrottled overruns
// the device long before the picture lands. The per-chunk ACK is the flow
// control; it needs no hardware RTS/CTS, which this board does not wire up.
//
// The CRC32 is IEEE/zlib, so the host can verify with a plain zlib.crc32() and
// the device refuses to display a frame that arrived corrupted rather than
// showing a subtly wrong picture — which, for colour measurement, would be worse
// than showing nothing.
#pragma once

#include <stdint.h>

/* Payload bytes per ACKed chunk. Large enough that the 47 round trips for an
 * 800x480 frame cost little against the ~17 s the bytes themselves take at
 * 115200 baud, small enough to bound the device-side staging buffer. */
#define FRAME_PROTO_CHUNK_BYTES     4096u

/* Single-byte per-chunk replies. */
#define FRAME_PROTO_ACK             'K'
#define FRAME_PROTO_NAK             'X'

/* Per-chunk receive timeout. Generous: a 4096-byte chunk is ~356 ms of wire
 * time at 115200, so this only fires when the host has genuinely gone away. */
#define FRAME_PROTO_RX_TIMEOUT_MS   5000u

/* Control-line prefixes, shared so host and device cannot drift apart. */
#define FRAME_PROTO_READY           "READY"
#define FRAME_PROTO_DONE            "DONE"
#define FRAME_PROTO_REFRESHED       "REFRESHED"

/* Incremental IEEE CRC32, bit-identical to Python's zlib.crc32(data, crc).
 * Seed with 0 and chain across chunks. Bitwise (no lookup table): a 192 KB
 * frame costs ~1.5 M iterations, negligible beside the 17 s of wire time, and
 * it keeps 1 KB of table out of a flash-constrained image. */
uint32_t frame_proto_crc32(uint32_t crc, const uint8_t *data, uint32_t len);

/* Number of chunks needed for *total* bytes, i.e. ceil(total/chunk).
 * Returns 0 when chunk is 0 or total is 0. */
uint32_t frame_proto_chunk_count(uint32_t total, uint32_t chunk);

/* Payload size of chunk *index* (0-based); the final chunk is short when
 * total is not a multiple of chunk. Returns 0 when index is out of range. */
uint32_t frame_proto_chunk_size(uint32_t total, uint32_t chunk, uint32_t index);
