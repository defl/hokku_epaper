"""EXIF-orientation fixtures: one known upright picture, stored every way a camera can.

Every fixture decodes (when the orientation is honoured) to the same UPRIGHT
picture: a landscape canvas split into four solid quadrants of distinct colours.
The stored pixels are that picture put through the *inverse* of the tagged
orientation, so a reader that ignores the tag sees a visibly different layout
and one that applies the wrong transform lands on a different permutation.
Checking which colour sits in which quadrant therefore verifies the full
transform (all 8 orientations are distinguishable), not just the aspect ratio.

The orientation → transform table is written from the EXIF 2.32 spec (TIFF tag
0x0112), deliberately NOT taken from Pillow's exif_transpose — the thing under
test must not also be the oracle. OpenCV's independent libjpeg-based EXIF
handling agrees with it on plain JPEGs (see test_exif_orientation.py).
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import numpy as np
from PIL import ExifTags, Image, PngImagePlugin

UPRIGHT_SIZE = (96, 64)  # (w, h) — landscape, so a swap is visible
# All four are native e-paper palette colours, so they stay distinct through
# the render pipeline's palette mapping (a green here comes out panel-yellow).
QUADRANT_COLOURS = {
    "top_left": (220, 30, 30),  # red
    "top_right": (25, 25, 25),  # black
    "bottom_left": (30, 30, 220),  # blue
    "bottom_right": (230, 220, 30),  # yellow
}
ORIENTATIONS = tuple(range(1, 9))
AXIS_SWAPPING = frozenset({5, 6, 7, 8})

# EXIF spec: how to turn the STORED pixels into the displayed picture, as numpy
# ops on an (h, w, c) array.
_STORED_TO_DISPLAY = {
    1: lambda a: a,
    2: lambda a: a[:, ::-1],  # mirror horizontal
    3: lambda a: a[::-1, ::-1],  # rotate 180
    4: lambda a: a[::-1, :],  # mirror vertical
    5: lambda a: a.transpose(1, 0, 2),  # mirror horizontal + rotate 270 CW (transpose)
    6: lambda a: np.rot90(a, k=-1),  # rotate 90 CW
    7: lambda a: a[::-1, ::-1].transpose(1, 0, 2),  # mirror horizontal + rotate 90 CW
    8: lambda a: np.rot90(a, k=1),  # rotate 270 CW (90 CCW)
}
# Inverses: every op above is its own inverse except the two pure rotations.
_DISPLAY_TO_STORED = {
    **_STORED_TO_DISPLAY,
    6: lambda a: np.rot90(a, k=1),
    8: lambda a: np.rot90(a, k=-1),
}


def upright_array(size: tuple[int, int] = UPRIGHT_SIZE) -> np.ndarray:
    w, h = size
    a = np.zeros((h, w, 3), dtype=np.uint8)
    a[: h // 2, : w // 2] = QUADRANT_COLOURS["top_left"]
    a[: h // 2, w // 2 :] = QUADRANT_COLOURS["top_right"]
    a[h // 2 :, : w // 2] = QUADRANT_COLOURS["bottom_left"]
    a[h // 2 :, w // 2 :] = QUADRANT_COLOURS["bottom_right"]
    return a


def stored_image(orientation: int, size: tuple[int, int] = UPRIGHT_SIZE) -> Image.Image:
    """The raw pixels a camera would write for *orientation*."""
    upright = upright_array(size)
    stored = np.ascontiguousarray(_DISPLAY_TO_STORED[orientation](upright))
    # Self-check the table: stored → display must round-trip to upright.
    assert np.array_equal(_STORED_TO_DISPLAY[orientation](stored), upright), orientation
    return Image.fromarray(stored, "RGB")


def exif_bytes(orientation: int) -> bytes:
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = orientation
    return exif.tobytes()


def xmp_packet(orientation: int) -> str:
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:tiff="http://ns.adobe.com/tiff/1.0/" '
        f'tiff:Orientation="{orientation}"/></rdf:RDF></x:xmpmeta>'
        '<?xpacket end="w"?>'
    )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png_with_chunk_after_idat(img: Image.Image, kind: bytes, data: bytes) -> bytes:
    """A PNG with one extra chunk between the last IDAT and IEND — legal, and
    what some tools write for metadata added after encoding."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()
    iend = raw.rindex(b"IEND") - 4  # back up over the length field
    return raw[:iend] + _png_chunk(kind, data) + raw[iend:]


def _itxt(key: str, text: str) -> bytes:
    return key.encode("latin-1") + b"\0\0\0" + b"\0" + b"\0" + text.encode("utf-8")


def _imagemagick_raw_profile(orientation: int) -> str:
    """The hex-dump EXIF text chunk ImageMagick writes instead of eXIf."""
    payload = b"Exif\x00\x00" + exif_bytes(orientation)
    hexed = payload.hex()
    lines = "\n".join(hexed[i : i + 72] for i in range(0, len(hexed), 72))
    return f"\nexif\n{len(payload):8d}\n{lines}\n"


