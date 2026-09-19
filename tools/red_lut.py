#!/usr/bin/env python3
"""Build a correction LUT that is the shipped one everywhere except in reds.

The gamut-dial test found every campaign cusp LUT paler than the shipped
correction in all twelve hue sectors, so the dial never spanned what ships. The
four photographs it rescued were the red-heavy ones, and red is where the shipped
correction is most extreme on glass: the darkest colour it renders (L* 29.9) at
the highest chroma, which is the red wall's "gray goop". Everywhere else the
uniformly paler LUTs cost colour — `DSC02087`, blue-dominant, lost on all three
sittings.

So this isolates the one change the evidence points at. Each LUT node takes the
shipped output, blended toward a cusp LUT's output by a weight that depends only
on the node's *input* colour:

* **hue** — full weight across the red sector (CIELAB hue 0-30°, the one whose
  share of the picture separated the rescued images), cosine taper to zero 20°
  beyond each edge;
* **chroma** — a smooth ramp, so neutrals keep exactly the shipped mapping and
  hue — undefined at low chroma — never decides anything.

Both inputs are smooth, so the blended LUT is continuous: no seam at the window
edge for the dither to find.

**Skin is not separable by hue.** The first build ramped chroma from 10 to 25
on the assumption that skin sat at ~50° and outside the window. It does not:
inside detected faces the middle 80 % of chromatic pixels span hue 10-57°,
median 37°, a quarter of them inside the full-weight band. On glass the red LUTs
won on every red-heavy photograph and lost only by putting blue patches into
skin. Chroma is what separates them: in the red band, 80 % of face pixels are
below C* 30 while half of the red walls, scarves and clothes are above C* 40.
So the skin-safe build ramps chroma from 30 to 45 (`--chroma-lo 30
--chroma-hi 45`): full effect reaches 2.5 % of face pixels (mostly lips) and
44 % of other red-band pixels.

    python tools/red_lut.py --toward build/camcal/lut_b1.0.npy \\
        --chroma-lo 30 --chroma-hi 45 --out build/camcal/lut_red_safe_100.npy
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

from color_validate_photos import lab_img, srgb_img_to_xyz
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_renderer import ImageRenderer

RED_FROM, RED_TO = 0.0, 30.0  # full-weight hue band, degrees (CIELAB)
TAPER = 20.0  # cosine roll-off beyond each edge
# The first build's ramp; kept as the default so lut_red_{50,100} reproduce.
CHROMA_LO, CHROMA_HI = 10.0, 25.0


def smoothstep(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    t = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def red_weight(
    rgb: np.ndarray, chroma_lo: float = CHROMA_LO, chroma_hi: float = CHROMA_HI
) -> np.ndarray:
    """Weight in [0, 1] per input sRGB colour (..., 3), 0-255."""
    lab = lab_img(srgb_img_to_xyz(rgb))
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    hue = np.degrees(np.arctan2(lab[..., 2], lab[..., 1]))  # -180..180
    centre, half = (RED_FROM + RED_TO) / 2, (RED_TO - RED_FROM) / 2
    dist = np.abs((hue - centre + 180.0) % 360.0 - 180.0)  # angular distance to centre
    beyond = np.clip(dist - half, 0.0, TAPER)
    w_hue = 0.5 * (1.0 + np.cos(np.pi * beyond / TAPER))
    return w_hue * smoothstep(chroma, chroma_lo, chroma_hi)


def build(
    shipped: np.ndarray,
    toward: np.ndarray,
    strength: float,
    chroma_lo: float = CHROMA_LO,
    chroma_hi: float = CHROMA_HI,
) -> np.ndarray:
    """Blend on *toward*'s grid; the shipped LUT is resampled onto it."""
    n = toward.shape[0]
    axis = np.linspace(0.0, 255.0, n, dtype=np.float32)
    grid = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1)
    base = ImageRenderer.apply_correction_lut(grid, shipped.astype(np.float32))
    w = (strength * red_weight(grid, chroma_lo, chroma_hi))[..., None]
    return ((1.0 - w) * base + w * toward).astype(np.float32)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--toward", type=Path, required=True, help="cusp LUT to blend reds toward")
    ap.add_argument("--strength", type=float, default=1.0, help="blend at full red weight")
    ap.add_argument("--chroma-lo", type=float, default=CHROMA_LO, help="ramp starts (C*)")
    ap.add_argument("--chroma-hi", type=float, default=CHROMA_HI, help="full weight from (C*)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    if display.correction_lut_path is None:
        print(f"  {args.model} has no correction LUT to blend from")
        return 1
    shipped = np.load(display.correction_lut_path).astype(np.float32)
    toward = np.load(args.toward).astype(np.float32)
    lut = build(shipped, toward, args.strength, args.chroma_lo, args.chroma_hi)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.save(args.out, lut)

    # Report where it moved, so a wrong window shows up before any panel time.
    n = lut.shape[0]
    axis = np.linspace(0.0, 255.0, n, dtype=np.float32)
    grid = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1)
    base = ImageRenderer.apply_correction_lut(grid, shipped)
    moved = np.abs(lut - base).max(axis=-1)
    lab = lab_img(srgb_img_to_xyz(grid))
    hue = (np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) + 360.0) % 360.0
    chromatic = np.hypot(lab[..., 1], lab[..., 2]) > 25
    print(f"  {args.out}: {n}^3 nodes, {100 * (moved > 1).mean():.1f} % moved >1 RGB level")
    for lo in range(0, 360, 30):
        sel = chromatic & (hue >= lo) & (hue < lo + 30)
        if sel.any():
            print(f"    hue {lo:3d}-{lo + 30:3d}: mean move {moved[sel].mean():5.1f} RGB levels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
