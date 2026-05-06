"""Spectra 6 wire-format packing and preview colors from palette indices."""
from __future__ import annotations

import numpy as np

from webserver.display_constants import (
    FULL_W,
    PANEL_BYTES,
    PANEL_H,
    PANEL_W,
    PALETTE_NIBBLE,
    PALETTE_PREVIEW_RGB,
    TOTAL_BYTES,
)


def indices_to_panel_bytes(result_idx: np.ndarray) -> bytes:
    """Palette indices (H×W, uint8) → raw bytes for both panel halves."""
    result_idx = np.asarray(result_idx)
    if result_idx.shape != (PANEL_H, FULL_W):
        raise ValueError(
            f"Expected result_idx shape ({PANEL_H}, {FULL_W}), got {result_idx.shape}",
        )
    nibbles = PALETTE_NIBBLE[result_idx]
    panel1_nib = nibbles[:, :PANEL_W]
    panel2_nib = nibbles[:, PANEL_W:]
    panel1_bin = (panel1_nib[:, 0::2] << 4) | panel1_nib[:, 1::2]
    panel2_bin = (panel2_nib[:, 0::2] << 4) | panel2_nib[:, 1::2]
    raw = panel1_bin.astype(np.uint8).tobytes() + panel2_bin.astype(np.uint8).tobytes()
    if len(raw) != TOTAL_BYTES:
        raise RuntimeError(f"Expected {TOTAL_BYTES} panel bytes, got {len(raw)}")
    return raw


def preview_rgb_from_indices(result_idx: np.ndarray) -> np.ndarray:
    """Palette indices (any H×W, uint8) → RGB raster (H×W×3) using :data:`PALETTE_PREVIEW_RGB`."""
    return PALETTE_PREVIEW_RGB[np.asarray(result_idx, dtype=np.uint8)]


def indices_to_preview_rgb(result_idx: np.ndarray) -> np.ndarray:
    """Palette indices → RGB preview raster (panel memory layout, uint8 H×W×3)."""
    result_idx = np.asarray(result_idx)
    if result_idx.shape != (PANEL_H, FULL_W):
        raise ValueError(
            f"Expected result_idx shape ({PANEL_H}, {FULL_W}), got {result_idx.shape}",
        )
    return preview_rgb_from_indices(result_idx)


_NIBBLE_TO_INDEX = np.full(16, 255, dtype=np.uint8)
for _i, _n in enumerate(PALETTE_NIBBLE):
    _NIBBLE_TO_INDEX[int(_n)] = _i


def panel_bytes_to_indices(raw: bytes) -> np.ndarray:
    """Packed panel wire bytes → palette indices (inverse of :func:`indices_to_panel_bytes`)."""
    if len(raw) != TOTAL_BYTES:
        raise ValueError(f"Expected {TOTAL_BYTES} panel bytes, got {len(raw)}")
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
