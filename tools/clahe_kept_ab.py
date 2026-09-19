#!/usr/bin/env python3
"""A/B keeping CLAHE at the old 1.75 value while separately testing whether
today's other changes (S-curve DRC, gamut-correction LUT) still help on top
of it — rather than assuming CLAHE must be off for them to matter.

LEFT (previous settings): clahe_clip_limit=1.75, old linear+highlight-only-
tanh-shoulder DRC (reconstructed), no gamut-correction LUT — the full
pre-session pipeline.

RIGHT (CLAHE kept, rest added): clahe_clip_limit=1.75 (unchanged from LEFT),
but today's S-curve DRC (k=7, current code) and gamut-correction LUT (on,
shipped asset) added on top.

Reuses drc_scurve_ab.py's reconstructed old DRC functions. Same half-canvas
pattern as the other *_ab.py tools here.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from drc_scurve_ab import _old_drc_cielab_l, _old_drc_oklab_l
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import StreamingDither
from hokku.webserver.image_renderer import ImageRenderer
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import DEFAULT_IMAGE_CONFIG
from send_frame import open_device, send_frame


def to_visible(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=1) if display.panel_rotated else idx


def to_panel(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=3) if display.panel_rotated else idx


def render_previous(display, img: Image.Image, half_w: int) -> np.ndarray:
    visual_h = display.visual_h
    canvas_w, canvas_h = (visual_h, half_w) if display.panel_rotated else (half_w, visual_h)
    cfg = replace(DEFAULT_IMAGE_CONFIG, clahe_clip_limit=1.75)
    original_path = display.correction_lut_path
    original_cielab = ImageRenderer._drc_cielab_l
    original_oklab = ImageRenderer._drc_oklab_l
    try:
        display.correction_lut_path = None
        ImageRenderer._drc_cielab_l = staticmethod(_old_drc_cielab_l)
        ImageRenderer._drc_oklab_l = staticmethod(_old_drc_oklab_l)
        renderer = ImageRenderer(dither=StreamingDither(display), display=display)
        idx = renderer.render_indices(img.copy(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h)
    finally:
        display.correction_lut_path = original_path
        ImageRenderer._drc_cielab_l = original_cielab
        ImageRenderer._drc_oklab_l = original_oklab
    return to_visible(idx, display)


def render_clahe_kept(display, img: Image.Image, half_w: int) -> np.ndarray:
    visual_h = display.visual_h
    canvas_w, canvas_h = (visual_h, half_w) if display.panel_rotated else (half_w, visual_h)
    cfg = replace(DEFAULT_IMAGE_CONFIG, clahe_clip_limit=1.75)
    renderer = ImageRenderer(dither=StreamingDither(display), display=display)
    idx = renderer.render_indices(img.copy(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h)
    return to_visible(idx, display)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", required=True, choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--image", type=Path, required=True)
    ap.add_argument("--port", required=True)
    ap.add_argument("--console-timeout", type=float, default=180.0)
    ap.add_argument("--divider", type=int, default=2, help="black separator width in px")
    ap.add_argument("--save", type=Path, help="write the composed preview as a PNG, no upload")
    ap.add_argument(
        "--bench-flip180",
        action="store_true",
        help="rotate the composed image 180° before sending — for a unit that is "
        "physically mounted upside down on the test bench (e.g. Huessen for USB "
        "access), not a property of the panel or the production pipeline",
    )
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    print("  LEFT  previous (clahe=1.75, old linear+shoulder DRC, no gamut LUT)")
    print("  RIGHT clahe=1.75 kept + S-curve DRC k=7 + gamut LUT on")

    img = Image.open(args.image).convert("RGB")
    half = display.visual_w // 2

    left_vis = render_previous(display, img, half)
    right_vis = render_clahe_kept(display, img, half)
    assert left_vis.shape == (display.visual_h, half), left_vis.shape

    out_w = half * 2
    out_vis = np.empty((display.visual_h, out_w), dtype=np.uint8)
    out_vis[:, :half] = left_vis
    out_vis[:, half:out_w] = right_vis
    if args.divider > 0:
        c = half - args.divider // 2
        out_vis[:, c : c + args.divider] = 0  # black ink

    if args.bench_flip180:
        out_vis = np.rot90(out_vis, k=2)

    if args.save:
        rgb = np.rint(display.palette_measured_rgb).clip(0, 255).astype(np.uint8)[out_vis]
        Image.fromarray(rgb).save(args.save)
        print(f"  preview (as-mounted orientation) -> {args.save}")
        return 0

    out_panel = to_panel(out_vis, display)
    data = display.indices_to_panel_bytes(out_panel)

    print(f"opening {args.port} ({args.model})...", flush=True)
    s = open_device(args.port, args.model, timeout_s=args.console_timeout, interactive=False)
    if s is None:
        return 1
    try:
        ok = send_frame(s, data, "CLAHE-kept A/B")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    print("\nOn the glass: LEFT = previous, RIGHT = clahe kept + DRC + LUT.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
