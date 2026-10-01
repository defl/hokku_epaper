#!/usr/bin/env python3
"""A/B the DRC's lightness anchors on real glass: the old palette-table-derived
range against this panel's own measured range — for a human to judge.

Both ``bigme_f7`` and ``huessen_epf1301`` ship a ``drc_anchor_l`` on their
``display.py`` now (see ``docs/screens/*/measurements/findings.md``), which
``compress_dynamic_range`` uses to map a source image's L* onto the panel's
*actual reachable* range. Before that fix, the DRC read the range from
whichever ink table the model's ``palette_measured_rgb`` happened to imply,
which was 11-13 L* wider than either panel can really show — the F7 lost half
a test portrait to flat black over it; Huessen measured the same shape of bug
but was never shipped mis-anchored (see the doc for the exact numbers).

Nothing in the pipeline is changed here. This renders the SAME photo twice
through the real production path, toggling only the display's own
``drc_anchor_l`` between ``None`` (the old, palette-table-derived behaviour)
and its current value, then puts both renders on the same glass at once —
side by side is how the difference is actually seen; sequential viewing on
two separate pushes relies on colour memory, which is poor.

Each side is rendered at HALF-panel width rather than rendering the whole
panel twice and slicing. Slicing shows the same half of the photo on both
sides — a fair comparison of a useless composition (half a face, twice).
Rendering to the half canvas lets the pipeline's own fit logic show the WHOLE
photo, smaller, on each side, so both sides get an identical composition and
the DRC anchors remain the only difference.

Geometry note for a ``panel_rotated`` screen (Huessen): the production canvas
is composed in the panel's own memory order (portrait). Asking for a
half-VISIBLE-width render means swapping which of ``canvas_w``/``canvas_h``
is halved before calling ``render_indices`` (see ``render_half``), then
rotating the result into VISIBLE (as-mounted) orientation to place it — the
same ``rotate(-90, expand=True)`` / ``np.rot90(..., k=3)`` pair
``_prepare_canvas`` itself uses (see ``image_abc.py``), just applied to a
finished index array instead of a PIL image.
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
from hokku.webserver.presets import DEFAULT_IMAGE_CONFIG
from send_frame import open_device, send_frame


def to_visible(idx: np.ndarray, display) -> np.ndarray:
    """Panel-memory index array -> as-mounted landscape orientation."""
    return np.rot90(idx, k=1) if display.panel_rotated else idx


def to_panel(idx: np.ndarray, display) -> np.ndarray:
    """As-mounted landscape orientation -> panel-memory index array."""
    return np.rot90(idx, k=3) if display.panel_rotated else idx


def render_half(
    display, cfg, img: Image.Image, anchor: tuple[float, float] | None, half_w: int
) -> np.ndarray:
    """Render the WHOLE photo into a half_w x visual_h canvas (VISIBLE
    orientation), with drc_anchor_l temporarily overridden.

    On a rotated panel, canvas_w/canvas_h must be given in the panel's own
    memory order, which _prepare_canvas swaps back to visible order for a
    landscape render — so asking for a half-width VISIBLE canvas means
    passing (canvas_w=visual_h, canvas_h=half_w) here, not (half_w, visual_h).
    """
    visual_h = display.visual_h
    canvas_w, canvas_h = (visual_h, half_w) if display.panel_rotated else (half_w, visual_h)
    original = display.drc_anchor_l
    try:
        display.drc_anchor_l = anchor
        renderer = ImageRenderer(dither=StreamingDither(display), display=display)
        idx = renderer.render_indices(img.copy(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h)
    finally:
        display.drc_anchor_l = original
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
    mapped_anchor = display.drc_anchor_l
    if mapped_anchor is None:
        raise SystemExit(f"{args.model} has no drc_anchor_l set — nothing to A/B")

    print("  LEFT  (unprocessed) DRC range derived from palette_measured_rgb")
    print(
        f"  RIGHT (processed)   DRC range L* {mapped_anchor[0]:.2f} .. {mapped_anchor[1]:.2f}  <- measured"
    )

    img = Image.open(args.image).convert("RGB")
    cfg = DEFAULT_IMAGE_CONFIG
    half = display.visual_w // 2

    left_vis = render_half(display, cfg, img, None, half)
    right_vis = render_half(display, cfg, img, mapped_anchor, half)
    assert left_vis.shape == (display.visual_h, half), left_vis.shape

    # No pre-swap here. An EARLIER version of this file swapped which variant
    # filled which half before the 180° flip, reasoning that the rotation
    # reverses column order and would otherwise swap physical sides — that
    # reasoning was wrong, and it silently mislabelled every Huessen A/B this
    # tool ever ran with --bench-flip180 (confirmed empirically on real glass
    # with a solid red/green marker frame: content placed at PRE-rotation
    # columns [0:half] lands on the physical LEFT once both the software
    # np.rot90(k=2) below AND the panel's own upside-down bench mount are in
    # effect — the two 180°s cancel completely, on BOTH axes at once, not
    # just vertically. No content swap is needed to compensate; only the
    # np.rot90 itself is needed, and it alone is already correct).
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
        ok = send_frame(s, data, "DRC anchor A/B")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    print("\nOn the glass: LEFT = unprocessed (palette-table) DRC range, RIGHT = processed/mapped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