def write_fixture(
    kind: str, orientation: int, directory: Path, size: tuple[int, int] = UPRIGHT_SIZE
) -> Path:
    """Write the *kind* container holding the *orientation* fixture; return its path."""
    img = stored_image(orientation, size)
    stem = f"{kind}_o{orientation}"
    exif = exif_bytes(orientation)
    if kind == "jpeg":
        path = directory / f"{stem}.jpg"
        img.save(path, quality=95, subsampling=0, exif=exif)
    elif kind == "jpeg_progressive":
        path = directory / f"{stem}.jpg"
        img.save(path, quality=95, subsampling=0, progressive=True, exif=exif)
    elif kind == "jpeg_xmp_only":
        path = directory / f"{stem}.jpg"
        img.save(path, quality=95, subsampling=0, xmp=xmp_packet(orientation).encode())
    elif kind == "mpo":
        # Multi-picture JPEG, as written by many Samsung/Fujifilm cameras — often
        # still named .jpg. Second frame is a throwaway preview.
        path = directory / f"{stem}.jpg"
        img.save(path, format="MPO", save_all=True, append_images=[img.copy()], exif=exif)
    elif kind == "png":
        path = directory / f"{stem}.png"
        img.save(path, exif=exif)
    elif kind == "png_rgba":
        # Alpha takes a separate branch in the renderer.
        path = directory / f"{stem}.png"
        img.convert("RGBA").save(path, exif=exif)
    elif kind == "png_exif_after_idat":
        path = directory / f"{stem}.png"
        path.write_bytes(_png_with_chunk_after_idat(img, b"eXIf", exif))
    elif kind == "png_xmp_only":
        path = directory / f"{stem}.png"
        info = PngImagePlugin.PngInfo()
        info.add_itxt("XML:com.adobe.xmp", xmp_packet(orientation))
        img.save(path, pnginfo=info)
    elif kind == "png_xmp_after_idat":
        path = directory / f"{stem}.png"
        chunk = _itxt("XML:com.adobe.xmp", xmp_packet(orientation))
        path.write_bytes(_png_with_chunk_after_idat(img, b"iTXt", chunk))
    elif kind == "png_imagemagick_profile":
        path = directory / f"{stem}.png"
        info = PngImagePlugin.PngInfo()
        info.add_text("Raw profile type exif", _imagemagick_raw_profile(orientation), zip=True)
        img.save(path, pnginfo=info)
    elif kind == "png_imagemagick_profile_after_idat":
        path = directory / f"{stem}.png"
        text = _imagemagick_raw_profile(orientation).encode("latin-1")
        chunk = b"Raw profile type exif\0\0" + zlib.compress(text)
        path.write_bytes(_png_with_chunk_after_idat(img, b"zTXt", chunk))
    elif kind == "webp":
        path = directory / f"{stem}.webp"
        img.save(path, quality=95, exif=exif)
    elif kind == "webp_lossless":
        path = directory / f"{stem}.webp"
        img.save(path, lossless=True, exif=exif)
    elif kind == "tiff":
        path = directory / f"{stem}.tiff"
        img.save(path, exif=exif)
    elif kind == "tiff_deflate":
        # Compressed TIFFs load through libtiff, a different code path in Pillow.
        path = directory / f"{stem}.tiff"
        img.save(path, compression="tiff_adobe_deflate", exif=exif)
    elif kind == "heif":
        path = directory / f"{stem}.heic"
        img.save(path, quality=95, exif=exif)
    elif kind == "avif":
        path = directory / f"{stem}.avif"
        img.save(path, quality=95, exif=exif)
    elif kind == "jxl":
        path = directory / f"{stem}.jxl"
        img.save(path, lossless=True, exif=exif)
    else:
        raise ValueError(kind)
    return path


FIXTURE_KINDS = (
    "jpeg",
    "jpeg_progressive",
    "jpeg_xmp_only",
    "mpo",
    "png",
    "png_rgba",
    "png_exif_after_idat",
    "png_xmp_only",
    "png_xmp_after_idat",
    "png_imagemagick_profile",
    "png_imagemagick_profile_after_idat",
    "webp",
    "webp_lossless",
    "tiff",
    "tiff_deflate",
    "heif",
    "avif",
    "jxl",
)


def quadrant_means(img: Image.Image) -> dict[str, np.ndarray]:
    """Mean colour of each quadrant's centre patch (edges excluded: lossy bleed)."""
    w, h = img.size
    a = np.asarray(img.convert("RGB"), dtype=np.float64)
    out = {}
    for name, (ry, rx) in {
        "top_left": (0, 0),
        "top_right": (0, 1),
        "bottom_left": (1, 0),
        "bottom_right": (1, 1),
    }.items():
        cy, cx = int((ry + 0.5) * h / 2), int((rx + 0.5) * w / 2)
        dy, dx = max(1, h // 8), max(1, w // 8)
        out[name] = a[cy - dy : cy + dy, cx - dx : cx + dx].reshape(-1, 3).mean(axis=0)
    return out


def upright_mismatch(
    img: Image.Image,
    upright: tuple[int, int] = UPRIGHT_SIZE,
    reference: Image.Image | None = None,
) -> str | None:
    """None if *img* shows the upright picture (any scale); else what is wrong.

    Each quadrant's centre patch is matched to the nearest reference colour,
    which survives lossy compression and resampling. The picture is upright iff
    every quadrant holds its own colour. *upright* is the fixture's upright size,
    for its aspect. Stages that also transform colour (the render pipeline's
    tone/saturation/palette mapping) pass *reference*: the same stage's output
    for an untagged fixture, whose quadrant colours then stand in for the
    originals.
    """
    w, h = img.size
    if (w > h) != (upright[0] > upright[1]):
        return f"wrong aspect: {w}x{h}"
    refs = (
        quadrant_means(reference)
        if reference is not None
        else {k: np.asarray(v, dtype=np.float64) for k, v in QUADRANT_COLOURS.items()}
    )
    found = {}
    for name, patch in quadrant_means(img).items():
        found[name] = min(refs, key=lambda r: float(np.linalg.norm(patch - refs[r])))
    wrong = {k: v for k, v in found.items() if k != v}
    return f"quadrant holds the wrong colour: {wrong}" if wrong else None
