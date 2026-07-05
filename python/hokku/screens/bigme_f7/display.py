"""Display specification for the Bigme F7 (EK79655 / 7-color ACeP panel).

Panel geometry: 800×480 pixels, no rotation.
Color depth: 7-color ACeP, nibble-packed (EK79655 controller).

Nibble encoding confirmed from multiple open-source projects
(protivinsky/photoink, esphome PR#6380):
  0x00=Black, 0x01=White, 0x02=Green, 0x03=Blue,
  0x04=Red,   0x05=Yellow, 0x06=Orange

palette_measured_rgb values are nominal starting points — NOT yet
photographically measured from the physical device.  Calibrate and
replace once measured from a physical F7 panel.
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

    # Nominal RGB for the seven ACeP inks (measure from device and replace).
    palette_measured_rgb = np.array(
        [
            [0, 0, 0],  # 0 black
            [255, 255, 255],  # 1 white
            [0, 255, 0],  # 2 green
            [0, 0, 255],  # 3 blue
            [255, 0, 0],  # 4 red
            [255, 255, 0],  # 5 yellow
            [255, 127, 0],  # 6 orange
        ],
        dtype=np.float32,
    )

    palette_preview_rgb = np.array(
        [
            [0, 0, 0],
            [255, 255, 255],
            [0, 200, 0],
            [0, 0, 200],
            [220, 0, 0],
            [240, 240, 0],
            [255, 140, 0],
        ],
        dtype=np.uint8,
    )

    # EK79655 nibble map: index → nibble value (0x00–0x06).
    palette_nibble = np.array([0x0, 0x1, 0x2, 0x3, 0x4, 0x5, 0x6], dtype=np.uint8)

    def indices_to_panel_bytes(self, result_idx: NDArray) -> bytes:
        """Palette indices (panel_h × panel_w, uint8) → wire bytes."""
        if result_idx.shape != (self.panel_h, self.panel_w):
            raise ValueError(f"Expected ({self.panel_h}, {self.panel_w}), got {result_idx.shape}")
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
        return out.astype(np.uint8)
