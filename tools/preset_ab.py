#!/usr/bin/env python3
"""A/B any two named ImageConfig presets on real glass — for a human to judge.

Same half-canvas pattern as clahe_ab.py/drc_scurve_ab.py/color_lut_ab.py (see
drc_anchor_ab.py's docstring for the geometry reasoning, unchanged here): each
side renders the WHOLE photo at half-panel width via the pipeline's own fit
logic, composed with a black divider, pushed as one frame.

Generic over PRESET_IMAGE_CONFIGS rather than hardcoding a pair, so it covers
any "what does preset X look like next to preset Y" question — e.g.
calibration_raw (nearest-ink passthrough, no tonal chain at all — the closest
thing this pipeline has to "unaltered") against default_general (everything
this session tuned: CLAHE off, S-curve DRC, blend=0.5 gamut-correction LUT).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import StreamingDither
from hokku.webserver.image_renderer import ImageRenderer
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from send_frame import open_device, send_frame


def to_visible(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=1) if display.panel_rotated else idx


def to_panel(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=3) if display.panel_rotated else idx


def render_half(display, cfg, img: Image.Image, half_w: int) -> np.ndarray:
    visual_h = display.visual_h
    canvas_w, canvas_h = (visual_h, half_w) if display.panel_rotated else (half_w, visual_h)
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
    ap.add_argument("--left", required=True, choices=sorted(PRESET_IMAGE_CONFIGS))
    ap.add_argument("--right", required=True, choices=sorted(PRESET_IMAGE_CONFIGS))
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
    print(f"  LEFT  ({args.left})")
    print(f"  RIGHT ({args.right})")

    img = Image.open(args.image).convert("RGB")
    cfg_left = PRESET_IMAGE_CONFIGS[args.left]
    cfg_right = PRESET_IMAGE_CONFIGS[args.right]
    half = display.visual_w // 2

    left_vis = render_half(display, cfg_left, img, half)
    right_vis = render_half(display, cfg_right, img, half)
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
        ok = send_frame(s, data, "preset A/B")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    print(f"\nOn the glass: LEFT = {args.left}, RIGHT = {args.right}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
