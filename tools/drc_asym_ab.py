#!/usr/bin/env python3
"""A/B today's symmetric S-curve DRC (one k for both toe and shoulder)
against an ASYMMETRIC version (independent k_toe/k_shoulder) — for a human
to judge whether decoupling them fixes the confirmed trade-off: k=7
symmetric gives good blacks (steep toe) but washes out sky blue (the same
steepness applied to the shoulder pushes moderately-bright content too hard
toward white).

_scurve_asym reuses ImageRenderer._scurve unmodified on each independently
rescaled half ([0,0.5]->[0,1]->_scurve->[0,1]->[0,0.5] and the mirror for
the top half), so each half correctly inherits _scurve's own k=0-is-linear
handling (a naive "vary k across a single centered sigmoid" construction
does NOT degenerate to linear at k=0 — it degenerates to a constant, which
is wrong). Value-continuous at t=0.5 by construction (both halves evaluate
to exactly 0.5 there regardless of k); slope may kink at the join if
k_toe != k_shoulder — accepted for now: a mid-gray transition, not an
extreme, so far less likely to read as a visible artifact than the toe/
shoulder failure modes this exists to fix. Reconstructed here only for A/B
comparison — not in image_renderer.py yet, pending this validation.

Same half-canvas pattern as drc_scurve_ab.py, and reuses its old-formula
reconstructions for a --vs-old 3-way-by-pairs option isn't included here —
this tool is specifically symmetric-vs-asymmetric.
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
from hokku.webserver.dither_streaming import StreamingDither, oklab_to_rgb, rgb_to_lab, rgb_to_oklab
from hokku.webserver.image_renderer import ImageRenderer
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import DEFAULT_IMAGE_CONFIG
from send_frame import open_device, send_frame


def _scurve_asym(t: np.ndarray, k_toe: float, k_shoulder: float) -> np.ndarray:
    f32 = np.float32
    below = t < f32(0.5)
    t_left_local = np.clip(t * f32(2.0), f32(0.0), f32(1.0))
    left = ImageRenderer._scurve(t_left_local, k_toe) * f32(0.5)
    t_right_local = np.clip((t - f32(0.5)) * f32(2.0), f32(0.0), f32(1.0))
    right = f32(0.5) + ImageRenderer._scurve(t_right_local, k_shoulder) * f32(0.5)
    return np.where(below, left, right).astype(f32)


def _asym_drc_cielab_l(rgb: np.ndarray, anchor_l, k_toe: float, k_shoulder: float) -> np.ndarray:
    f32 = np.float32
    lab = rgb_to_lab(rgb, dtype=f32)
    L = lab[..., 0]
    lo, hi = anchor_l
    black_L = f32(lo)
    white_L = f32(hi)
    t = np.clip(L / f32(100.0), f32(0.0), f32(1.0)).astype(f32)
    t_curved = _scurve_asym(t, k_toe, k_shoulder)
    lab[..., 0] = black_L + (white_L - black_L) * t_curved
    return ImageRenderer._lab_to_rgb(lab)


def _asym_drc_oklab_l(rgb: np.ndarray, anchor_l, k_toe: float, k_shoulder: float) -> np.ndarray:
    f32 = np.float32
    oklab = rgb_to_oklab(rgb, dtype=f32)
    L = oklab[..., 0]
    lo, hi = anchor_l
    black_L = f32(lo)
    white_L = f32(hi)
    t = np.clip(L, f32(0.0), f32(1.0)).astype(f32)
    t_curved = _scurve_asym(t, k_toe, k_shoulder)
    oklab[..., 0] = black_L + (white_L - black_L) * t_curved
    return oklab_to_rgb(oklab, dtype=f32)


def to_visible(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=1) if display.panel_rotated else idx


def to_panel(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=3) if display.panel_rotated else idx


def render_symmetric(display, cfg, img: Image.Image, target_w: int, target_h: int) -> np.ndarray:
    """target_w/target_h are the VISIBLE-orientation size of this variant's
    slice (half-width for a side-by-side split, half-height for a
    top/bottom stack — caller decides)."""
    canvas_w, canvas_h = (target_h, target_w) if display.panel_rotated else (target_w, target_h)
    renderer = ImageRenderer(dither=StreamingDither(display), display=display)
    idx = renderer.render_indices(img.copy(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h)
    return to_visible(idx, display)


def render_asym(
    display,
    cfg,
    img: Image.Image,
    k_toe: float,
    k_shoulder: float,
    target_w: int,
    target_h: int,
) -> np.ndarray:
    canvas_w, canvas_h = (target_h, target_w) if display.panel_rotated else (target_w, target_h)
    original_cielab = ImageRenderer._drc_cielab_l
    original_oklab = ImageRenderer._drc_oklab_l
    try:
        if cfg.drc_l_space == "cielab":
            ImageRenderer._drc_cielab_l = staticmethod(
                lambda rgb, anchor_l=None: _asym_drc_cielab_l(rgb, anchor_l, k_toe, k_shoulder)
            )
        else:
            ImageRenderer._drc_oklab_l = staticmethod(
                lambda rgb, anchor_l=None: _asym_drc_oklab_l(rgb, anchor_l, k_toe, k_shoulder)
            )
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
    ap.add_argument("--k-toe", type=float, required=True)
    ap.add_argument("--k-shoulder", type=float, required=True)
    ap.add_argument(
        "--left-k-toe",
        type=float,
        default=None,
        help="if set (with --left-k-shoulder), LEFT is also asymmetric "
        "instead of today's symmetric default — for comparing two "
        "asymmetric candidates directly",
    )
    ap.add_argument("--left-k-shoulder", type=float, default=None)
    ap.add_argument(
        "--clahe",
        type=float,
        default=None,
        help="override clahe_clip_limit on BOTH sides (default: whatever "
        "DEFAULT_IMAGE_CONFIG currently ships)",
    )
    ap.add_argument("--console-timeout", type=float, default=180.0)
    ap.add_argument("--divider", type=int, default=2, help="black separator width in px")
    ap.add_argument("--save", type=Path, help="write the composed preview as a PNG, no upload")
    ap.add_argument(
        "--stack",
        action="store_true",
        help="stack TOP/BOTTOM (each at half height, full width) instead of "
        "the default LEFT/RIGHT (each at half width, full height) — for "
        "strongly landscape/panoramic images where a half-width slice "
        "distorts the comparison too much to judge",
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
    if display.drc_anchor_l is None:
        raise SystemExit(f"{args.model} has no drc_anchor_l set — nothing to A/B")

    left_asym = args.left_k_toe is not None and args.left_k_shoulder is not None
    side_a, side_b = ("TOP", "BOTTOM") if args.stack else ("LEFT", "RIGHT")
    if left_asym:
        print(f"  {side_a}  asymmetric k_toe={args.left_k_toe} k_shoulder={args.left_k_shoulder}")
    else:
        print(f"  {side_a}  symmetric k={ImageRenderer._DRC_SIGMOID_K} (current default)")
    print(f"  {side_b} asymmetric k_toe={args.k_toe} k_shoulder={args.k_shoulder}")

    img = Image.open(args.image).convert("RGB")
    cfg = DEFAULT_IMAGE_CONFIG
    if args.clahe is not None:
        cfg = replace(cfg, clahe_clip_limit=args.clahe)
        print(f"  (clahe_clip_limit overridden to {args.clahe} on both sides)")

    if args.stack:
        half = display.visual_h // 2
        dims = (display.visual_w, half)
    else:
        half = display.visual_w // 2
        dims = (half, display.visual_h)

    if left_asym:
        k_toe, k_shoulder = args.left_k_toe, args.left_k_shoulder
        assert k_toe is not None and k_shoulder is not None  # left_asym means both are set
        a_vis = render_asym(display, cfg, img, float(k_toe), float(k_shoulder), *dims)
    else:
        a_vis = render_symmetric(display, cfg, img, *dims)
    b_vis = render_asym(display, cfg, img, args.k_toe, args.k_shoulder, *dims)
    assert a_vis.shape == (dims[1], dims[0]), a_vis.shape

    if args.stack:
        out_h = half * 2
        out_vis = np.empty((out_h, display.visual_w), dtype=np.uint8)
        out_vis[:half, :] = a_vis
        out_vis[half:out_h, :] = b_vis
        if args.divider > 0:
            c = half - args.divider // 2
            out_vis[c : c + args.divider, :] = 0  # black ink
    else:
        out_w = half * 2
        out_vis = np.empty((display.visual_h, out_w), dtype=np.uint8)
        out_vis[:, :half] = a_vis
        out_vis[:, half:out_w] = b_vis
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
        ok = send_frame(s, data, "DRC asymmetric A/B")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    a_desc = (
        f"asymmetric toe={args.left_k_toe}/shoulder={args.left_k_shoulder}"
        if left_asym
        else "symmetric"
    )
    print(
        f"\nOn the glass: {side_a} = {a_desc}, {side_b} = asymmetric toe={args.k_toe}/shoulder={args.k_shoulder}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
