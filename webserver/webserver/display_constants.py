"""Display geometry, palette, and file-type constants for EL133UF1 / Spectra 6."""
import numpy as np

PANEL_W = 600
PANEL_H = 1600
PANEL_BYTES = PANEL_W * PANEL_H // 2
TOTAL_BYTES = PANEL_BYTES * 2
FULL_W = PANEL_W * 2
VISUAL_W = 1600
VISUAL_H = 1200

PALETTE_MEASURED_RGB = np.array([
    [2, 2, 2],
    [190, 200, 200],
    [205, 202, 0],
    [135, 19, 0],
    [5, 64, 158],
    [39, 102, 60],
], dtype=np.float32)

PALETTE_PREVIEW_RGB = np.array([
    [0, 0, 0],
    [255, 255, 255],
    [255, 230, 50],
    [200, 20, 20],
    [30, 80, 200],
    [20, 120, 40],
], dtype=np.uint8)

PALETTE_NIBBLE = np.array([0x0, 0x1, 0x2, 0x3, 0x5, 0x6], dtype=np.uint8)
