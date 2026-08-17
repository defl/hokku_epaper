#!/usr/bin/env python3
"""Build a 3-D RGB->RGB gamut-correction LUT from a panel's colour campaign.

The ``gamut_dense`` phase (729 points, a uniform 9x9x9 sRGB grid) was collected
specifically to support this — see docs/screens/<model>/measurements/findings.md.
It is NOT used directly as the correction's ground truth, for a documented reason:
those 729 points were measured under DitherConfig(hue_cutoff_deg=30,
neutral_chroma=12), but production ships (hue_cutoff_deg=95, neutral_chroma=8)
(presets.DEFAULT_IMAGE_CONFIG). Roughly a quarter of the 729 points select a
DIFFERENT physical ink under the two configs (checked below, per-model) — using
the raw measured Lab at those nodes would silently train the correction against
a rendering pipeline that isn't the one actually shipping.

Instead: fit the panel's ink-mixing PHYSICS (Yule-Nielsen n + measured primaries
— both come from SOLID ink patches, unaffected by which LUT config was used),
then use that fitted model to SIMULATE "requested RGB -> predicted on-glass Lab"
for the shipped config directly, via color_evaluate.predict_lab_on_glass — which
dithers a real canvas through the actual production dither() and predicts colour
from the resulting ink fractions. This sidesteps the config mismatch: it depends
only on the (config-independent) physics model, not on which config the 729
points happened to be measured under.

Before trusting the simulator for anything else, it's checked against the
subset of gamut_dense points where ink selection agrees between the measured
and shipped configs — this must land close to the model's own fitted residual,
or nothing downstream is trustworthy.

The correction itself is an inverse lookup: for a requested RGB, find what RGB
to feed the pipeline so its predicted on-glass Lab lands closer to the
requested RGB's own naive sRGB Lab. Built via a dense forward grid (steps^3
nodes, each simulated) + K-nearest-neighbour inverse-distance-weighted
inversion in Lab space (not 1-nearest-neighbour, which collapses many adjacent
requests onto one candidate and produces a patchy, banding-prone LUT) + a light
box-filter smoothing pass.

Usage:
  python tools/color_lut_build.py --model bigme_f7 --dry-run
  python tools/color_lut_build.py --model bigme_f7
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from color_campaign_spec import gamut_cube_rgb, gray_ramp_rgb, skin_locus_rgb
from color_evaluate import predict_lab_on_glass, simulate_ink_fraction, srgb8_to_xyz_pct
from color_model import INK_NAMES, delta_e76, fit_n, lab, load_records, primaries
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.image_renderer import ImageRenderer
from hokku.webserver.presets import DEFAULT_IMAGE_CONFIG

# The config the gamut_dense phase was actually measured under (see module
# docstring) — NOT what ships. Only used to classify which measured points are
# safe to sanity-check the simulator against.
MEASURED_GAMUT_CFG = DitherConfig("atkinson", "hue_aware", True, 30.0, 12.0)

# Max total-variation distance between the measured-config and shipped-config
# SIMULATED ink-fraction vectors for a gamut_dense point to count as "clean".
# A single-cell LUT-lookup check (comparing only the ink chosen for the exact
# nominal RGB) is not enough: error diffusion spreads a flat field's pixel
# values around that nominal value, and nearby values can cross a
# hue_cutoff_deg decision boundary even when the nominal cell itself agrees —
# confirmed directly: rgb=(223,159,0) picks the SAME ink at its nominal value
# under both configs, but the two configs' full dithered fraction vectors are
# (white .79, red .19) vs (yellow .91, red .09) — a completely different ink
# mix, invisible to a single-cell check.
TVD_CLEAN_THRESHOLD = 0.05


def classify_config_sensitivity(
    display, gamut_records: list[dict], canvas_hw: tuple[int, int]
) -> tuple[list[dict], list[dict]]:
    """Split gamut_dense records into 'clean' (the real production dither's
    SIMULATED ink-fraction mix agrees between the measured and shipped config)
    and 'sensitive' (it doesn't)."""
    shipped_cfg = DEFAULT_IMAGE_CONFIG.dither
    clean, sensitive = [], []
    for r in gamut_records:
        rgb = tuple(r["rgb"])
        frac_m = simulate_ink_fraction(display, rgb, MEASURED_GAMUT_CFG, canvas_hw)
        frac_s = simulate_ink_fraction(display, rgb, shipped_cfg, canvas_hw)
        tvd = 0.5 * float(np.abs(frac_m - frac_s).sum())
        (clean if tvd <= TVD_CLEAN_THRESHOLD else sensitive).append(r)
    return clean, sensitive


def sanity_check(
    display, clean_records: list[dict], prim_mat: np.ndarray, n: float, canvas_hw
) -> np.ndarray:
    """dE76 between the forward simulator (at the SHIPPED config) and real
    measurement, restricted to gamut_dense points immune to the config
    mismatch. Should land close to the model's own fitted residual."""
    des = []
    for r in clean_records:
        predicted = predict_lab_on_glass(
            display, tuple(r["rgb"]), DEFAULT_IMAGE_CONFIG.dither, prim_mat, n, canvas_hw
        )
        measured = lab(np.asarray(r["xyz_d65_pct"]))
        des.append(delta_e76(predicted, measured))
    return np.array(des)


def build_forward_grid(
    display, prim_mat: np.ndarray, n: float, steps: int, canvas_hw: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """(steps,steps,steps,3) uint8 RGB grid + matching float32 predicted-Lab grid,
    simulated at the SHIPPED dither config."""
    cube = np.array(gamut_cube_rgb(steps), dtype=np.uint8).reshape(steps, steps, steps, 3)
    flat_rgb = cube.reshape(-1, 3)
    flat_lab = np.empty((flat_rgb.shape[0], 3), dtype=np.float32)
    total = flat_rgb.shape[0]
    for i, rgb in enumerate(flat_rgb):
        flat_lab[i] = predict_lab_on_glass(
            display, tuple(int(c) for c in rgb), DEFAULT_IMAGE_CONFIG.dither, prim_mat, n, canvas_hw
        )
        if (i + 1) % 200 == 0 or i + 1 == total:
            print(f"    forward grid: {i + 1}/{total}", end="\r", flush=True)
    print()
    return cube, flat_lab.reshape(steps, steps, steps, 3)


def invert_grid(grid_rgb: np.ndarray, grid_lab: np.ndarray, k: int) -> np.ndarray:
    """KNN inverse-distance-weighted inversion.

    Reuses the forward grid's own RGB set as both the simulated-outcome set
    AND the correction domain (one build pass serves both). For each domain
    node, treat its own RGB as a REQUEST, find the k forward-grid nodes whose
    predicted Lab is closest to that request's naive sRGB Lab, and blend their
    RGBs by inverse squared distance. This is deliberately not 1-nearest-
    neighbour: a single nearest choice collapses many adjacent requests onto
    one candidate RGB, producing flat plateaus that jump at cell boundaries —
    visible as new dither banding once trilinear-interpolated at render time.
    """
    steps = grid_rgb.shape[0]
    flat_rgb = grid_rgb.reshape(-1, 3).astype(np.float32)
    flat_lab = grid_lab.reshape(-1, 3).astype(np.float32)
    n_nodes = flat_rgb.shape[0]

    target_lab = np.array([lab(srgb8_to_xyz_pct(rgb)) for rgb in flat_rgb], dtype=np.float32)

    diff = target_lab[:, None, :] - flat_lab[None, :, :]
    d2 = np.sum(diff * diff, axis=2)  # (n_nodes, n_nodes)

    k = min(k, n_nodes)
    nn_idx = np.argpartition(d2, k - 1, axis=1)[:, :k]
    nn_d2 = np.take_along_axis(d2, nn_idx, axis=1)
    w = 1.0 / (nn_d2 + 1e-3)
    w /= w.sum(axis=1, keepdims=True)

    nn_rgb = flat_rgb[nn_idx]  # (n_nodes, k, 3)
    corrected = np.sum(nn_rgb * w[:, :, None], axis=1)
    return corrected.reshape(steps, steps, steps, 3).astype(np.float32)


def smooth_grid(grid: np.ndarray, radius: int) -> np.ndarray:
    """Edge-clamped box-filter smoothing over all 3 grid axes.

    Removes residual node-to-node jitter from KNN near-ties (adjacent domain
    nodes occasionally picking a visibly different neighbour set even where
    the underlying physical response is smooth). Not a correctness
    requirement, a regularization pass.
    """
    if radius <= 0:
        return grid
    n = grid.shape[0]
    padded = np.pad(
        grid, ((radius, radius), (radius, radius), (radius, radius), (0, 0)), mode="edge"
    )
    out = np.zeros_like(grid, dtype=np.float32)
    count = 0
    for di in range(-radius, radius + 1):
        for dj in range(-radius, radius + 1):
            for dk in range(-radius, radius + 1):
                out += padded[
                    radius + di : radius + di + n,
                    radius + dj : radius + dj + n,
                    radius + dk : radius + dk + n,
                    :,
                ]
                count += 1
    return out / count


def validation_report(display, prim_mat, n, cfg, canvas_hw, corrected_lut) -> None:
    """Before/after dE and hue error on held-out point sets (not coincident
    with the build grid), applying the correction through the EXACT production
    interpolation code (ImageRenderer.apply_correction_lut)."""
    sets = {
        "skin (60)": skin_locus_rgb(60, 20260808),
        "greys (13)": gray_ramp_rgb(13),
        "gamut 5^3 (125)": gamut_cube_rgb(5),
    }
    print(
        f"\n{'set':16s} {'':>8s} {'mean dE':>8s} {'median':>8s} {'p95':>8s} {'worst':>8s} {'mean|dh|':>9s} {'p95|dh|':>8s}"
    )
    worst_rows: list[tuple[float, tuple]] = []
    for label, colours in sets.items():
        before_de, after_de, before_dh, after_dh = [], [], [], []
        for rgb in colours:
            want = lab(srgb8_to_xyz_pct(rgb))
            before = predict_lab_on_glass(display, rgb, cfg, prim_mat, n, canvas_hw)
            rgb_arr = np.array(rgb, dtype=np.float32).reshape(1, 1, 3)
            corrected_rgb = ImageRenderer.apply_correction_lut(rgb_arr, corrected_lut)[0, 0]
            after = predict_lab_on_glass(
                display, tuple(float(c) for c in corrected_rgb), cfg, prim_mat, n, canvas_hw
            )

            def hue_deg(c):
                return float(np.degrees(np.arctan2(c[2], c[1])))

            hw, hb, ha = hue_deg(want), hue_deg(before), hue_deg(after)

            def hue_err(h1, h2):
                return abs((h1 - h2 + 180.0) % 360.0 - 180.0)

            before_de.append(delta_e76(before, want))
            after_de.append(delta_e76(after, want))
            before_dh.append(hue_err(hb, hw))
            after_dh.append(hue_err(ha, hw))
            worst_rows.append(
                (
                    after_de[-1],
                    (
                        rgb,
                        want,
                        tuple(float(c) for c in corrected_rgb),
                        after,
                        after_de[-1],
                        after_dh[-1],
                    ),
                )
            )
        for phase, de, dh in (("before", before_de, before_dh), ("after ", after_de, after_dh)):
            de_a, dh_a = np.array(de), np.array(dh)
            print(
                f"{label:16s} {phase:>8s} {de_a.mean():8.2f} {np.median(de_a):8.2f} "
                f"{np.percentile(de_a, 95):8.2f} {de_a.max():8.2f} {dh_a.mean():9.2f} {np.percentile(dh_a, 95):8.2f}"
            )

    worst_rows.sort(key=lambda t: -t[0])
    print("\nworst 10 (after correction):")
    print(
        f"  {'rgb':16s} {'want L*a*b*':>26s} {'corrected_rgb':>20s} {'got L*a*b*':>26s} {'dE':>6s} {'|dh|':>6s}"
    )
    for _de, (rgb, want, corr_rgb, got, de_v, dh_v) in worst_rows[:10]:
        w = f"{want[0]:5.1f},{want[1]:5.1f},{want[2]:5.1f}"
        g = f"{got[0]:5.1f},{got[1]:5.1f},{got[2]:5.1f}"
        c = f"{corr_rgb[0]:5.1f},{corr_rgb[1]:5.1f},{corr_rgb[2]:5.1f}"
        print(f"  {rgb!s:16s} {w:>26s} {c:>20s} {g:>26s} {de_v:6.2f} {dh_v:6.2f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", required=True, choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--data", type=pathlib.Path)
    ap.add_argument("--steps", type=int, default=17)
    ap.add_argument("--canvas", type=int, nargs=2, default=(96, 160))
    ap.add_argument("--knn", type=int, default=8)
    ap.add_argument("--blur-radius", type=int, default=1)
    ap.add_argument("--blend", type=float, default=1.0)
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument(
        "--dry-run", action="store_true", help="build + validate, do not write the asset"
    )
    args = ap.parse_args(argv)

    data = args.data or pathlib.Path(f"docs/screens/{args.model}/measurements/data/campaign.jsonl")
    out = args.out or pathlib.Path(f"python/hokku/screens/{args.model}/correction_lut.npy")
    canvas_hw = tuple(args.canvas)

    display = DISPLAY_REGISTRY[args.model]
    records = load_records(data)
    prim = primaries(records)
    if any(k not in prim for k in INK_NAMES):
        print("ABORT: not every ink has been measured — no model basis.")
        return 1
    prim_mat = np.array([prim[k]["xyz"] for k in INK_NAMES])
    mix = [r for r in records if r.get("source") != "ink"]
    n, fit_err = fit_n(mix, prim, spectral=False)
    print(f"model: Yule-Nielsen n = {n:.2f}, fitted residual {fit_err:.2f} dE, {len(mix)} patches")

    gamut_records = [r for r in records if r.get("phase") == "gamut_dense"]
    print(f"gamut_dense: {len(gamut_records)} measured points")
    print("  classifying by simulated ink-fraction agreement (measured(30/12) vs shipped(95/8))...")
    clean, sensitive = classify_config_sensitivity(display, gamut_records, canvas_hw)
    print(
        f"  {len(clean)} clean (ink-fraction mix agrees measured(30/12) vs shipped(95/8)), "
        f"{len(sensitive)} config-sensitive (excluded from sanity check)"
    )

    print(
        f"\nsanity check: forward simulator (shipped config) vs measurement, on the {len(clean)} clean points"
    )
    sanity_de = sanity_check(display, clean, prim_mat, n, canvas_hw)
    print(
        f"  mean {sanity_de.mean():.2f}  median {np.median(sanity_de):.2f}  "
        f"p95 {np.percentile(sanity_de, 95):.2f}  worst {sanity_de.max():.2f}  (model residual: {fit_err:.2f})"
    )
    if sanity_de.mean() > 1.5 * max(fit_err, 1e-6):
        print(
            "ABORT: sanity-check dE is more than 1.5x the model's own residual — "
            "the forward simulator does not reproduce reality closely enough to trust for inversion."
        )
        return 1

    print(f"\nbuilding forward grid: {args.steps}^3 = {args.steps**3} nodes, canvas {canvas_hw}")
    grid_rgb, grid_lab = build_forward_grid(display, prim_mat, n, args.steps, canvas_hw)

    print(f"inverting (KNN={args.knn}, blur radius={args.blur_radius}, blend={args.blend})")
    corrected = invert_grid(grid_rgb, grid_lab, args.knn)
    corrected = smooth_grid(corrected, args.blur_radius)
    corrected = args.blend * corrected + (1.0 - args.blend) * grid_rgb.astype(np.float32)
    corrected = np.clip(corrected, 0.0, 255.0).astype(np.float32)

    validation_report(display, prim_mat, n, DEFAULT_IMAGE_CONFIG.dither, canvas_hw, corrected)

    if args.dry_run:
        print(f"\n--dry-run: not writing {out}")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    np.save(out, corrected)
    print(f"\nwrote {out}  shape={corrected.shape}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
