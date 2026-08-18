#!/usr/bin/env python3
"""A/B the 3-D gamut-correction LUT on real glass: with vs without — for a
human to judge.

Both ``bigme_f7`` and ``huessen_epf1301`` ship a ``correction_lut_path`` on
their ``display.py`` now (see ``docs/screens/*/measurements/findings.md`` and
``tools/color_lut_build.py``), which ``ImageRenderer`` applies right after the
DRC stage to correct gamut/hue drift the palette-selection LUT doesn't know
about. It was validated offline against held-out point sets before this —
this is the real-glass check the offline numbers can't replace.

Same pattern as ``tools/drc_anchor_ab.py`` (see that file's docstring for the
half-canvas/rotation reasoning, unchanged here): each side renders the WHOLE
photo at half-panel width via the pipeline's own fit logic, composed with a
black divider, pushed as one frame.

One wrinkle drc_anchor_ab.py doesn't have: ``ImageRenderer._correction_lut()``
is cached by model_id (``_cached_correction_lut``, an lru_cache — unlike
``_drc_anchors()``, which is cheap enough to not need caching), so toggling
``display.correction_lut_path`` between renders in the same process requires
explicitly clearing that cache each time, or the second render would silently
reuse the first's cached result.
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
from hokku.webserver.image_renderer import ImageRenderer, _cached_correction_lut
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import DEFAULT_IMAGE_CONFIG
from send_frame import open_device, send_frame


def to_visible(idx: np.ndarray, display) -> np.ndarray:
    """Panel-memory index array -> as-mounted landscape orientation."""
    return np.rot90(idx, k=1) if display.panel_rotated else idx


def to_panel(idx: np.ndarray, display) -> np.ndarray:
    """As-mounted landscape orientation -> panel-memory index array."""
    return np.rot90(idx, k=3) if display.panel_rotated else idx


def render_half(display, cfg, img: Image.Image, use_correction: bool, half_w: int) -> np.ndarray:
    """Render the WHOLE photo into a half_w x visual_h canvas (VISIBLE
    orientation), with the correction LUT temporarily enabled or disabled.

    See drc_anchor_ab.render_half for why canvas_w/canvas_h swap on a
    panel_rotated screen.
    """
    visual_h = display.visual_h
    canvas_w, canvas_h = (visual_h, half_w) if display.panel_rotated else (half_w, visual_h)
    original = display.correction_lut_path
    try:
        display.correction_lut_path = original if use_correction else None
        _cached_correction_lut.cache_clear()
        renderer = ImageRenderer(dither=StreamingDither(display), display=display)
        idx = renderer.render_indices(img.copy(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h)
    finally:
        display.correction_lut_path = original
        _cached_correction_lut.cache_clear()
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
    if display.correction_lut_path is None:
        raise SystemExit(f"{args.model} has no correction_lut_path set — nothing to A/B")

    print("  LEFT  (uncorrected) no gamut-correction LUT")
    print(f"  RIGHT (corrected)   correction_lut_path = {display.correction_lut_path}")

    img = Image.open(args.image).convert("RGB")
    cfg = DEFAULT_IMAGE_CONFIG
    half = display.visual_w // 2

    left_vis = render_half(display, cfg, img, False, half)
    right_vis = render_half(display, cfg, img, True, half)
    assert left_vis.shape == (display.visual_h, half), left_vis.shape

    # No pre-swap here — see drc_anchor_ab.py for the full account. An earlier
    # version of both files swapped which variant filled which half before
    # the 180° flip, on the (wrong) assumption that the rotation would
    # otherwise swap physical sides. Confirmed empirically on real glass with
    # a solid red/green marker frame: the software np.rot90(k=2) below and
    # the panel's own upside-down bench mount cancel completely on their
    # own — content placed at PRE-rotation columns [0:half] lands on the
    # physical LEFT. The swap was actively wrong, not just unnecessary; it
    # mislabelled every Huessen correction-LUT A/B this tool ran.
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
        ok = send_frame(s, data, "correction LUT A/B")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    print("\nOn the glass: LEFT = uncorrected, RIGHT = gamut-correction LUT applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
