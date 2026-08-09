#!/usr/bin/env python3
"""Check the LUT finding on real photographs, not flat swatches.

Flat-swatch screening said the ``hue_aware`` modifier is what drives skin toward
blue. That needs testing on photographs before anyone acts on it: a swatch-derived
recommendation in this project has already failed photo validation once, because a
uniform field exercises none of the spatial behaviour that error diffusion is
mostly about.

The model here is deliberately different from the swatch one. A viewer does not
see individual dots; the eye integrates locally, so what is perceived is the LOCAL
ink mixture. So each ink's coverage map is blurred over a small neighbourhood and
the Yule-Nielsen relation applied per position — turning a dithered raster into a
prediction of what the picture actually looks like.

Skin is then scored on its own. Whole-image error is dominated by the panel's
gamut and says little about the artifact people notice; the complaint was that
faces go blue, so warm pixels are selected and their b* shift measured directly.

The blur radius is a stand-in for chromatic acuity, which is coarser than
luminance acuity. It is a modelling choice, not a measurement, so the sweep
reports several radii rather than hiding behind one.
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

from color_model import INK_NAMES, fit_n, load_records, primaries, yn_mix
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.dither_streaming import dither

D65 = np.array([0.95047, 1.0, 1.08883])


def lab_img(xyz_pct: np.ndarray) -> np.ndarray:
    """(H,W,3) XYZ percent -> (H,W,3) CIELAB."""
    t = xyz_pct / 100.0 / D65
    f = np.where(t > 216 / 24389, np.cbrt(np.maximum(t, 0)), (24389 / 27 * t + 16) / 116)
    return np.stack(
        [116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])],
        axis=-1,
    )


def srgb_img_to_xyz(rgb: np.ndarray) -> np.ndarray:
    s = rgb.astype(np.float64) / 255.0
    lin = np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)
    m = np.linalg.inv(
        np.array(
            [
                [3.2406, -1.5372, -0.4986],
                [-0.9689, 1.8758, 0.0415],
                [0.0557, -0.2040, 1.0570],
            ]
        )
    )
    return lin @ m.T * 100.0


def box_blur(a: np.ndarray, r: int) -> np.ndarray:
    """Separable box blur via summed-area sums; edges use a shrinking window.

    Plain numpy on purpose — scipy is not installed in this environment, and a box
    blur is a good enough stand-in for the eye's local integration.
    """
    if r <= 0:
        return a
    out = a.astype(np.float64)
    for axis in (0, 1):
        n = out.shape[axis]
        c = np.cumsum(out, axis=axis)
        c = np.concatenate([np.zeros_like(np.take(c, [0], axis=axis)), c], axis=axis)
        lo = np.clip(np.arange(n) - r, 0, n)
        hi = np.clip(np.arange(n) + r + 1, 0, n)
        width = (hi - lo).astype(np.float64)
        take_hi = np.take(c, hi, axis=axis)
        take_lo = np.take(c, lo, axis=axis)
        shape = [1] * out.ndim
        shape[axis] = n
        out = (take_hi - take_lo) / width.reshape(shape)
    return out


def predict_image(idx: np.ndarray, prim_mat: np.ndarray, n: float, radius: int) -> np.ndarray:
    """Dithered indices -> predicted perceived XYZ, via locally-averaged coverage."""
    h, w = idx.shape
    frac = np.zeros((h, w, len(INK_NAMES)), dtype=np.float64)
    for i in range(len(INK_NAMES)):
        frac[..., i] = idx == i
    frac = box_blur(frac, radius)
    frac /= np.maximum(frac.sum(axis=-1, keepdims=True), 1e-9)
    flat = frac.reshape(-1, len(INK_NAMES))
    return yn_mix(flat, prim_mat, n).reshape(h, w, 3)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--images", type=Path, default=Path("images/test"))
    ap.add_argument("--radius", type=int, nargs="*", default=[2, 6, 12])
    ap.add_argument(
        "--only",
        nargs="*",
        default=[
            "Actress_Anna_Unterberger-2.jpg",
            "Robert_De_Niro_KVIFF_portrait.jpg",
            "Wayuu_woman_with_sad_face_in_the_market_buying.jpg",
        ],
        help="portraits by default — the artifact under test is about faces",
    )
    ap.add_argument("--luts", nargs="*", default=["hue_aware", "oklab", "cam16ucs", "euclidean"])
    ap.add_argument(
        "--whole-image",
        action="store_true",
        help="score every image overall, not just skin — the LUT change is global",
    )
    args = ap.parse_args(argv)

    records = load_records(args.data)
    prim = primaries(records)
    if any(k not in prim for k in INK_NAMES):
        print("ABORT: not every ink measured.")
        return 1
    mix = [r for r in records if r.get("source") != "ink"]
    n, _ = fit_n(mix, prim, spectral=False)
    prim_mat = np.array([prim[k]["xyz"] for k in INK_NAMES])
    display = DISPLAY_REGISTRY["bigme_f7"]
    print(f"model n = {n:.2f}; panel {display.panel_w}x{display.panel_h}")

    if args.whole_image:
        # Recommending a global LUT change off a skin metric is only half an
        # argument; this asks what it does to everything else. Whole-image dE is
        # gamut-dominated and so moves little by construction, which is the point:
        # the question is whether the change makes anything clearly WORSE.
        exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heif", ".jxl"}
        files = sorted(p for p in args.images.iterdir() if p.suffix.lower() in exts)
        print(f"\nwhole-image mean dE over {len(files)} test images (radius 6)")
        print(f"  {'image':44s} " + "  ".join(f"{lut[:11]:>11s}" for lut in args.luts))
        totals = dict.fromkeys(args.luts, 0.0)
        counted = 0
        for path in files:
            try:
                img = Image.open(path).convert("RGB")
            except Image.DecompressionBombError:
                # The test set deliberately includes a 100 MP image; the server
                # gates these at ingest rather than decoding them. Skipping keeps
                # that policy visible here instead of quietly raising the limit.
                print(f"  {path.name[:44]:44s} (skipped: over PIL's decode limit)")
                continue
            except (OSError, ValueError) as exc:
                print(f"  {path.name[:44]:44s} (unreadable: {type(exc).__name__})")
                continue
            img = img.resize((display.panel_w, display.panel_h), Image.Resampling.LANCZOS)
            src = np.array(img, dtype=np.uint8)
            src_lab = lab_img(srgb_img_to_xyz(src))
            cells = []
            for lut in args.luts:
                cfg = DitherConfig("atkinson", lut, True, 30.0, 12.0)
                idx = dither(src.copy(), cfg, display)
                pred = lab_img(predict_image(idx, prim_mat, n, 6))
                de = float(np.mean(np.linalg.norm(pred - src_lab, axis=-1)))
                totals[lut] += de
                cells.append(f"{de:11.2f}")
            counted += 1
            print(f"  {path.name[:44]:44s} " + "  ".join(cells))
        if counted:
            print(
                f"  {'MEAN':44s} "
                + "  ".join(f"{totals[lut] / counted:11.2f}" for lut in args.luts)
            )
        return 0

    for name in args.only:
        path = args.images / name
        if not path.exists():
            print(f"  (missing {name})")
            continue
        img = Image.open(path).convert("RGB")
        img = img.resize((display.panel_w, display.panel_h), Image.Resampling.LANCZOS)
        src = np.array(img, dtype=np.uint8)
        src_lab = lab_img(srgb_img_to_xyz(src))
        chroma = np.hypot(src_lab[..., 1], src_lab[..., 2])
        hue = np.degrees(np.arctan2(src_lab[..., 2], src_lab[..., 1]))
        # Warm, reasonably colourful, mid-lightness: the skin-dominated region.
        skin = (hue > -40) & (hue < 70) & (chroma > 12) & (src_lab[..., 0] > 20)
        print(f"\n{name}   skin-like pixels: {100 * skin.mean():.1f}%")
        if skin.sum() < 500:
            print("  too few skin pixels to judge")
            continue
        print(f"  {'lut':14s} " + "  ".join(f"r={r:<2d} db*" for r in args.radius))
        for lut in args.luts:
            cfg = DitherConfig("atkinson", lut, True, 30.0, 12.0)
            idx = dither(src.copy(), cfg, display)
            cells = []
            for r in args.radius:
                pred_lab = lab_img(predict_image(idx, prim_mat, n, r))
                db = float(np.mean(pred_lab[..., 2][skin] - src_lab[..., 2][skin]))
                cells.append(f"{db:9.2f}")
            print(f"  {lut:14s} " + "  ".join(cells))

    print(
        "\ndb* is the shift of the blue-yellow axis on skin pixels: negative is\n"
        "bluer than the original. Radii bracket how much the eye integrates; the\n"
        "ranking mattering more than the absolute values."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
