"""Display specification for the Bigme F7 (EK79655 / E-Ink Spectra 6 panel).

Panel geometry: 800×480 pixels, natively landscape.
Color depth: 6-color Spectra 6, nibble-packed (EK79655 controller).

The EK79655 drives an E-Ink **Spectra 6** (E6) panel — the SAME ink family and
nibble encoding as the EL133UF1, NOT a 7-color ACeP panel.  Verified against the
OEM's own built-in image (flash 0xb7000): its nibbles land almost entirely on
{0,1,2,3,5,6} with 4 essentially unused — the Spectra 6 code set (4 and 7 skipped).
Decoding that image with the palette below reproduces a coherent, natural-colored
photo; the ACeP palette produced garbage colors.

Nibble → ink (Spectra 6):
  0x0=Black, 0x1=White, 0x2=Yellow, 0x3=Red, 0x5=Blue, 0x6=Green

Panel scan origin: the F7's memory (0,0) is the opposite corner from the render
canvas's top-left, so a full 180° flip is applied in the pack/unpack layer —
otherwise the image shows upside-down when the device is mounted landscape.

palette_measured_rgb: the measured on-glass sRGB of the six E6 inks, taken from
the epdoptimize `spectra6` default palette (paperlesspaper/Utzel-Butzel) — the
calibrated appearance used in a shipping e-ink frame's dither pipeline. These are
muted/shifted from the nominal primaries (dark impure red, olive yellow, muddy
green, grey-cyan white), which is what real E6 glass looks like; they agree
directionally with an independent hand-tuning on a 7.3" Spectra 6 panel. Source:
github.com/Utzel-Butzel/epdoptimize (src/dither/data/default-palettes.json).
Fine-tune against the physical F7 if desired.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from hokku.screens.display import Display


class BigmeF7Display(Display):
    model_id = "bigme_f7"

    panel_w = 800
    panel_h = 480
    total_bytes = 192_000  # 800 × 480 × 4bpp / 8

    visual_w = 800
    visual_h = 480

    panel_rotated = False  # panel memory is natively landscape (800×480)

    # Measured on-glass sRGB of the six Spectra 6 inks (epdoptimize `spectra6`).
    palette_measured_rgb = np.array(
        [
            [31, 34, 38],  # 0 black
            [185, 199, 201],  # 1 white
            [193, 187, 30],  # 2 yellow
            [98, 32, 30],  # 3 red
            [35, 63, 142],  # 4 blue
            [53, 86, 58],  # 5 green
        ],
        dtype=np.float32,
    )

    # Measured on this glass with an X-Rite ColorMunki Photo (ArgyllCMS spotread,
    # reflective 45/0, D65), averaged over 11 readings of each solid ink:
    # black L* 10.21, white L* 68.02 — a contrast ratio of 31.9:1.
    #
    # The full campaign later put 42 readings on each ink across 20 sessions and
    # landed on L* 10.18 / 67.96 (33.0:1). Left unchanged: the difference is
    # 0.03 and 0.06 L*, an order of magnitude inside the 0.35 dE noise floor, so
    # editing these would be churn that reads like a real change in git blame.
    #
    # The palette table above is NOT the source for this. It came from a
    # third-party dataset and puts white at L* 79.26, which is 11 L* beyond
    # anything this panel can actually show. Deriving the DRC range from it
    # compresses images into a range wider than the display, clipping both ends.
    # See docs/screens/bigme_f7/measurements/findings.md.
    drc_anchor_l = (10.21, 68.02)

    palette_preview_rgb = np.array(
        [
            [0, 0, 0],  # black
            [255, 255, 255],  # white
            [255, 230, 50],  # yellow
            [200, 20, 20],  # red
            [30, 80, 200],  # blue
            [20, 120, 40],  # green
        ],
        dtype=np.uint8,
    )

    # Spectra 6 nibble map: palette index → controller nibble. 0x4 and 0x7 are
    # undefined on this controller and are intentionally skipped.
    palette_nibble = np.array([0x0, 0x1, 0x2, 0x3, 0x5, 0x6], dtype=np.uint8)

    def indices_to_panel_bytes(self, result_idx: NDArray) -> bytes:
        """Palette indices (panel_h × panel_w, uint8) → wire bytes."""
        if result_idx.shape != (self.panel_h, self.panel_w):
            raise ValueError(f"Expected ({self.panel_h}, {self.panel_w}), got {result_idx.shape}")
        # Panel scan origin is the opposite corner → 180° flip to display upright.
        result_idx = result_idx[::-1, ::-1]
        nibbles = self.palette_nibble[result_idx]
        # Two pixels per byte: high nibble = even pixel, low nibble = odd pixel.
        packed = (nibbles[:, 0::2] << 4) | nibbles[:, 1::2]
        raw = packed.astype(np.uint8).tobytes()
        if len(raw) != self.total_bytes:
            raise RuntimeError(f"Expected {self.total_bytes} bytes, got {len(raw)}")
        return raw

    def panel_bytes_to_indices(self, raw: bytes) -> NDArray:
        """Inverse of ``indices_to_panel_bytes``; raises on unknown nibbles."""
        if len(raw) != self.total_bytes:
            raise ValueError(f"Expected {self.total_bytes} bytes, got {len(raw)}")
        packed = np.frombuffer(raw, dtype=np.uint8).reshape(self.panel_h, self.panel_w // 2)
        nibbles = np.empty((self.panel_h, self.panel_w), dtype=np.uint8)
        nibbles[:, 0::2] = (packed >> 4) & 0x0F
        nibbles[:, 1::2] = packed & 0x0F
        nibble_to_index = np.full(16, 255, dtype=np.uint8)
        for i, n in enumerate(self.palette_nibble):
            nibble_to_index[int(n)] = i
        out = nibble_to_index[nibbles.astype(np.uint16)]
        if np.any(out == 255):
            raise ValueError("Panel bytes contain a nibble not in the device palette")
        # Undo the 180° scan-origin flip applied when packing.
        return out.astype(np.uint8)[::-1, ::-1]
