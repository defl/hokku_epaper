"""Display specification for the Huessen EPF1301 (EL133UF1 / Spectra 6 panel).

Panel geometry: two physical 600×1600 halves stitched to 1200×1600.
Mounted in landscape, so the visible area is 1600×1200.
Color depth: 6-color Spectra 6, nibble-packed (UC8179C controller).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from hokku.screens.display import Display


class HuessenEpf1301Display(Display):
    model_id = "huessen_epf1301"

    panel_w = 1200  # FULL_W — two 600-wide halves stitched
    panel_h = 1600  # PANEL_H
    total_bytes = 960_000  # two 600×1600 panels × (600 × 1600 / 2 bytes each)

    visual_w = 1600  # after 90° rotation
    visual_h = 1200

    # Measured RGB values of the six on-panel inks (used for Lab→palette LUTs).
    palette_measured_rgb = np.array(
        [
            [2, 2, 2],  # 0 black
            [190, 200, 200],  # 1 white
            [205, 202, 0],  # 2 yellow
            [135, 19, 0],  # 3 red
            [5, 64, 158],  # 4 blue
            [39, 102, 60],  # 5 green
        ],
        dtype=np.float32,
    )

    # Punchier RGB used only for browser previews (real ink is duller).
    palette_preview_rgb = np.array(
        [
            [0, 0, 0],
            [255, 255, 255],
            [255, 230, 50],
            [200, 20, 20],
            [30, 80, 200],
            [20, 120, 40],
        ],
        dtype=np.uint8,
    )

    # Maps palette index 0..5 → device nibble.
    # Indexes 4 and 7 are skipped on purpose; the controller treats them as undefined.
    palette_nibble = np.array([0x0, 0x1, 0x2, 0x3, 0x5, 0x6], dtype=np.uint8)

    _PANEL_W_HALF = 600  # each physical half-panel

    def indices_to_panel_bytes(self, result_idx: NDArray) -> bytes:
        """Palette indices (panel_h × panel_w, uint8) → wire bytes for both panels."""
        if result_idx.shape != (self.panel_h, self.panel_w):
            raise ValueError(f"Expected ({self.panel_h}, {self.panel_w}), got {result_idx.shape}")
        nibbles = self.palette_nibble[result_idx]
        panel1 = nibbles[:, : self._PANEL_W_HALF]
        panel2 = nibbles[:, self._PANEL_W_HALF :]
        p1 = (panel1[:, 0::2] << 4) | panel1[:, 1::2]
        p2 = (panel2[:, 0::2] << 4) | panel2[:, 1::2]
        raw = p1.astype(np.uint8).tobytes() + p2.astype(np.uint8).tobytes()
        if len(raw) != self.total_bytes:
            raise RuntimeError(f"Expected {self.total_bytes} bytes, got {len(raw)}")
        return raw

    def panel_bytes_to_indices(self, raw: bytes) -> NDArray:
        """Inverse of ``indices_to_panel_bytes``; raises on unknown nibbles."""
        panel_bytes = self._PANEL_W_HALF * self.panel_h // 2
        if len(raw) != self.total_bytes:
            raise ValueError(f"Expected {self.total_bytes} bytes, got {len(raw)}")
        b1 = np.frombuffer(raw[:panel_bytes], dtype=np.uint8).reshape(
            self.panel_h, self._PANEL_W_HALF // 2
        )
        b2 = np.frombuffer(raw[panel_bytes:], dtype=np.uint8).reshape(
            self.panel_h, self._PANEL_W_HALF // 2
        )
        nib1 = np.empty((self.panel_h, self._PANEL_W_HALF), dtype=np.uint8)
        nib1[:, 0::2] = (b1 >> 4) & 0x0F
        nib1[:, 1::2] = b1 & 0x0F
        nib2 = np.empty((self.panel_h, self._PANEL_W_HALF), dtype=np.uint8)
        nib2[:, 0::2] = (b2 >> 4) & 0x0F
        nib2[:, 1::2] = b2 & 0x0F
        nibbles = np.hstack([nib1, nib2])
        nibble_to_index = np.full(16, 255, dtype=np.uint8)
        for i, n in enumerate(self.palette_nibble):
            nibble_to_index[int(n)] = i
        out = nibble_to_index[nibbles.astype(np.uint16)]
        if np.any(out == 255):
            raise ValueError("Panel bytes contain a nibble not in the device palette")
        return out.astype(np.uint8)
