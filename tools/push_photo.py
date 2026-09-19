#!/usr/bin/env python3
"""Render one photo through a named preset (default: production's shipped
default) at full panel resolution and push it — no A/B split, just "what
does this actually look like."
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--image", type=Path, required=True)
    ap.add_argument("--port", required=True)
    ap.add_argument("--preset", default="default_general", choices=sorted(PRESET_IMAGE_CONFIGS))
    ap.add_argument("--console-timeout", type=float, default=180.0)
    ap.add_argument("--save", type=Path, help="write the preview as a PNG, no upload")
    ap.add_argument(
        "--bench-flip180",
        action="store_true",
        help="rotate 180° before sending — for a unit that is physically mounted "
        "upside down on the test bench (e.g. Huessen for USB access), not a "
        "property of the panel or the production pipeline",
    )
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    cfg = PRESET_IMAGE_CONFIGS[args.preset]
    print(f"  preset: {args.preset}")

    img = Image.open(args.image).convert("RGB")
    renderer = ImageRenderer(dither=StreamingDither(display), display=display)
    idx = renderer.render_indices(img, cfg, Orientation.LANDSCAPE, display.panel_w, display.panel_h)
    if args.bench_flip180:
        idx = np.rot90(idx, k=2)
    data = display.indices_to_panel_bytes(idx)

    if args.save:
        vis = np.rot90(idx, k=1) if display.panel_rotated else idx
        rgb = np.rint(display.palette_measured_rgb).clip(0, 255).astype(np.uint8)[vis]
        Image.fromarray(rgb).save(args.save)
        print(f"  preview -> {args.save}")
        return 0

    print(f"opening {args.port} ({args.model})...", flush=True)
    s = open_device(args.port, args.model, timeout_s=args.console_timeout, interactive=False)
    if s is None:
        return 1
    try:
        ok = send_frame(s, data, "push_photo")
    finally:
        s.close()
    if not ok:
        print("upload failed")
        return 1
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
