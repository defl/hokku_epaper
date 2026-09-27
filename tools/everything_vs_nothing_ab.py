#!/usr/bin/env python3
"""A/B the full pipeline against the SAME crop/fit + SAME dither/palette-LUT
step, with every stage between them (autocontrast, gamma, midtone,
brightness, contrast, CLAHE, unsharp, adaptive saturation, DRC, gamut-
correction LUT, pre-dither noise) skipped entirely by default — not set to
an identity value, actually not run. --right-stages adds specific stages
back one at a time (in real pipeline order) to isolate how much each one
contributes; DRC and the correction LUT are never reachable this way since
they run in render_indices itself, outside _apply_prepare_enhancements.

This isolates "how much is all the tonal/DRC/LUT machinery doing" with an
apples-to-apples final ink-selection step: both sides use the exact same
DitherConfig (algorithm, palette LUT, serpentine, hue cutoff, neutral
chroma), so any difference comes only from what the dither was FED, not how
it picked inks.

The RIGHT side temporarily monkeypatches
hokku.webserver.image_abc._apply_prepare_enhancements to a partial
reimplementation (_partial_prepare) so
AbstractImageRenderer._prepare_canvas's crop/fit/rotate logic can be reused
unchanged, then dithers the result directly via dither_streaming.dither() —
bypassing render_indices entirely, so DRC and the correction LUT (which
render_indices calls itself, outside _apply_prepare_enhancements) never run
regardless of --right-stages.

Same half-canvas pattern as the other *_ab.py tools in this directory.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import hokku.webserver.image_abc as image_abc_module
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import StreamingDither, dither
from hokku.webserver.image_renderer import ImageRenderer
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import DEFAULT_IMAGE_CONFIG
from send_frame import open_device, send_frame

# Same order _apply_prepare_enhancements runs them in (image_abc.py).
STAGE_ORDER = [
    "autocontrast",
    "gamma",
    "midtone",
    "brightness",
    "contrast",
    "clahe",
    "unsharp",
    "saturation",
]


def _partial_prepare(canvas, cfg, stages: set[str]):
    """Re-implements _apply_prepare_enhancements's stage order, running only
    the requested subset — the rest are skipped entirely (not identity)."""
    if "autocontrast" in stages:
        canvas = ImageOps.autocontrast(canvas, cutoff=cfg.prepare_autocontrast_cutoff)
    if "gamma" in stages:
        gamma_lut = [int(((i / 255.0) ** cfg.prepare_gamma) * 255) for i in range(256)] * 3
        canvas = canvas.point(gamma_lut)
    if "midtone" in stages and cfg.prepare_midtone != 1.0:
        exp = 1.0 / cfg.prepare_midtone
        midtone_lut = [round(255 * (i / 255.0) ** exp) if i > 0 else 0 for i in range(256)] * 3
        canvas = canvas.point(midtone_lut)
    if "brightness" in stages:
        canvas = ImageEnhance.Brightness(canvas).enhance(cfg.prepare_brightness)
    if "contrast" in stages:
        canvas = ImageEnhance.Contrast(canvas).enhance(cfg.prepare_contrast)
    if "clahe" in stages and cfg.clahe_clip_limit > 0.0:
        arr = np.asarray(canvas, dtype=np.uint8)
        lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
        clahe = cv2.createCLAHE(clipLimit=cfg.clahe_clip_limit, tileGridSize=(8, 8))
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        canvas = Image.fromarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))
    if "unsharp" in stages:
        canvas = canvas.filter(
            ImageFilter.UnsharpMask(
                radius=cfg.prepare_usm_radius, percent=cfg.prepare_usm_amount, threshold=3
            )
        )
    if "saturation" in stages and cfg.adaptive_saturate_space == "off":
        canvas = ImageEnhance.Color(canvas).enhance(cfg.color_enhance)
    return canvas


def to_visible(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=1) if display.panel_rotated else idx


def to_panel(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=3) if display.panel_rotated else idx


def render_everything(display, cfg, img: Image.Image, half_w: int) -> np.ndarray:
    visual_h = display.visual_h
    canvas_w, canvas_h = (visual_h, half_w) if display.panel_rotated else (half_w, visual_h)
    renderer = ImageRenderer(dither=StreamingDither(display), display=display)
    idx = renderer.render_indices(img.copy(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h)
    return to_visible(idx, display)


def render_partial(display, cfg, img: Image.Image, stages: set[str], half_w: int) -> np.ndarray:
    """Same crop/fit + same dither; only `stages` run in between (in pipeline
    order), everything else — including DRC and the correction LUT, which
    render_indices applies itself outside _apply_prepare_enhancements —
    skipped entirely (not identity-valued — not called at all)."""
    visual_h = display.visual_h
    canvas_w, canvas_h = (visual_h, half_w) if display.panel_rotated else (half_w, visual_h)
    renderer = ImageRenderer(dither=StreamingDither(display), display=display)

    original = image_abc_module._apply_prepare_enhancements
    try:
        image_abc_module._apply_prepare_enhancements = lambda canvas, cfg, keepout=None: (
            _partial_prepare(canvas, cfg, stages)
        )
        arr, padding_mask = renderer._prepare_canvas(
            img.copy(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h
        )
    finally:
        image_abc_module._apply_prepare_enhancements = original

    idx = dither(arr, cfg.dither, display)
    idx[padding_mask] = 1  # white ink, matching render_indices's own letterbox handling
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
        "--right-stages",
        default="",
        help=f"comma-separated stages to run on the RIGHT side, in pipeline order "
        f"(default: none). Choices: {','.join(STAGE_ORDER)}",
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
    right_stages = {s.strip() for s in args.right_stages.split(",") if s.strip()}
    unknown = right_stages - set(STAGE_ORDER)
    if unknown:
        raise SystemExit(f"unknown stage(s): {sorted(unknown)} — choices: {STAGE_ORDER}")
    right_label = "+".join(s for s in STAGE_ORDER if s in right_stages) or "nothing"

    print("  LEFT  everything (current default pipeline, full)")
    print(f"  RIGHT {right_label} (same crop/fit, same dither+palette LUT)")

    img = Image.open(args.image).convert("RGB")
    cfg = DEFAULT_IMAGE_CONFIG
    half = display.visual_w // 2

    left_vis = render_everything(display, cfg, img, half)
    right_vis = render_partial(display, cfg, img, right_stages, half)
    assert left_vis.shape == (display.visual_h, half), left_vis.shape
    assert right_vis.shape == (display.visual_h, half), right_vis.shape

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
        ok = send_frame(s, data, "everything-vs-nothing A/B")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    print(f"\nOn the glass: LEFT = everything, RIGHT = {right_label} (same dither).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
