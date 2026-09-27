#!/usr/bin/env python3
"""Put several points of the lightness-vs-chroma dial on glass and photograph them.

The dial is ``gamut_clamp.py --blend``: 0 keeps a photograph's lightness and lets
strong colour go pale, 1 keeps the colour and lets the picture go dark. Both
extremes were measured and both are wrong, in opposite directions, so the setting
is a judgement about what a photograph should give up rather than something a
metric can settle — every metric tried here preferred an extreme, and the one
that looked best to a person was in the middle.

So this renders a real image at several blends, photographs each off the panel,
converts the photographs back to colorimetry, and lays them beside the source at
the same scale. The output is something to look at, which is the only instrument
that has been right every time in this investigation.

    python tools/blend_sweep.py --port COM4 --bench-flip180 \
        --server-image 20080529_035054000_iOS.jpg --blends 0.25 0.5 0.75
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import cam_rig
import panel_arms
from cam_compare import source_canvas
from cam_figures import _row
from correction_ab import push
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver import image_renderer
from hokku_server import DEFAULT_BASE, fetch_original, image_config
from panel_arms import arm_config, arm_display, parse_arm, render_arm

ARM = "palette+gmap+nblack"
M_XYZ2RGB = np.array(
    [[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415], [0.0557, -0.2040, 1.0570]]
)


def xyz_to_srgb(xyz: np.ndarray) -> np.ndarray:
    lin = np.clip(xyz / 100.0 @ M_XYZ2RGB.T, 0, 1)
    s = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(s * 255, 0, 255).astype(np.uint8)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--port", required=True)
    ap.add_argument("--bench-flip180", action="store_true")
    ap.add_argument("--console-timeout", type=float, default=180.0)
    ap.add_argument("--outdir", type=Path, default=Path("build/camcal"))
    ap.add_argument("--server", default=DEFAULT_BASE)
    ap.add_argument("--server-image", required=True)
    ap.add_argument("--blends", type=float, nargs="*", default=[0.25, 0.5, 0.75])
    ap.add_argument("--settle", type=int, default=2000)
    ap.add_argument("--reuse-shots", action="store_true")
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    outdir = args.outdir
    fit = json.loads((outdir / "colour_fit.json").read_text(encoding="utf-8"))
    coeffs = np.array(fit["coeffs"])
    cam_rig.check_fit_sanity(coeffs, float(fit["white_y"]), float(fit["black_y"]))
    white = cam_rig.load_linear(outdir / "flat_white_shot.jpg")
    black = cam_rig.load_linear(outdir / "flat_black_shot.jpg")
    illum, glare = cam_rig.photometric_fields(
        white.raw.rgb, black.raw.rgb, float(fit["white_y"]), float(fit["black_y"])
    )

    path = fetch_original(args.server_image, outdir / "server_images", base=args.server)
    img = Image.open(path).convert("RGB")
    cfg = image_config(base=args.server)
    tiles = [("source image", source_canvas(path, display, "default_general"))]

    for blend in args.blends:
        stem = f"blend_{blend:.2f}"
        shot_path = outdir / f"{stem}__shot.jpg"
        lut = outdir / f"lut_b{blend}.npy"
        if not lut.exists():
            print(f"missing {lut} — build it with gamut_clamp.py --blend {blend}")
            return 1
        # The renderer memoises correction LUTs by model id, so swapping the file
        # under it is invisible unless the cache is dropped. Without this every
        # blend after the first silently renders with the first one's LUT.
        shutil.copy(lut, panel_arms.GAMUT_LUT_PATH)
        image_renderer._cached_correction_lut.cache_clear()

        toggles = parse_arm(ARM)
        arm_disp = arm_display(display, toggles)
        idx = render_arm(arm_disp, img, arm_config(cfg, toggles), display.panel_w, display.panel_h)
        expected = (
            np.rint(display.palette_measured_rgb)
            .clip(0, 255)
            .astype(np.uint8)[np.rot90(idx, k=1) if display.panel_rotated else idx]
        )
        cv2.imwrite(
            str(outdir / f"{stem}__expected.png"), cv2.cvtColor(expected, cv2.COLOR_RGB2BGR)
        )

        if not args.reuse_shots:
            if not push(
                idx, arm_disp, display, args.port, args.bench_flip180, args.console_timeout
            ):
                print("upload failed")
                return 1
            cam_rig.capture_linear(shot_path, settle_ms=args.settle, verbose=False)

        shot = cam_rig.load_linear(shot_path)
        corrected = cam_rig.apply_photometric(shot.raw.rgb, illum, glare)
        homography, _info = cam_rig.content_homography(shot.jpeg_bgr, expected, verbose=False)
        rect = cam_rig.rectify_linear(corrected, cam_rig.scale_homography(homography), display)
        measured = xyz_to_srgb(cam_rig.apply_camera_fit(rect / 100.0, coeffs))
        tiles.append((f"blend {blend:g}", measured))
        print(f"  blend {blend:g}: photographed")

    x0, y0, x1, y1 = 140, 60, 1530, 1080
    out = _row([(t, im[y0:y1, x0:x1]) for t, im in tiles], 560)
    dest = outdir / f"blend_sweep_{Path(args.server_image).stem[:20]}.png"
    cv2.imwrite(str(dest), cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
    print(f"  -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
