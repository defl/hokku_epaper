#!/usr/bin/env python3
"""Read a Raspberry Pi DNG into linear RGB, with no ISP in the way.

The JPEG the camera hands back has been through automatic white balance, a
colour-correction matrix, a tone curve and 8-bit quantisation. Every one of
those is a nonlinearity between the ink and the number, and together they are
what held the rig's colour fit at 7.9 dE: an 11-term polynomial fitted to 80
patches was still *under*-fitting, and raising the order made cross-validation
worse rather than better, which is the signature of a relationship that is not
smooth in camera RGB rather than of too little model.

The DNG carries the sensor's own values instead: linear, 10-bit, one colour per
photosite, nothing applied. Fitting that to measured XYZ is close to the linear
problem it ought to be.

It also recovers exposure. Through the JPEG's tone curve the panel looked
well exposed while the raw plane was peaking at 281 of 1023 — the curve was
lifting the midtones and hiding nearly two stops of unused range.

No new dependency. A Pi DNG is an uncompressed TIFF: a thumbnail in IFD0 and
the raw plane in a SubIFD, single strip, 16-bit little-endian containers holding
10-bit values. That is a short parser, and a short parser that reads the CFA
pattern and levels out of the file is safer than a long dependency that assumes
them.

Demosaic is 2x2 binning, not interpolation, and deliberately so. Every consumer
of this data averages over blocks anyway — chart patches, and dithered image
regions — so interpolating to full resolution would invent detail, cost time,
and then be averaged away. Binning halves the resolution to 1296x972 and returns
one honest measurement per photosite quad.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# TIFF field types -> (struct code, bytes each). Type 13 is IFD, a LONG offset.
_TYPES = {
    1: ("B", 1),
    2: ("c", 1),
    3: ("H", 2),
    4: ("I", 4),
    5: ("II", 8),
    6: ("b", 1),
    7: ("B", 1),
    8: ("h", 2),
    9: ("i", 4),
    10: ("ii", 8),
    11: ("f", 4),
    12: ("d", 8),
    13: ("I", 4),
}

_SUBIFDS = 330
_IMAGE_WIDTH = 256
_IMAGE_LENGTH = 257
_BITS_PER_SAMPLE = 258
_COMPRESSION = 259
_STRIP_OFFSETS = 273
_STRIP_BYTE_COUNTS = 279
_NEW_SUBFILE_TYPE = 254
_CFA_PATTERN = 33422
_CFA_DIM = 33421
_BLACK_LEVEL = 50714
_WHITE_LEVEL = 50717

# DNG CFAPattern colour codes.
_RED, _GREEN, _BLUE = 0, 1, 2


@dataclass(frozen=True)
class Raw:
    """Linear RGB from one raw capture, plus what it took to get there."""

    rgb: np.ndarray  # (h/2, w/2, 3) float32, 0..1 after black/white levels
    black: float
    white: float
    saturated_fraction: float
    shape_full: tuple[int, int]


def _read_ifd(buf: bytes, offset: int, bo: str) -> dict[int, tuple[int, int, bytes, int]]:
    """tag -> (type, count, raw value bytes, offset-or-inline)."""
    (count,) = struct.unpack_from(bo + "H", buf, offset)
    out: dict[int, tuple[int, int, bytes, int]] = {}
    for i in range(count):
        entry = offset + 2 + i * 12
        tag, typ, cnt = struct.unpack_from(bo + "HHI", buf, entry)
        _fmt, size = _TYPES.get(typ, ("B", 1))
        total = size * cnt
        (ptr,) = struct.unpack_from(bo + "I", buf, entry + 8)
        raw = buf[entry + 8 : entry + 8 + total] if total <= 4 else buf[ptr : ptr + total]
        out[tag] = (typ, cnt, raw, ptr)
    return out


def _values(entry: tuple[int, int, bytes, int], bo: str) -> list[float]:
    typ, cnt, raw, _ptr = entry
    fmt, size = _TYPES.get(typ, ("B", 1))
    if typ in (5, 10):  # RATIONAL / SRATIONAL: numerator, denominator pairs
        nums = struct.unpack(bo + fmt * cnt, raw[: size * cnt])
        return [nums[i] / (nums[i + 1] or 1) for i in range(0, len(nums), 2)]
    return list(struct.unpack(bo + fmt * cnt, raw[: size * cnt]))


def read_dng(path: Path | str) -> Raw:
    """Parse a Pi DNG and return black-subtracted, normalised linear RGB."""
    buf = Path(path).read_bytes()
    if buf[:2] not in (b"II", b"MM"):
        raise ValueError(f"{path}: not a TIFF/DNG")
    bo = "<" if buf[:2] == b"II" else ">"
    (first,) = struct.unpack_from(bo + "I", buf, 4)
    ifd0 = _read_ifd(buf, first, bo)

    # The raw plane lives in a SubIFD; IFD0 is the embedded preview thumbnail.
    if _SUBIFDS not in ifd0:
        raise ValueError(f"{path}: no SubIFD — not a Pi DNG?")
    sub_offsets = _values(ifd0[_SUBIFDS], bo)
    raw_ifd = None
    for off in sub_offsets:
        cand = _read_ifd(buf, int(off), bo)
        # NewSubfileType 0 marks the full-resolution image rather than a reduced one.
        if _NEW_SUBFILE_TYPE not in cand or _values(cand[_NEW_SUBFILE_TYPE], bo)[0] == 0:
            raw_ifd = cand
            break
    if raw_ifd is None:
        raise ValueError(f"{path}: no full-resolution SubIFD")

    if _values(raw_ifd[_COMPRESSION], bo)[0] != 1:
        raise ValueError(f"{path}: compressed DNG, this reader handles uncompressed only")
    width = int(_values(raw_ifd[_IMAGE_WIDTH], bo)[0])
    height = int(_values(raw_ifd[_IMAGE_LENGTH], bo)[0])
    bits = int(_values(raw_ifd[_BITS_PER_SAMPLE], bo)[0])
    if bits != 16:
        raise ValueError(f"{path}: {bits}-bit samples, expected 16-bit containers")

    offsets = [int(v) for v in _values(raw_ifd[_STRIP_OFFSETS], bo)]
    counts = [int(v) for v in _values(raw_ifd[_STRIP_BYTE_COUNTS], bo)]
    payload = b"".join(buf[o : o + c] for o, c in zip(offsets, counts))
    plane = np.frombuffer(payload, dtype=bo + "u2", count=width * height)
    plane = plane.reshape(height, width).astype(np.float32)

    white = float(_values(raw_ifd[_WHITE_LEVEL], bo)[0]) if _WHITE_LEVEL in raw_ifd else 1023.0
    black = float(np.mean(_values(raw_ifd[_BLACK_LEVEL], bo))) if _BLACK_LEVEL in raw_ifd else 0.0

    # Saturation is measured BEFORE normalising, and reported rather than fixed:
    # a clipped photosite carries no information about how far past the top it
    # went, so the only safe response is to know it happened and shorten the
    # exposure. Silently clamping is what made the first colour fit unexplainable.
    saturated = float(np.mean(plane >= white - 1))

    scale = max(white - black, 1.0)
    norm = np.clip((plane - black) / scale, 0.0, None)

    pattern = [int(v) for v in _values(raw_ifd[_CFA_PATTERN], bo)]
    dim = [int(v) for v in _values(raw_ifd[_CFA_DIM], bo)] if _CFA_DIM in raw_ifd else [2, 2]
    if dim != [2, 2] or len(pattern) != 4:
        raise ValueError(f"{path}: CFA pattern {pattern} dim {dim} is not a 2x2 Bayer")

    # Read the mosaic from the file rather than assuming one. This sensor is
    # GBRG, but the same code then handles a module that is not.
    planes: dict[int, list[np.ndarray]] = {_RED: [], _GREEN: [], _BLUE: []}
    for i, colour in enumerate(pattern):
        planes[colour].append(norm[i // 2 :: 2, i % 2 :: 2])
    rgb = np.stack(
        [np.mean(planes[c], axis=0) for c in (_RED, _GREEN, _BLUE)],
        axis=-1,
    ).astype(np.float32)

    return Raw(
        rgb=rgb,
        black=black,
        white=white,
        saturated_fraction=saturated,
        shape_full=(height, width),
    )


def summarise(raw: Raw) -> str:
    peak = float(np.percentile(raw.rgb, 99.9))
    return (
        f"{raw.shape_full[1]}x{raw.shape_full[0]} -> {raw.rgb.shape[1]}x{raw.rgb.shape[0]}  "
        f"black {raw.black:.0f} white {raw.white:.0f}  "
        f"p99.9 {peak:.3f} of 1.0  saturated {100 * raw.saturated_fraction:.3f}%"
    )
