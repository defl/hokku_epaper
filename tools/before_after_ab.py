#!/usr/bin/env python3
"""A/B this session's starting point against where it landed, on real glass.

LEFT (original): clahe_clip_limit=1.75, old linear+highlight-only-tanh-
shoulder DRC (reconstructed — this session fully replaced it), no gamut-
correction LUT.

RIGHT (new): today's picked defaults — CLAHE off, S-curve DRC at k=7, gamut-
correction LUT on (shipped asset, already at its validated 0.5 blend).

Reuses drc_scurve_ab.py's reconstructed old DRC functions rather than
duplicating them. Same half-canvas pattern as the other *_ab.py tools here.
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


def render_original(display, img: Image.Image, half_w: int, clahe: float = 1.75) -> np.ndarray:
    visual_h = display.visual_h
    canvas_w, canvas_h = (visual_h, half_w) if display.panel_rotated else (half_w, visual_h)
    cfg = replace(DEFAULT_IMAGE_CONFIG, clahe_clip_limit=clahe)
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


def render_new(display, img: Image.Image, half_w: int, clahe: float | None = None) -> np.ndarray:
    visual_h = display.visual_h
    canvas_w, canvas_h = (visual_h, half_w) if display.panel_rotated else (half_w, visual_h)
    cfg = (
        DEFAULT_IMAGE_CONFIG
        if clahe is None
        else replace(DEFAULT_IMAGE_CONFIG, clahe_clip_limit=clahe)
    )
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
        "--clahe",
        type=float,
        default=None,
        help="override clahe_clip_limit on BOTH sides (default: LEFT=1.75, "
        "RIGHT=whatever DEFAULT_IMAGE_CONFIG ships) — set this to isolate "
        "just the DRC+LUT change with CLAHE held fixed on both sides",
    )
    ap.add_argument(
        "--bench-flip180",
        action="store_true",
        help="rotate the composed image 180° before sending — for a unit that is "
        "physically mounted upside down on the test bench (e.g. Huessen for USB "
        "access), not a property of the panel or the production pipeline",
    )
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    left_clahe = 1.75 if args.clahe is None else args.clahe
    print(f"  LEFT  original (clahe={left_clahe}, old linear+shoulder DRC, no gamut LUT)")
    print(
        f"  RIGHT new (clahe={args.clahe if args.clahe is not None else 'default'}, S-curve DRC k=7, gamut LUT on)"
    )

    img = Image.open(args.image).convert("RGB")
    half = display.visual_w // 2

    left_vis = render_original(display, img, half, clahe=left_clahe)
    right_vis = render_new(display, img, half, clahe=args.clahe)
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
        ok = send_frame(s, data, "before/after A/B")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    print("\nOn the glass: LEFT = original, RIGHT = new.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
