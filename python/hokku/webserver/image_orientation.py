"""Displayed (EXIF-oriented) size of an image, read from its header only.

The render path decodes through ``ImageOps.exif_transpose``, so every size the
server records about a source must be the *displayed* one, or orientation
filtering and preview framing disagree with what actually reaches the panel
(issue #40). This module answers that without decoding pixel data, which the
ingest path must never do (see the decode-budget gate in the image manager).

Where the orientation lives, per format, as Pillow sees it:

* JPEG / MPO / WebP / AVIF / JXL — EXIF (or XMP ``tiff:Orientation``) is parsed
  at open; ``img.size`` is the stored size. Swap for orientations 5-8.
* TIFF — Pillow already reports the displayed size at open and transposes on
  load, so ``img.size`` is final. Swapping again would undo it.
* HEIF — libheif applies the container rotation on decode and pillow_heif
  resets the EXIF tag to 1, so ``img.size`` is final.
* PNG — ``getexif()`` decodes the whole image when no eXIf chunk precedes IDAT,
  because EXIF/XMP chunks may legally follow the pixel data (and the renderer,
  which does load, honours them there). So the chunk list is walked instead,
  seeking over IDAT, and whatever metadata it finds is handed to Pillow's own
  ``getexif()`` — its EXIF-then-XMP precedence is reused, not re-implemented.

tests/test_exif_orientation.py pins every one of these against the real render
path, across all 8 orientations.
"""

from __future__ import annotations

import struct
import zlib
from typing import IO

from PIL import ExifTags, Image, ImageFile

# EXIF Orientation values (transpose, rotate 90/270, transverse) whose display
# form swaps width and height.
_AXIS_SWAPPING = frozenset({5, 6, 7, 8})

# Formats whose Pillow plugin reports the already-oriented size at open.
_SIZE_ALREADY_ORIENTED = frozenset({"TIFF", "HEIF"})

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Text-chunk keywords Pillow's getexif() takes orientation from: XMP, and the
# hex-encoded EXIF ImageMagick writes.
_PNG_TEXT_KEYS = frozenset({"XML:com.adobe.xmp", "Raw profile type exif"})
# Metadata chunks larger than this are skipped rather than read: an orientation
# tag never needs it, and a hostile file must not make the header read allocate.
_MAX_METADATA_CHUNK = 4 * 1024 * 1024


def displayed_size(img: ImageFile.ImageFile) -> tuple[int, int]:
    """(width, height) of *img* as the render path will show it. No decode."""
    w, h = img.size
    if img.format in _SIZE_ALREADY_ORIENTED:
        return w, h
    if _orientation(img) in _AXIS_SWAPPING:
        return h, w
    return w, h


def _orientation(img: ImageFile.ImageFile) -> int | None:
    if img.format == "PNG":
        _load_png_metadata_without_decoding(img)
        # The base-class getexif(): PngImageFile's override decodes the image
        # whenever eXIf is absent, to look for chunks we have just read.
        return Image.Image.getexif(img).get(ExifTags.Base.Orientation)
    return img.getexif().get(ExifTags.Base.Orientation)


def _load_png_metadata_without_decoding(img: ImageFile.ImageFile) -> None:
    """Copy orientation-bearing chunks from anywhere in the file into ``img.info``.

    Chunks before IDAT are already there (Pillow parses them at open); this
    also finds the ones after it, which Pillow only reaches by decoding. The
    first occurrence wins, as it does in Pillow.
    """
    fp = img.fp
    if fp is None:
        return
    pos = fp.tell()
    try:
        fp.seek(0)
        if fp.read(8) != _PNG_SIGNATURE:
            return
        for kind, data in _png_metadata_chunks(fp):
            if kind == b"eXIf":
                img.info.setdefault("exif", b"Exif\x00\x00" + data)
                continue
            key, text = _png_text(kind, data)
            if key in _PNG_TEXT_KEYS and text is not None:
                img.info.setdefault(key, text)
    except (OSError, struct.error, zlib.error, UnicodeDecodeError, ValueError):
        pass  # malformed metadata: behave as if absent, like the renderer would
    finally:
        fp.seek(pos)


def _png_metadata_chunks(fp: IO[bytes]):
    """Yield (type, data) for eXIf and text chunks; seek past everything else."""
    while True:
        header = fp.read(8)
        if len(header) < 8:
            return
        length, kind = struct.unpack(">I4s", header)
        if kind == b"IEND":
            return
        if kind in (b"eXIf", b"iTXt", b"tEXt", b"zTXt") and length <= _MAX_METADATA_CHUNK:
            data = fp.read(length)
            fp.seek(4, 1)  # CRC
            yield kind, data
        else:
            fp.seek(length + 4, 1)  # data + CRC; IDAT is never read


def _png_text(kind: bytes, data: bytes) -> tuple[str, str | None]:
    """(keyword, text) of a tEXt/zTXt/iTXt chunk; text is only decoded for a
    keyword in _PNG_TEXT_KEYS, since nothing else is needed."""
    raw_key, sep, rest = data.partition(b"\0")
    key = raw_key.decode("latin-1")
    if not sep or key not in _PNG_TEXT_KEYS:
        return key, None
    if kind == b"tEXt":
        return key, rest.decode("latin-1")
    if kind == b"zTXt":
        return key, _inflate(rest[1:]).decode("latin-1")
    # iTXt: compression flag, method, language\0, translated keyword\0, text
    compressed = rest[0] == 1
    _lang, _, rest = rest[2:].partition(b"\0")
    _tk, _, text = rest.partition(b"\0")
    return key, (_inflate(text) if compressed else text).decode("utf-8")


def _inflate(data: bytes) -> bytes:
    """zlib-decompress with an output cap — a text chunk must not be a zip bomb."""
    d = zlib.decompressobj()
    out = d.decompress(data, _MAX_METADATA_CHUNK)
    if d.unconsumed_tail:
        raise ValueError("compressed PNG text chunk exceeds the metadata cap")
    return out
