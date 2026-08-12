#!/usr/bin/env python3
"""A/B the DRC's lightness anchors: the shipped ones against this panel's own.

``compress_dynamic_range`` maps a source image's L\\* range onto the panel's
reachable range, with a tanh shoulder near white. It reads that range from
``PALETTE_LAB``, which ``dither_streaming`` derives from the **Huessen EPF1301**
reference display — deliberately, per the comment there — regardless of which
screen is being rendered for.

On the F7 that is badly wrong:

    DRC target range   L* 0.55 .. 79.86   (width 79.31)
    F7 measured        L* 10.21 .. 68.02  (width 57.81)

The target is 37 % wider than the panel can show, so both ends clip: everything
the DRC emits below L* 10.21 flattens to black and everything above L* 68.02
flattens to white. On the De Niro portrait that is 50 % of the image crushed into
undifferentiated shadow. The tanh shoulder cannot help either — its whole roll-off
region lies beyond the panel's white.

Nothing is changed in the pipeline here. This renders the same photo twice through
the real production path, patching only the two anchor numbers on the right-hand
side, so a human can judge whether correcting them actually looks better. Today
has already produced three plausible improvements that a human rejected on sight,
so the measurement is the argument for looking, not a substitute for it.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np
import serial
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "python"))

from color_measure_f7 import catch_console, upload_with_retry
from color_model import lab, load_records, primaries
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver import image_renderer as IR
from hokku.webserver.dither_streaming import (
    PALETTE_MEASURED_RGB,
    StreamingDither,
    rgb_to_lab,
    rgb_to_oklab,
)
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import DEFAULT_IMAGE_CONFIG


def measured_anchor_rgb(data: pathlib.Path) -> np.ndarray:
    """sRGB stand-ins whose L\\* equals the panel's measured black and white.

    The DRC only ever reads row 0 and row 1 of the L\\* column, so matching
    lightness is sufficient and avoids disturbing anything that reads chroma.
    """
    prim = primaries(load_records(data))
    out = np.array(PALETTE_MEASURED_RGB, dtype=np.float32).copy()
    for row, ink in ((0, "black"), (1, "white")):
        target_l = lab(prim[ink]["xyz"])[0]
        # Solve a neutral grey with this L*: monotonic, so a scan is exact enough
        # and cannot diverge the way an inverted analytic form can.
        vals = np.arange(256, dtype=np.float32)
        greys = np.stack([vals, vals, vals], axis=-1)
        ls = rgb_to_lab(greys)[:, 0]
        out[row] = greys[int(np.argmin(np.abs(ls - target_l)))]
    return out


def render(
    img: Image.Image, display, cfg, anchors_rgb: np.ndarray | None, w: int, h: int
) -> np.ndarray:
    """Full production render onto a w x h canvas.

    Each side is rendered at HALF-panel width rather than rendering the whole
    panel twice and slicing. Slicing shows the same half of the photo on both
    sides — a fair comparison of a useless composition (half a face, twice).
    Rendering to the half canvas lets the pipeline's own fit/crop pick a sensible
    framing, and both sides get identical treatment, so the DRC anchors remain
    the only difference.
    """
    old_lab, old_oklab = IR.PALETTE_LAB, IR.PALETTE_OKLAB
    try:
        if anchors_rgb is not None:
            IR.PALETTE_LAB = rgb_to_lab(anchors_rgb)
            IR.PALETTE_OKLAB = rgb_to_oklab(anchors_rgb)
        renderer = IR.ImageRenderer(dither=StreamingDither(display), display=display)
        return renderer.render_indices(img.copy(), cfg, Orientation.LANDSCAPE, w, h)
    finally:
        IR.PALETTE_LAB, IR.PALETTE_OKLAB = old_lab, old_oklab


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--image",
        type=pathlib.Path,
        default=pathlib.Path("images/test/Robert_De_Niro_KVIFF_portrait.jpg"),
    )
    ap.add_argument(
        "--data", type=pathlib.Path, default=pathlib.Path("build/colorcal/campaign.jsonl")
    )
    ap.add_argument("--port", default="COM10")
    ap.add_argument("--divider", type=int, default=2)
    ap.add_argument("--save", type=pathlib.Path)
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY["bigme_f7"]
    W, H = display.panel_w, display.panel_h
    cfg = DEFAULT_IMAGE_CONFIG

    shipped = np.array(IR.PALETTE_LAB, dtype=float)
    fixed_rgb = measured_anchor_rgb(args.data)
    fixed = rgb_to_lab(fixed_rgb)
    print(f"  LEFT  (ships)   DRC range L* {shipped[0, 0]:.2f} .. {shipped[1, 0]:.2f}")
    print(f"  RIGHT (fixed)   DRC range L* {fixed[0, 0]:.2f} .. {fixed[1, 0]:.2f}   <- measured F7")

    img = Image.open(args.image).convert("RGB")
    half = W // 2
    left = render(img, display, cfg, None, half, H)
    right = render(img, display, cfg, fixed_rgb, half, H)

    out = np.empty((H, W), dtype=np.uint8)
    out[:, :half] = left[:, :half]
    out[:, half:] = right[:, :half]
    if args.divider > 0:
        c = half - args.divider // 2
        out[:, c : c + args.divider] = 0

    if args.save:
        rgb = np.rint(display.palette_measured_rgb).clip(0, 255).astype(np.uint8)[out]
        Image.fromarray(rgb).save(args.save)
        print(f"  preview -> {args.save}")

    data = display.indices_to_panel_bytes(out)
    print(f"catching console on {args.port}...", flush=True)
    s = catch_console(args.port, 600.0)
    if s is None:
        print("console never answered — long-press the power button and retry")
        return 1
    try:
        ok = upload_with_retry(s, data, "DRC A/B", attempts=6, gap_s=8.0)
    finally:
        try:
            s.close()
        except (serial.SerialException, OSError):
            pass
    if not ok:
        print("upload failed")
        return 1
    print("\nOn the glass: LEFT = shipped DRC anchors, RIGHT = this panel's measured range.")
    print("Look into the shadows. Half this image is currently crushed below panel black.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
