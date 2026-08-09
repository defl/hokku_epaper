#!/usr/bin/env python3
"""Would changing the palette anchors actually improve the picture?

The renderer picks inks by nearest-neighbour against ``palette_measured_rgb``,
and then error-diffusion propagates the residual computed against those same
values. Ours came from a third-party table (``epdoptimize``), not from this
glass, and measurement says they are wrong by a mean of dE 13 after
white-normalising — the real inks are more saturated than the code believes, red
worst by 24.

Changing them is not obviously safe, though: they are not a correction applied at
the end, they are the quantiser's model of reality, so every ink choice in every
image moves at once. This answers the question before any hardware time is spent:

  1. dither a test colour with the CURRENT anchors -> exact ink fractions
  2. predict what the panel really shows, via the fitted Yule-Nielsen model
  3. repeat with CANDIDATE anchors
  4. compare each prediction against what was asked for

Step 2 is the part that makes this honest — the ink fractions are exact (we
generate the raster), and the model that turns them into a colour was fitted to
real measurements of this panel rather than assumed.

The model's own residual is ~2.5 dE, so differences smaller than that are noise
and are reported as such rather than claimed as wins.

Usage:
  python tools/color_evaluate.py --data build/colorcal/archive/<file>.jsonl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from color_campaign_spec import gamut_cube_rgb, gray_ramp_rgb, skin_locus_rgb
from color_model import INK_NAMES, delta_e76, fit_n, lab, load_records, primaries, yn_mix
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.dither_streaming import dither

_M_RGB2XYZ = np.linalg.inv(
    np.array(
        [
            [3.2406, -1.5372, -0.4986],
            [-0.9689, 1.8758, 0.0415],
            [0.0557, -0.2040, 1.0570],
        ]
    )
)


def srgb8_to_xyz_pct(rgb) -> np.ndarray:
    s = np.asarray(rgb, dtype=float) / 255.0
    lin = np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)
    return (_M_RGB2XYZ @ lin) * 100.0


def xyz_pct_to_srgb8(xyz) -> np.ndarray:
    m = np.linalg.inv(_M_RGB2XYZ)
    lin = np.clip(m @ (np.asarray(xyz, dtype=float) / 100.0), 0.0, 1.0)
    s = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
    return np.rint(s * 255)


def measured_palette(prim: dict[str, dict], normalise_white: bool) -> np.ndarray:
    """Measured primaries as an sRGB palette the renderer can consume.

    White-normalising is not cosmetic. The absolute measurements have paper white
    at L* 68, so adopting them raw tells the quantiser that its brightest possible
    ink is a mid grey, and every image is pulled dark. Scaling so the panel's own
    white maps to the palette's existing white keeps the overall level intact and
    changes only the hue/chroma relationships — which is the part measurement
    actually disagreed about.
    """
    xyz = {k: np.asarray(prim[k]["xyz"], dtype=float) for k in INK_NAMES}
    if normalise_white:
        target_white = srgb8_to_xyz_pct(DISPLAY_REGISTRY["bigme_f7"].palette_measured_rgb[1])
        scale = target_white / xyz["white"]
        xyz = {k: v * scale for k, v in xyz.items()}
    return np.array([xyz_pct_to_srgb8(xyz[k]) for k in INK_NAMES], dtype=np.float32)


def evaluate(display, palette, colours, cfg, prim, n, canvas_hw) -> dict[str, np.ndarray]:
    """What the panel would really show vs what was asked for, broken out.

    Aggregate dE is the wrong headline for this device. Most of sRGB is simply
    unreachable — the gamut set scores dE 50 no matter what palette is used — so a
    single number is dominated by a limit no anchor choice can move, and hides the
    part that is actually fixable.

    HUE is the fixable part, and the one that gets noticed: "lips go blue" is a
    hue complaint, not a lightness one. So hue error is reported separately, along
    with the specific direction of the artifact — warm colours drifting toward
    blue — which is what the anchor change should influence if it helps at all.
    """
    h, w = canvas_hw
    prim_mat = np.array([prim[k]["xyz"] for k in INK_NAMES])
    original = display.palette_measured_rgb
    de, dh, dc, dl, blue_shift = [], [], [], [], []
    try:
        display.palette_measured_rgb = palette
        for rgb in colours:
            canvas = np.zeros((h, w, 3), dtype=np.uint8)
            canvas[:, :] = rgb
            idx = dither(canvas, cfg, display)
            frac = np.bincount(idx.ravel(), minlength=len(INK_NAMES)) / idx.size
            got = lab(yn_mix(frac, prim_mat, n))
            want = lab(srgb8_to_xyz_pct(rgb))
            de.append(delta_e76(got, want))
            dl.append(got[0] - want[0])
            cw, cg = float(np.hypot(*want[1:])), float(np.hypot(*got[1:]))
            dc.append(cg - cw)
            hw = float(np.degrees(np.arctan2(want[2], want[1])))
            hg = float(np.degrees(np.arctan2(got[2], got[1])))
            d = (hg - hw + 180.0) % 360.0 - 180.0
            dh.append(d)
            # Warm source (hue -40..70 deg) losing b* is the blue-lips signature.
            if -40.0 <= hw <= 70.0 and cw > 8.0:
                blue_shift.append(got[2] - want[2])
    finally:
        display.palette_measured_rgb = original
    return {
        "de": np.array(de),
        "dl": np.array(dl),
        "dc": np.array(dc),
        "dh": np.array(dh),
        "blue_shift": np.array(blue_shift) if blue_shift else np.array([np.nan]),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--canvas", type=int, nargs=2, default=(96, 160))
    ap.add_argument("--lut", default="hue_aware")
    ap.add_argument(
        "--compare-luts",
        action="store_true",
        help="rank every LUT on skin hue fidelity with the CURRENT anchors",
    )
    args = ap.parse_args(argv)

    records = load_records(args.data)
    prim = primaries(records)
    if any(k not in prim for k in INK_NAMES):
        print("ABORT: not every ink has been measured — no model basis.")
        return 1
    mix = [r for r in records if r.get("source") != "ink"]
    n, _ = fit_n(mix, prim, spectral=False)
    print(f"model: Yule-Nielsen n = {n:.2f}, fitted on {len(mix)} patches")

    display = DISPLAY_REGISTRY["bigme_f7"]
    cfg = DitherConfig("atkinson", args.lut, True, 30.0, 12.0)
    current = np.array(display.palette_measured_rgb, dtype=np.float32)
    cand_norm = measured_palette(prim, normalise_white=True)
    cand_abs = measured_palette(prim, normalise_white=False)

    print(f"\n{'ink':8s} {'current':>16s} {'measured(norm)':>16s} {'measured(abs)':>16s}")
    for i, name in enumerate(INK_NAMES):
        print(
            f"{name:8s} {tuple(int(v) for v in current[i])!s:>16s} "
            f"{tuple(int(v) for v in cand_norm[i])!s:>16s} "
            f"{tuple(int(v) for v in cand_abs[i])!s:>16s}"
        )

    if args.compare_luts:
        # The original complaint was that the LUT drives the artifact ("all but
        # oklab make lips blue"), and the anchor results above say the anchors do
        # not. This ranks the knob that is actually suspected, on the colours the
        # complaint is about, with everything else held fixed.
        skin = skin_locus_rgb(60, 20260808)
        print("\nskin, CURRENT anchors, atkinson+serpentine — LUT comparison")
        print(f"{'lut':22s} {'dE':>7s} {'|dhue|':>8s} {'dChroma':>8s} {'blue shift':>11s}")
        rows = []
        for lut in (
            "bw",
            "euclidean",
            "euclidean_weighted",
            "hue_aware",
            "hue_aware_weighted",
            "oklab",
            "oklab_hue_aware",
            "cam16ucs",
            "cam16ucs_hue_aware",
        ):
            c = DitherConfig("atkinson", lut, True, 30.0, 12.0)
            r = evaluate(display, current, skin, c, prim, n, tuple(args.canvas))
            rows.append((lut, r))
            print(
                f"{lut:22s} {r['de'].mean():7.2f} {np.abs(r['dh']).mean():8.2f} "
                f"{r['dc'].mean():8.2f} {np.nanmean(r['blue_shift']):11.2f}"
            )
        best = min(rows, key=lambda t: abs(np.nanmean(t[1]["blue_shift"])))
        print(f"\nleast blue shift on skin: {best[0]}")
        return 0

    sets = {
        "skin (60)": skin_locus_rgb(60, 20260808),
        "greys (13)": gray_ramp_rgb(13),
        "gamut 5^3 (125)": gamut_cube_rgb(5),
    }
    print(f"\nrequested vs predicted-on-glass, LUT={args.lut}")
    for label, colours in sets.items():
        res = {
            "current": evaluate(display, current, colours, cfg, prim, n, tuple(args.canvas)),
            "measured(norm)": evaluate(
                display, cand_norm, colours, cfg, prim, n, tuple(args.canvas)
            ),
            "measured(abs)": evaluate(display, cand_abs, colours, cfg, prim, n, tuple(args.canvas)),
        }
        print(f"\n  {label}")
        print(
            f"    {'palette':16s} {'dE':>7s} {'|dhue|':>8s} {'dChroma':>8s} "
            f"{'dL':>7s} {'blue shift':>11s}"
        )
        for name, r in res.items():
            print(
                f"    {name:16s} {r['de'].mean():7.2f} {np.abs(r['dh']).mean():8.2f} "
                f"{r['dc'].mean():8.2f} {r['dl'].mean():7.2f} "
                f"{np.nanmean(r['blue_shift']):11.2f}"
            )

    print(
        "\nRead this as a screening result, not a decision. It compares palettes\n"
        "through a model with a ~2.5 dE residual, on flat fields, ignoring the\n"
        "tonal chain that runs ahead of the dither in production. It is enough to\n"
        "say which candidate is worth putting on real glass — no more."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
