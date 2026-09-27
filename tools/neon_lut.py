#!/usr/bin/env python3
"""A correction LUT that calms neon colours and leaves skin alone.

After colour boosts off and autocontrast off, the remaining complaints are
nearly all "neon" and nearly all non-skin: grass, yellow hair highlights, a sky
"too blue", red goggles, orange crowns, a purple hat. Plain desaturation (0.7x
everywhere) fixed some of those photographs and washed out others — "faces
undersaturated" — and its gain fell with skin share (rho -0.41). So this reduces
chroma selectively:

* **Skin is protected by hue and chroma together.** Inside detected faces,
  the middle 80 % of chromatic pixels span CIELAB hue 10-57° and sit below C*
  30 (measured on the test set). Colours in that hue window keep their chroma
  up to C* 30, with the protection fading out by C* 45 — so a cheek is
  untouched but saturated red goggles and orange crowns in the same hues are
  calmed.
* **Everything else with real colour is calmed**, ramping in from C* 15 to 35 so
  neutrals and near-neutrals are never touched.

Built as a composition, so nothing downstream changes: each LUT node's input
colour is desaturated in CIELAB, then mapped through the shipped correction
LUT. ``--no-skin`` builds the same strength without the protection, the control
that tells whether protecting skin is what matters.

    python tools/neon_lut.py --strength 0.7 --out build/camcal/lut_neon_70.npy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from gamut_clamp import lab_to_rgb
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import rgb_to_lab
from hokku.webserver.image_renderer import ImageRenderer
from red_lut import smoothstep

SKIN_CENTRE, SKIN_HALF, SKIN_TAPER = 35.0, 25.0, 15.0  # hue window, degrees
SKIN_KEEP_LO, SKIN_KEEP_HI = 30.0, 45.0  # skin protection fades out over this C*
CALM_LO, CALM_HI = 15.0, 35.0  # calming ramps in over this C*


def skin_protection(lab: np.ndarray) -> np.ndarray:
    """1 where a colour is skin-like (hue window, skin-level chroma), else 0."""
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    hue = np.degrees(np.arctan2(lab[..., 2], lab[..., 1]))
    dist = np.abs((hue - SKIN_CENTRE + 180.0) % 360.0 - 180.0)
    beyond = np.clip(dist - SKIN_HALF, 0.0, SKIN_TAPER)
    in_window = 0.5 * (1.0 + np.cos(np.pi * beyond / SKIN_TAPER))
    return in_window * (1.0 - smoothstep(chroma, SKIN_KEEP_LO, SKIN_KEEP_HI))


def calm_weight(lab: np.ndarray, protect_skin: bool = True) -> np.ndarray:
    """0..1: how much of the chroma reduction a colour receives."""
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    w = smoothstep(chroma, CALM_LO, CALM_HI)
    return w * (1.0 - skin_protection(lab)) if protect_skin else w


def build(shipped: np.ndarray, strength: float, protect_skin: bool, n: int = 33) -> np.ndarray:
    """Desaturate each node's input by up to (1 - strength), then apply *shipped*."""
    axis = np.linspace(0.0, 255.0, n, dtype=np.float32)
    grid = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1)
    lab = rgb_to_lab(grid.astype(np.float64))
    scale = 1.0 - calm_weight(lab, protect_skin) * (1.0 - strength)
    calmed = lab.copy()
    calmed[..., 1:] *= scale[..., None]
    rgb = lab_to_rgb(calmed).astype(np.float32)
    return ImageRenderer.apply_correction_lut(rgb, shipped.astype(np.float32))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--strength", type=float, required=True, help="chroma kept at full weight")
    ap.add_argument("--no-skin", action="store_true", help="no skin protection (the control)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    if display.correction_lut_path is None:
        print(f"  {args.model} has no correction LUT to compose with")
        return 1
    shipped = np.load(display.correction_lut_path).astype(np.float32)
    lut = build(shipped, args.strength, not args.no_skin)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.save(args.out, lut)

    # Where it moved, against the shipped LUT on the same grid — skin must not.
    n = lut.shape[0]
    axis = np.linspace(0.0, 255.0, n, dtype=np.float32)
    grid = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1)
    base = ImageRenderer.apply_correction_lut(grid, shipped)
    moved = np.abs(lut - base).max(axis=-1)
    lab = rgb_to_lab(grid.astype(np.float64))
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    hue = (np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) + 360.0) % 360.0
    skin = (hue > 15) & (hue < 55) & (chroma > 12) & (chroma < 28) & (lab[..., 0] > 30)
    print(
        f"  {args.out}: {n}^3 nodes, strength {args.strength}, "
        f"skin {'unprotected' if args.no_skin else 'protected'}"
    )
    print(
        f"    skin-like colours (hue 15-55, C* 12-28): mean move {moved[skin].mean():5.1f} RGB levels"
    )
    for lo, hi, name in (
        (60, 150, "yellow-green"),
        (150, 270, "cyan-blue"),
        (270, 350, "purple-magenta"),
        (350, 375, "red"),
    ):
        sel = (chroma > 35) & (((hue - lo) % 360) < (hi - lo))
        print(f"    saturated {name:14s}: mean move {moved[sel].mean():5.1f} RGB levels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
