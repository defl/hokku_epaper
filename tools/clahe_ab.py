#!/usr/bin/env python3
"""A/B CLAHE on real glass: the new default (clip_limit=0.0, disabled) against
the old value (clip_limit=1.75) — closes the loop confirming the shipped
presets.py change renders the same as what was judged "better by a lot" when
this A/B first ran (before that judgement, this tool compared 1.75 against a
0.0 override rather than two fixed values).

Same half-canvas/no-swap/flip pattern as drc_anchor_ab.py, color_lut_ab.py,
and drc_scurve_ab.py (see drc_anchor_ab.py's docstring for the geometry
reasoning, unchanged here). Toggles ONLY cfg.clahe_clip_limit — same DRC,
same anchor, same everything else on both sides, so this isolates CLAHE's
contribution specifically.

Background: traced L* through every pre-DRC pipeline stage on a synthetic
gradient wedge and found CLAHE reversing most of what the preceding
Contrast(1.1) stage had just done, specifically in smooth/low-local-detail
regions (a gradient, or a flat background) — CLAHE's job is to equalize
LOCAL contrast per 8x8 tile, so a smooth area's histogram gets renormalized
toward the same spread as busier, more textured tiles, undoing global
contrast that isn't backed by local detail. That's the suspected reason a
DRC-level S-curve had so little visible effect: CLAHE, which runs BEFORE
the DRC, had already absorbed most of the available contrast difference by
the time DRC saw the image.
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
    """Render the WHOLE photo into a half_w x visual_h canvas (VISIBLE
    orientation) with the given cfg (already has clahe_clip_limit set)."""
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

    # presets.py now ships clahe_clip_limit=0.0 as the default (this A/B is what
    # justified that change) — so DEFAULT_IMAGE_CONFIG IS the "off" side today.
    # The old 1.75 is hardcoded here rather than read from anywhere, since
    # nothing in the codebase carries that value any more.
    _OLD_CLAHE_CLIP_LIMIT = 1.75
    print(f"  LEFT  (new default) clahe_clip_limit={DEFAULT_IMAGE_CONFIG.clahe_clip_limit}")
    print(f"  RIGHT (old value)   clahe_clip_limit={_OLD_CLAHE_CLIP_LIMIT}")

    img = Image.open(args.image).convert("RGB")
    cfg_off = DEFAULT_IMAGE_CONFIG
    cfg_on = replace(cfg_off, clahe_clip_limit=_OLD_CLAHE_CLIP_LIMIT)
    half = display.visual_w // 2

    left_vis = render_half(display, cfg_off, img, half)
    right_vis = render_half(display, cfg_on, img, half)
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
        ok = send_frame(s, data, "CLAHE A/B")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    print("\nOn the glass: LEFT = new default (CLAHE off), RIGHT = old value (clip_limit=1.75).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
