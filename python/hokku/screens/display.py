"""Abstract base class for e-paper display specifications.

Each concrete subclass encodes the geometry, palette, and wire-format
packing for one hardware screen model.  The render pipeline accepts a
``Display`` instance rather than importing model-specific constants
directly, so adding a new screen model requires only a new subclass and
a registry entry — nothing else changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
from numpy.typing import NDArray


class Display(ABC):
    """Complete hardware specification for one e-paper screen model."""

    model_id: str
    """``<brand>_<model>`` identifier matching the X-Screen-Model header value."""

    panel_w: int
    """Physical panel width in pixels (before any rotation)."""

    panel_h: int
    """Physical panel height in pixels (before any rotation)."""

    total_bytes: int
    """Total wire-format byte count for one full-screen image."""

    visual_w: int
    """Visible width after mounting rotation (what the viewer sees)."""

    visual_h: int
    """Visible height after mounting rotation (what the viewer sees)."""

    panel_rotated: bool
    """True if panel memory is rotated 90° relative to the visible image.

    Huessen EPF1301 stores a portrait 1200×1600 buffer but is mounted
    landscape, so a landscape render is composed at 1600×1200 then rotated
    -90° into panel memory (``panel_rotated=True``).  The Bigme F7 panel
    memory is natively landscape (800×480 == visible), so no rotation is
    applied (``panel_rotated=False``).  The render pipeline gates its
    rotate/dimension-swap on this flag.
    """

    palette_measured_rgb: NDArray
    """Shape (N, 3) float32 — measured RGB of each ink colour on-panel."""

    drc_anchor_l: tuple[float, float] | None = None
    """(black L\\*, white L\\*) the dynamic-range compressor should target, or None.

    This is the panel's *reachable* lightness range, and it is deliberately
    separate from ``palette_measured_rgb``. The palette drives ink SELECTION,
    where being a few ΔE out barely matters because the gamut dominates. The DRC
    instead decides what range the whole image is squeezed into, and being wrong
    there clips everything past the end — measured at 50 % of one test portrait
    collapsing into flat black.

    ``None`` derives the range from rows 0 and 1 of ``palette_measured_rgb``,
    which is right whenever that table reflects the real panel. Set it explicitly
    when the panel has been measured and the palette has not been re-derived from
    those measurements.
    """

    correction_lut_path: Path | None = None
    """Path to a checked-in 3-D RGB->RGB gamut-correction LUT (.npy, shape
    (N, N, N, 3) float32), or None if this panel has none built yet.

    Built by tools/color_lut_build.py from the gamut_dense measurement phase
    (see docs/screens/<model>/measurements/findings.md). Maps a requested RGB
    (already DRC-compressed, see ImageRenderer._prep_stripe) to a corrected RGB
    that, fed through the shipped dither pipeline, more closely reproduces the
    requested colour's Lab appearance on THIS glass — correcting for gamut/hue
    drift the palette-selection LUT doesn't know about, complementing
    drc_anchor_l (which corrects lightness only). None-safe: renders unchanged
    when unset, same as drc_anchor_l.
    """

    palette_preview_rgb: NDArray
    """Shape (N, 3) uint8 — punchy RGB used for browser preview rendering."""

    palette_nibble: NDArray
    """Shape (N,) uint8 — maps palette index → controller nibble value."""

    @abstractmethod
    def indices_to_panel_bytes(self, result_idx: NDArray) -> bytes:
        """Convert a palette-index raster (panel_h × panel_w) to wire bytes."""

    @abstractmethod
    def panel_bytes_to_indices(self, raw: bytes) -> NDArray:
        """Inverse of ``indices_to_panel_bytes``; raises on unknown nibbles."""

    def indices_to_preview_rgb(self, result_idx: NDArray) -> NDArray:
        """Palette indices (any H×W) → RGB raster using the punchy preview palette."""
        return self.palette_preview_rgb[np.asarray(result_idx, dtype=np.uint8)]
