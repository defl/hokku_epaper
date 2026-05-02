"""Spectra 6 wire-format packing and preview colors from palette indices."""
from __future__ import annotations

import numpy as np

from webserver.display_constants import (
    FULL_W,
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


def indices_to_preview_rgb(result_idx: np.ndarray) -> np.ndarray:
    """Palette indices → RGB preview raster (panel memory layout, uint8 H×W×3)."""
    result_idx = np.asarray(result_idx)
    if result_idx.shape != (PANEL_H, FULL_W):
        raise ValueError(
            f"Expected result_idx shape ({PANEL_H}, {FULL_W}), got {result_idx.shape}",
        )
    return PALETTE_PREVIEW_RGB[result_idx]
