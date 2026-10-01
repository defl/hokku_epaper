#!/usr/bin/env python3
"""Fit a Yule-Nielsen ink-mixing model to measured panel data.

The dither pipeline assumes palette inks mix LINEARLY by area. Measurement says
otherwise: a 50 % black dither lands at ~71 % effective coverage. That gap is
optical — light entering the white paper between dots scatters sideways and
emerges having passed under a dot, so a dot absorbs more than its own area. The
standard correction is Yule-Nielsen:

    R_pred^(1/n)  =  SUM_i  a_i * R_i^(1/n)

with ``a_i`` the area fraction of ink i, ``R_i`` its measured reflectance, and
``n`` a single fitted number describing how far light spreads relative to the
halftone period. n = 1 is linear mixing (Murray-Davies); larger n means more
spreading. Fitting n is the whole model — everything else is measured.

This device is unusually clean for that model: each pixel is exactly ONE ink, so
there are no overprints and the Neugebauer primaries are simply the six inks we
measured directly. No ink-interaction terms to guess.

Every patch record carries its EXACT ink histogram (we generated the raster, so
it is known rather than inferred) alongside the measured colour, which is
precisely the (coverage -> colour) pairing the fit needs.

Spectral data is used when present — the model is properly spectral, and fitting
per-wavelength then integrating is more faithful than fitting tristimulus. Older
records carry XYZ only; those fall back to applying the same relation per
channel, which is the usual tristimulus approximation.

Usage:
  python tools/color_model.py --data build/colorcal/campaign.jsonl
  python tools/color_model.py --data <archive>.jsonl --per-config
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import numpy as np

INK_NAMES = ("black", "white", "yellow", "red", "blue", "green")
D65 = np.array([0.95047, 1.0, 1.08883])
_M_XYZ2RGB = np.array(
    [
        [3.2406, -1.5372, -0.4986],
        [-0.9689, 1.8758, 0.0415],
        [0.0557, -0.2040, 1.0570],
    ]
)


def lab(xyz_pct) -> np.ndarray:
    """XYZ in percent (Y=100 for a perfect diffuser) -> CIELAB under D65."""
    t = np.asarray(xyz_pct, dtype=float) / 100.0 / D65
    f = np.where(t > 216 / 24389, np.cbrt(t), (24389 / 27 * t + 16) / 116)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])


def delta_e76(a, b) -> float:
    return float(np.linalg.norm(np.asarray(a) - np.asarray(b)))


def xyz_to_srgb8(xyz_pct) -> tuple[int, int, int]:
    lin = _M_XYZ2RGB @ (np.asarray(xyz_pct, dtype=float) / 100.0)
    lin = np.clip(lin, 0.0, 1.0)
    s = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
    return tuple(int(v) for v in np.rint(s * 255))  # type: ignore[return-value]


# ── loading ───────────────────────────────────────────────────────────────────


def load_records(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn final line from a hard kill
        if "xyz_d65_pct" in rec and rec.get("ink_fraction"):
            out.append(rec)
    return out


def primaries(records: list[dict]) -> dict[str, dict]:
    """Measured colour of each solid ink, averaged over every time it was read.

    Solid inks arrive from three places — the `inks` phase, the campaign's
    open/close brackets, and the per-session anchors — and all of them are the
    same stimulus, so all of them are evidence.
    """
    acc: dict[str, list[dict]] = collections.defaultdict(list)
    for r in records:
        if r.get("source") != "ink":
            continue
        frac = r["ink_fraction"]
        solid = [k for k, v in frac.items() if v > 0.999]
        if len(solid) != 1:
            continue
        acc[solid[0]].append(r)
    out = {}
    for name, rs in acc.items():
        xyz = np.array([r["xyz_d65_pct"] for r in rs])
        entry = {
            "xyz": xyz.mean(axis=0),
            "n": len(rs),
            "spread_de": float(
                np.mean([delta_e76(lab(x), lab(xyz.mean(axis=0))) for x in xyz])
                if len(rs) > 1
                else 0.0
            ),
        }
        specs = [r["median_spectrum_pct"] for r in rs if r.get("median_spectrum_pct")]
        if specs:
            entry["spectrum"] = np.mean(np.array(specs), axis=0)
        out[name] = entry
    return out


# ── the model ─────────────────────────────────────────────────────────────────


def yn_mix(fractions: np.ndarray, prim: np.ndarray, n: float) -> np.ndarray:
    """Yule-Nielsen mix: (sum a_i * R_i^(1/n))^n, elementwise over channels/bands.

    Reflectances are clipped away from zero before the fractional power: black
    ink reads ~1 % and a zero would make the root explode, which would let one
    dark primary dominate the fit for purely numerical reasons.
    """
    r = np.maximum(prim, 1e-6) ** (1.0 / n)
    return (fractions @ r) ** n


def fit_n(records: list[dict], prim: dict[str, dict], spectral: bool) -> tuple[float, float]:
    """Find the single n minimising mean prediction error, and report that error.

    Scanned rather than gradient-solved: n is one bounded scalar, the objective is
    cheap, and a scan cannot land in a local minimum or wander outside the
    physically sensible range the way an unconstrained solver can.
    """
    key = "spectrum" if spectral else "xyz"
    prim_mat = np.array([prim[k][key] for k in INK_NAMES])
    rows, meas = [], []
    for r in records:
        rows.append([r["ink_fraction"].get(k, 0.0) for k in INK_NAMES])
        meas.append(r["median_spectrum_pct"] if spectral else r["xyz_d65_pct"])
    A, Y = np.array(rows), np.array(meas)

    best, best_err = 1.0, float("inf")
    for n in np.arange(1.0, 12.01, 0.01):
        pred = yn_mix(A, prim_mat, float(n))
        if spectral:
            err = float(np.sqrt(np.mean((pred - Y) ** 2)))
        else:
            err = float(np.mean([delta_e76(lab(p), lab(m)) for p, m in zip(pred, Y, strict=True)]))
        if err < best_err:
            best, best_err = float(n), err
    return best, best_err


def residuals(records, prim, n, spectral) -> np.ndarray:
    """Per-patch dE76 between the model's prediction and the measurement."""
    key = "spectrum" if spectral else "xyz"
    prim_mat = np.array([prim[k][key] for k in INK_NAMES])
    out = []
    for r in records:
        a = np.array([r["ink_fraction"].get(k, 0.0) for k in INK_NAMES])
        pred = yn_mix(a, prim_mat, n)
        if spectral:
            # Compare in XYZ so the number means something perceptual; the
            # per-band fit is what produced the prediction.
            continue
        out.append(delta_e76(lab(pred), lab(r["xyz_d65_pct"])))
    return np.array(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--per-config", action="store_true", help="fit n separately per dither config")
    ap.add_argument("--spectral", action="store_true", help="fit per-wavelength if spectra exist")
    args = ap.parse_args(argv)

    records = load_records(args.data)
    print(f"{len(records)} usable records in {args.data.name}")
    prim = primaries(records)
    missing = [k for k in INK_NAMES if k not in prim]
    if missing:
        print(f"ABORT: no measurement for {missing} — the model has no basis without every ink.")
        return 1

    print(f"\n{'ink':8s} {'n':>3s} {'L*':>7s} {'a*':>7s} {'b*':>7s} {'spread dE':>10s}")
    for name in INK_NAMES:
        p = prim[name]
        lb = lab(p["xyz"])
        print(
            f"{name:8s} {p['n']:3d} {lb[0]:7.2f} {lb[1]:7.2f} {lb[2]:7.2f} {p['spread_de']:10.3f}"
        )

    have_spec = all("spectrum" in prim[k] for k in INK_NAMES)
    spectral = bool(args.spectral and have_spec)
    print(
        f"\nspectra available for every primary: {have_spec}"
        + ("  (fitting spectrally)" if spectral else "  (fitting on XYZ)")
    )

    mix = [r for r in records if r.get("source") != "ink"]
    if not mix:
        print("\nNo mixed patches yet — nothing to fit. Measure some dithered fields first.")
        return 0

    n, _fit_err = fit_n(mix, prim, spectral)
    res = residuals(mix, prim, n, spectral=False)
    lin = residuals(mix, prim, 1.0, spectral=False)
    print(f"\nfitted over {len(mix)} mixed patches")
    print(f"  Yule-Nielsen n = {n:.2f}   (n=1 would be the linear mixing the pipeline assumes)")
    print(
        f"  mean dE76 {res.mean():6.2f}   median {np.median(res):6.2f}   p95 {np.percentile(res, 95):6.2f}"
    )
    print(
        f"  linear-mixing baseline: mean dE76 {lin.mean():6.2f}  -> model removes "
        f"{100 * (1 - res.mean() / max(lin.mean(), 1e-9)):.0f}% of the error"
    )

    if args.per_config:
        print(f"\n{'config':28s} {'patches':>7s} {'n':>6s} {'mean dE':>8s}")
        by = collections.defaultdict(list)
        for r in mix:
            by[r.get("config_name") or f"{r.get('source')}"].append(r)
        for cfg, rs in sorted(by.items()):
            if len(rs) < 4:
                continue
            cn, _ = fit_n(rs, prim, spectral)
            cr = residuals(rs, prim, cn, spectral=False)
            print(f"{cfg:28s} {len(rs):7d} {cn:6.2f} {cr.mean():8.2f}")

    print("\nCorrected palette anchors (measured, for palette_measured_rgb):")
    for name in INK_NAMES:
        print(f"  {name:8s} {xyz_to_srgb8(prim[name]['xyz'])}")
    print(
        "\nNOTE: these are ABSOLUTE reflectance. Paper white is far below sRGB white,\n"
        "so dropping them in unchanged darkens everything — they need white-normalising\n"
        "or a gamut-mapping intent chosen first. That is a decision, not a measurement."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
