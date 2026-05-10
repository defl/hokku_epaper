"""Display geometry, palette, and Spectra 6 wire-format packing.

Constants describe the EL133UF1 / Spectra 6 panel; pack/unpack functions move
between palette indices (per-pixel uint8 in 0..5) and the device's nibble-packed
wire format.
"""
from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


# Panel geometry — physical halves are 600×1600 each, stitched into 1200×1600.
PANEL_W = 600
PANEL_H = 1600
PANEL_BYTES = PANEL_W * PANEL_H // 2  # two pixels per byte (4-bit nibbles)
TOTAL_BYTES = PANEL_BYTES * 2
FULL_W = PANEL_W * 2

# Visible panel dims after rotation (the e-paper is mounted landscape).
VISUAL_W = 1600
VISUAL_H = 1200


# Measured RGB values of the six on-panel inks (used for Lab→palette LUTs).
PALETTE_MEASURED_RGB = np.array([
    [2,   2,   2  ],   # 0 black
    [190, 200, 200],   # 1 white
    [205, 202, 0  ],   # 2 yellow
    [135, 19,  0  ],   # 3 red
    [5,   64,  158],   # 4 blue
    [39,  102, 60 ],   # 5 green
], dtype=np.float32)

# Punchier RGB used only for browser previews (real ink is duller).
PALETTE_PREVIEW_RGB = np.array([
    [0,   0,   0  ],
    [255, 255, 255],
    [255, 230, 50 ],
    [200, 20,  20 ],
    [30,  80,  200],
    [20,  120, 40 ],
], dtype=np.uint8)

# Maps palette index 0..5 → device nibble. Indexes 4 and 7 are skipped on
# purpose; the controller treats them as undefined.
PALETTE_NIBBLE = np.array([0x0, 0x1, 0x2, 0x3, 0x5, 0x6], dtype=np.uint8)


def indices_to_panel_bytes(result_idx: NDArray[np.uint8]) -> bytes:
    """Palette indices (PANEL_H × FULL_W, uint8) → wire bytes for both panels."""
    if result_idx.shape != (PANEL_H, FULL_W):
        raise ValueError(
            f"Expected ({PANEL_H}, {FULL_W}), got {result_idx.shape}"
        )
    nibbles = PALETTE_NIBBLE[result_idx]
    panel1 = nibbles[:, :PANEL_W]
    panel2 = nibbles[:, PANEL_W:]
    p1 = (panel1[:, 0::2] << 4) | panel1[:, 1::2]
    p2 = (panel2[:, 0::2] << 4) | panel2[:, 1::2]
    raw = p1.astype(np.uint8).tobytes() + p2.astype(np.uint8).tobytes()
    if len(raw) != TOTAL_BYTES:
        raise RuntimeError(f"Expected {TOTAL_BYTES} bytes, got {len(raw)}")
    return raw


_NIBBLE_TO_INDEX = np.full(16, 255, dtype=np.uint8)
for _i, _n in enumerate(PALETTE_NIBBLE):
    _NIBBLE_TO_INDEX[int(_n)] = _i


def panel_bytes_to_indices(raw: bytes) -> NDArray[np.uint8]:
    """Inverse of indices_to_panel_bytes; raises on unknown nibbles."""
    if len(raw) != TOTAL_BYTES:
        raise ValueError(f"Expected {TOTAL_BYTES} bytes, got {len(raw)}")
    mid = PANEL_BYTES
    b1 = np.frombuffer(raw[:mid], dtype=np.uint8).reshape(PANEL_H, PANEL_W // 2)
    b2 = np.frombuffer(raw[mid:], dtype=np.uint8).reshape(PANEL_H, PANEL_W // 2)
    nib1 = np.empty((PANEL_H, PANEL_W), dtype=np.uint8)
    nib1[:, 0::2] = (b1 >> 4) & 0x0F
    nib1[:, 1::2] = b1 & 0x0F
    nib2 = np.empty((PANEL_H, PANEL_W), dtype=np.uint8)
    nib2[:, 0::2] = (b2 >> 4) & 0x0F
    nib2[:, 1::2] = b2 & 0x0F
    nibbles = np.hstack([nib1, nib2])
    out = _NIBBLE_TO_INDEX[nibbles.astype(np.uint16)]
    if np.any(out == 255):
        raise ValueError("Panel bytes contain a nibble not in the device palette")
    return out.astype(np.uint8)


def indices_to_preview_rgb(result_idx: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Palette indices (any H×W) → RGB raster using the punchy preview palette."""
    return PALETTE_PREVIEW_RGB[np.asarray(result_idx, dtype=np.uint8)]
