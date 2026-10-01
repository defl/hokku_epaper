#!/usr/bin/env python3
"""Put two dither configs side by side on the panel, for a human to judge.

Every comparison so far has been a number: dE, hue shift, a fitted model. Those
agree that the `hue_aware` LUT drives skin blue and that plain `euclidean` fixes
it, and the model has been validated against measured skin to within ~1 unit. What
none of it establishes is whether the difference is visible and preferable to a
person looking at a photograph, which is the actual goal.

So: the SAME crop, rendered twice, on the glass at once. Sequential viewing relies
on colour memory, which is poor; side by side is how the difference is actually
seen. Each half is dithered independently by its own config, so each is exactly
what that config would ship.

The tonal chain is deliberately still bypassed. Production runs autocontrast and
DRC ahead of the dither, and including them would confound the thing under test —
but it does mean this shows the dither's contribution, not the final pipeline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import serial
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from color_measure_f7 import catch_console, upload_with_retry
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.dither_streaming import dither


def crop_to(img: Image.Image, w: int, h: int) -> Image.Image:
    """Centre-crop to the target aspect, then resize — never squash.

    Both halves must show the SAME content for the comparison to mean anything,
    so this crops rather than fitting, and both halves get the identical crop.
    """
    tw, th = w / h, img.width / img.height
    if th > tw:
        new_w = int(img.height * tw)
        img = img.crop(((img.width - new_w) // 2, 0, (img.width + new_w) // 2, img.height))
    else:
        new_h = int(img.width / tw)
        img = img.crop((0, (img.height - new_h) // 2, img.width, (img.height + new_h) // 2))
    return img.resize((w, h), Image.Resampling.LANCZOS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--image", type=Path, default=Path("images/test/Robert_De_Niro_KVIFF_portrait.jpg")
    )
    ap.add_argument("--port", default="COM10")
    ap.add_argument("--left", default="hue_aware", help="LUT for the left half (production)")
    ap.add_argument("--right", default="oklab", help="LUT for the right half (candidate)")
    ap.add_argument("--algorithm", default="floyd_steinberg")
    ap.add_argument("--serpentine", action="store_true", default=True)
    # These ARE the hue-aware gate. Defaulting them to anything other than what
    # presets.py ships produced a whole day of conclusions about a config we do
    # not run: at cutoff=30 hue_aware looks disastrous on skin, at the shipped
    # cutoff=95 it is bit-identical to euclidean there. Never guess these.
    ap.add_argument("--hue-cutoff-deg", type=float, default=95.0)
    ap.add_argument("--neutral-chroma", type=float, default=8.0)
    ap.add_argument("--divider", type=int, default=2, help="black separator width in px")
    ap.add_argument("--save", type=Path, help="also write the composed preview as a PNG")
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY["bigme_f7"]
    W, H = display.panel_w, display.panel_h
    half = W // 2

    img = Image.open(args.image).convert("RGB")
    crop = crop_to(img, half, H)
    src = np.array(crop, dtype=np.uint8)

    out = np.zeros((H, W), dtype=np.uint8)
    for side, lut, x0 in (("left", args.left, 0), ("right", args.right, half)):
        cfg = DitherConfig(
            args.algorithm, lut, args.serpentine, args.hue_cutoff_deg, args.neutral_chroma
        )
        idx = dither(src.copy(), cfg, display)
        out[:, x0 : x0 + half] = idx
        print(
            f"  {side:5s} = {args.algorithm} serp={args.serpentine} / {lut} "
            f"(cutoff={args.hue_cutoff_deg}, chroma={args.neutral_chroma})"
        )

    if args.divider > 0:
        # A hard seam makes the boundary unambiguous; without it the eye tries to
        # read the two halves as one picture and the difference is harder to see.
        c = half - args.divider // 2
        out[:, c : c + args.divider] = 0  # black ink

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
        ok = upload_with_retry(s, data, "A/B comparison", attempts=6, gap_s=8.0)
    finally:
        try:
            s.close()
        except (serial.SerialException, OSError):
            pass
    if not ok:
        print("upload failed")
        return 1
    print(f"\nOn the glass now: LEFT = {args.left} (ships today), RIGHT = {args.right}.")
    print("Look at the skin. The claim is the left is bluer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
