# Panel cache compression investigation

This document records the compression benchmark run on `_panel.bin` cache files
(May 2026) and its conclusions. It is here so the tradeoffs don't have to be
re-derived from scratch if the feature is ever implemented.

---

## What the files are

Each `_panel.bin` file is exactly 960,000 bytes (938 KB) of 4-bit packed nibble
data: two pixels per byte, palette indices 0–5 for the six Spectra 6 colours.
Because only 6 of the 16 possible nibble values are ever used, and because
dithered images have large regions of spatially-correlated colour, the files are
highly compressible.

---

## Benchmark methodology

- **Machine A:** Windows 11 developer desktop (x86-64, modern Intel/AMD, DDR4).
- **Machine B:** Raspberry Pi Zero 2 W — Cortex-A53 @ 1 GHz, 512 MB LPDDR2
  (the production deployment target).
- **Data:** real `_panel.bin` files from 10 different uploaded images.
  Desktop results are averaged across all 10; Pi results used one representative
  file. Compression ratios are reproducible (deterministic); only timing differs
  between machines.
- **Iterations:** 5 per codec; median reported.
- **Codecs tested:** zstd (via `python3-zstd` apt package), zlib, gzip, bz2,
  lzma — all Python stdlib except zstd.

---

## Results

### Desktop (x86-64)

| Codec   | Compressed | Ratio | Space saved | Compress | Decompress |
|---------|-----------|-------|-------------|----------|------------|
| zstd-1  | 236 KB    | 4.4×  | 74.8%       | 2 ms     | 0.6 ms     |
| zstd-9  | 232 KB    | 4.5×  | 75.2%       | 14 ms    | 0.7 ms     |
| zlib-1  | 265 KB    | 3.9×  | 71.7%       | 10 ms    | 2 ms       |
| zlib-6  | 239 KB    | 4.3×  | 74.5%       | 70 ms    | 2 ms       |
| zlib-9  | 234 KB    | 4.4×  | 75.1%       | 292 ms   | 2 ms       |
| bz2-9   | 222 KB    | 4.7×  | 76.3%       | 39 ms    | 15 ms      |
| lzma-6  | 215 KB    | 4.8×  | 77.0%       | 151 ms   | 9 ms       |
| lz4     | 400 KB    | 2.6×  | 57.4%       | 1 ms     | 0.3 ms     |

### Pi Zero 2 W (Cortex-A53, production target)

| Codec   | Compressed | Ratio | Space saved | Compress | Decompress |
|---------|-----------|-------|-------------|----------|------------|
| zstd-1  | 290 KB    | 3.2×  | 69.0%       | 38 ms    | 6 ms       |
| zstd-9  | 290 KB    | 3.2×  | 69.1%       | 245 ms   | 11 ms      |
| zlib-1  | 323 KB    | 2.9×  | 65.5%       | 91 ms    | 18 ms      |
| zlib-6  | 299 KB    | 3.1×  | 68.1%       | 666 ms   | 16 ms      |
| bz2-9   | 283 KB    | 3.3×  | 69.7%       | 481 ms   | **197 ms** |
| lzma-0  | 310 KB    | 3.0×  | 66.9%       | 386 ms   | 86 ms      |

The Pi is roughly **9–12× slower** than the desktop for compression and
decompression — consistent with the clock-speed and microarchitecture gap
(Cortex-A53 is in-order, 1 GHz; no AVX2; narrower SIMD).

Compressed sizes differ slightly between the two rows because the desktop
figures are averaged across 10 images while the Pi figures come from one
image — the ratio is data-dependent, not machine-dependent.

---

## Interpretation

**Is the space saving worth it?**
Yes. Files shrink by 65–75%, from ~938 KB to ~230–325 KB each. For a library
of a few hundred images on an SD card, that is the difference between ~200 MB
and ~60 MB. The SD card is rarely the bottleneck, but the saving also reduces
the data that must be held in memory and transferred over the internal bus on
every panel serve.

**Does compression add meaningful latency?**

*Write path (after dithering):* Compression happens once, inside
`_on_render_done`, after a dithering job that itself takes several seconds.
Even 666 ms (zlib-6 on Pi) is buried in that noise.

*Read path (panel serve to ESP32):* `panel_bytes()` is called on every refresh
request — roughly once per scheduled refresh cycle (every 30 minutes by
default) or on manual demand. The decompression adds:

- **zstd-1:** 6 ms — imperceptible.
- **zlib-1:** 18 ms — imperceptible.
- **bz2-9:** 197 ms — noticeable; blocks the server thread for nearly 0.2 s on
  every panel fetch. **Reject.**
- **lzma:** 86 ms — marginal; probably acceptable but not justified given bz2
  achieves better ratio with similar compress time.

---

## Recommendation

**zstd-1** is the best codec: faster to compress than zlib-1 (38 ms vs 91 ms),
three times faster to decompress (6 ms vs 18 ms), and slightly better
compression ratio. The only cost is a new dependency.

**Dependency situation on the Pi:**
- System package: `python3-zstd` (apt, `import zstd`) — confirmed present in
  Debian Trixie (`python3-zstd 1.5.5.1-1+b4`). Would be added to the `.deb`'s
  `Depends:` field and to `requirements.txt` as `zstandard` (the pip name uses
  a different import, `import zstandard`, with a richer API — the two are not
  the same package).
- If adding a dependency is undesirable: **zlib-1** (Python stdlib, zero new
  deps) is the runner-up — 91 ms compress, 18 ms decompress, 65% savings.

**bz2 and lzma should not be used** due to slow decompression on the Pi
(197 ms and 86 ms respectively), which would block every panel serve.

---

## What implementation would touch

If this feature is built, the changes are confined to
`webserver/hokku_server/image_manager_abstract.py`:

- `_PANEL_SUFFIX` — change extension (e.g. `_panel.bin.zst`) so the scrubber
  recognises the new format and old uncompressed files are treated as orphans
  and cleaned up automatically.
- `_on_render_done` — compress `panel_bytes` before `write_bytes()`.
- `panel_bytes()` — decompress after `read_bytes()`, then validate
  `len(decompressed) == TOTAL_BYTES`.
- `_KNOWN_SUFFIXES` — add the new suffix.

No changes are needed in the Flask layer, the ESP32 firmware, or the render
worker — the panel bytes are decompressed in memory before being sent over HTTP,
so the wire format is unchanged.
