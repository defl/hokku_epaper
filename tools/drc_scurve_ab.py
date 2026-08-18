#!/usr/bin/env python3
"""A/B the DRC's L-compression shape on real glass: the old pure-linear map
(+ highlight-only tanh shoulder) against the new bounded S-curve — for a
human to judge.

Same half-canvas/no-swap/flip pattern as drc_anchor_ab.py and color_lut_ab.py
(see drc_anchor_ab.py's docstring for the geometry reasoning, unchanged
here). Toggles the DRC's L-compression shape, not drc_anchor_l itself —
both sides use the SAME (correct) anchor range, so this isolates the curve
shape as the only variable.

Background: the correct (narrow, measured) drc_anchor_l range gives a purely
linear L-compression a single, comparatively gentle slope end to end. Real-
glass comparisons found this measurably flatter in shadows/midtones (crisper
tie pattern, more visible background lamp glow, crisper facial shadows)
than the OLD, WRONG, too-wide range used to give — at the cost of that old
range blowing out highlights, since it assumed the panel could reach whiter
than it actually can. A pure linear map can't have both directions at once:
one slope, one behaviour. An S-curve can — steeper through the middle
(recovering the lost contrast), tapering smoothly to zero slope at BOTH
anchors so neither end can ever overshoot the panel's real range. See
ImageRenderer._DRC_CONTRAST_STRENGTH for the exact curve and its rationale.

The OLD linear-plus-shoulder implementation is reconstructed here rather
than kept in production code, since it was fully replaced.
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
from hokku.webserver.dither_streaming import StreamingDither, oklab_to_rgb, rgb_to_lab, rgb_to_oklab
from hokku.webserver.image_renderer import ImageRenderer
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import DEFAULT_IMAGE_CONFIG
from send_frame import open_device, send_frame


def _old_drc_cielab_l(rgb: np.ndarray, anchor_l: tuple[float, float] | None = None) -> np.ndarray:
    """The pure-linear-map-plus-highlight-only-tanh-shoulder DRC this session
    replaced. Reconstructed here only for A/B comparison purposes. Always
    called with an explicit anchor_l by this tool; the default only exists to
    match ImageRenderer._drc_cielab_l's real signature for the monkeypatch."""
    assert anchor_l is not None
    f32 = np.float32
    lab = rgb_to_lab(rgb, dtype=f32)
    lightness = lab[..., 0]
    black_l = f32(anchor_l[0])
    white_l = f32(anchor_l[1])
    ratio = f32((float(white_l) - float(black_l)) / 100.0)
    np.multiply(lightness, ratio, out=lightness)
    np.add(lightness, black_l, out=lightness)
    threshold = black_l + f32(0.85) * (white_l - black_l)
    headroom = white_l - threshold
    above = lightness > threshold
    if np.any(above):
        delta = lightness[above] - threshold
        lightness[above] = (threshold + headroom * np.tanh(delta / headroom)).astype(f32)
    return ImageRenderer._lab_to_rgb(lab)


def _old_drc_oklab_l(rgb: np.ndarray, anchor_l: tuple[float, float] | None = None) -> np.ndarray:
    """The OKLAB counterpart of _old_drc_cielab_l — same pure-linear-plus-
    highlight-only-tanh-shoulder shape, on OKLAB's native [0, 1] L axis
    instead of CIELAB's [0, 100]. Reconstructed for A/B comparison only.

    DEFAULT_IMAGE_CONFIG sets drc_l_space="oklab", so THIS is the function
    production actually calls — an earlier version of this tool only
    monkeypatched _drc_cielab_l, which render_indices never reaches under
    the default config. Both sides of that A/B silently rendered through
    the SAME (new) curve; the "nearly impossible to tell" verdict it
    produced was comparing a render to itself, not old against new."""
    assert anchor_l is not None
    f32 = np.float32
    oklab = rgb_to_oklab(rgb, dtype=f32)
    lightness = oklab[..., 0]
    black_l = f32(anchor_l[0])
    white_l = f32(anchor_l[1])
    ratio = white_l - black_l
    np.multiply(lightness, ratio, out=lightness)
    np.add(lightness, black_l, out=lightness)
    threshold = black_l + f32(0.85) * (white_l - black_l)
    headroom = white_l - threshold
    above = lightness > threshold
    if np.any(above):
        delta = lightness[above] - threshold
        lightness[above] = (threshold + headroom * np.tanh(delta / headroom)).astype(f32)
    return oklab_to_rgb(oklab, dtype=f32)


def to_visible(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=1) if display.panel_rotated else idx


def to_panel(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=3) if display.panel_rotated else idx


def render_half(display, cfg, img: Image.Image, use_old_curve: bool, half_w: int) -> np.ndarray:
    """Render the WHOLE photo into a half_w x visual_h canvas (VISIBLE
    orientation), with the DRC L-compression shape temporarily overridden.

    Patches whichever of _drc_cielab_l/_drc_oklab_l cfg.drc_l_space actually
    dispatches to (render_indices picks one based on that field — see
    ImageRenderer.compress_dynamic_range) — patching the other one is a
    silent no-op."""
    visual_h = display.visual_h
    canvas_w, canvas_h = (visual_h, half_w) if display.panel_rotated else (half_w, visual_h)
    original_cielab = ImageRenderer._drc_cielab_l
    original_oklab = ImageRenderer._drc_oklab_l
    try:
        if use_old_curve:
            if cfg.drc_l_space == "cielab":
                ImageRenderer._drc_cielab_l = staticmethod(_old_drc_cielab_l)
            else:
                ImageRenderer._drc_oklab_l = staticmethod(_old_drc_oklab_l)
        renderer = ImageRenderer(dither=StreamingDither(display), display=display)
        idx = renderer.render_indices(img.copy(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h)
    finally:
        ImageRenderer._drc_cielab_l = original_cielab
        ImageRenderer._drc_oklab_l = original_oklab
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
    if display.drc_anchor_l is None:
        raise SystemExit(f"{args.model} has no drc_anchor_l set — nothing to A/B")

    print("  LEFT  (old linear+shoulder) pure linear map, highlight-only tanh shoulder")
    print("  RIGHT (new S-curve)         bounded logistic sigmoid, both ends softened")

    img = Image.open(args.image).convert("RGB")
    cfg = DEFAULT_IMAGE_CONFIG
    half = display.visual_w // 2

    left_vis = render_half(display, cfg, img, True, half)
    right_vis = render_half(display, cfg, img, False, half)
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
        ok = send_frame(s, data, "DRC S-curve A/B")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    print("\nOn the glass: LEFT = old linear+shoulder, RIGHT = new S-curve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
