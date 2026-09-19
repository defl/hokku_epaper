#!/usr/bin/env python3
"""A/B the adaptive-saturation low-chroma threshold (OKLAB) on real glass:
current default against a raised value — for a human to judge whether it
excludes faint near-neutral tints (e.g. a barely-blue cloud wisp) from the
saturation boost without losing real colour elsewhere. Same half-canvas
pattern as clahe_ab.py.
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
    ap.add_argument("--right-thresh", type=float, required=True)
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
    left_thresh = DEFAULT_IMAGE_CONFIG.saturate_low_chroma_thresh_oklab

    print(f"  LEFT  saturate_low_chroma_thresh_oklab={left_thresh}")
    print(f"  RIGHT saturate_low_chroma_thresh_oklab={args.right_thresh}")

    img = Image.open(args.image).convert("RGB")
    cfg_left = DEFAULT_IMAGE_CONFIG
    cfg_right = replace(cfg_left, saturate_low_chroma_thresh_oklab=args.right_thresh)
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
        ok = send_frame(s, data, "saturation threshold A/B")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    print(f"\nOn the glass: LEFT = thresh={left_thresh}, RIGHT = thresh={args.right_thresh}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
