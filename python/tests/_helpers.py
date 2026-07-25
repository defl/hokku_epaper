"""Shared test helpers."""

from __future__ import annotations

import contextlib
import re
import struct
import zlib
from pathlib import Path

from PIL import Image

from hokku.webserver.image_renderer import DECODE_BUDGET_PIXELS, MAX_IMAGE_PIXELS

# Register the HEIF opener so header reads below can size .heic/.heif fixtures
# (conftest does this too for the test session; doing it here keeps the helper
# self-contained and idempotent).
with contextlib.suppress(Exception):  # pragma: no cover - plugin optional
    from pillow_heif import register_heif_opener

    register_heif_opener()

# Formats libjpeg can shrink-on-load (DCT scaling via PIL draft), so a large one
# still decodes small and is a valid render fixture. Everything else decodes
# full-size and is bounded by the decode budget.
_SHRINK_ON_LOAD_FORMATS = {"JPEG", "MPO"}


def is_oversize_fixture(p: Path) -> bool:
    """True if *p* is a fixture the render pipeline refuses to decode.

    Two kinds are skipped by tests that decode every image in ``images/test/``:

      * decompression bombs (e.g. ``synth_black_10000x10000.png``) above
        ``MAX_IMAGE_PIXELS`` — rejected at header read;
      * un-draftable sources above ``DECODE_BUDGET_PIXELS`` (e.g. a 38.9 MP HEIF
        panorama) — rejected post-draft so one image can't OOM the Pi. A large
        JPEG is NOT oversize: it shrink-on-loads under the budget.

    Mirrors ``open_image_for_render``'s accept/reject so the render-iterating
    suites stay in sync with what the appliance will actually process.
    """
    # Fast path: filename encodes dims (synthetic bombs — avoids opening them).
    m = re.search(r"(\d+)x(\d+)", p.name.lower())
    if m:
        try:
            if int(m.group(1)) * int(m.group(2)) > MAX_IMAGE_PIXELS:
                return True
        except ValueError:
            pass
    # Header-only actual-dimension check (no pixel decode).
    try:
        with Image.open(p) as im:
            pixels = im.size[0] * im.size[1]
            fmt = (im.format or "").upper()
    except Exception:
        # Unreadable here (e.g. .jxl/.svg go through other loaders) — leave it to
        # the individual test rather than guessing.
        return False
    if pixels > MAX_IMAGE_PIXELS:
        return True
    return fmt not in _SHRINK_ON_LOAD_FORMATS and pixels > DECODE_BUDGET_PIXELS


def make_declared_size_png(width: int, height: int) -> bytes:
    """Forge a PNG whose IHDR declares (width, height) but whose IDAT is empty.

    A few hundred bytes on disk, yet PIL's header parse reports the full declared
    dimensions — the shape of a decompression bomb, and a cheap way to exercise
    header-time size guards (bomb cap, decode budget) WITHOUT materialising a huge
    buffer. The IDAT won't decode; guards must reject on the header, before any
    decode, so that never matters.
    """

    def _crc(chunk_type: bytes, data: bytes) -> bytes:
        return struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + _crc(b"IHDR", ihdr_data)
    idat_data = zlib.compress(b"")
    idat = struct.pack(">I", len(idat_data)) + b"IDAT" + idat_data + _crc(b"IDAT", idat_data)
    iend = struct.pack(">I", 0) + b"IEND" + _crc(b"IEND", b"")
    return sig + ihdr + idat + iend
