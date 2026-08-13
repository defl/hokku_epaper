#!/usr/bin/env python3
"""Derive tone-correction curves for the panel, and say which one is defensible.

findings.md argues that error diffusion is a closed loop which already
compensates for the panel's dot gain, so an inverse-transfer LUT would
double-correct. The dataset can settle that, because it contains both kinds of
patch:

  ``bayer``     coverage set directly. OPEN loop — nothing chose it, so this
                measures the panel's dot gain in isolation.
  ``pipeline``  a flat sRGB was REQUESTED and ``dither()`` chose the coverage.
                CLOSED loop — this is what the renderer actually delivers.

Measured on the 191 neutral-request pipeline patches, the loop does NOT correct
the dot gain: +3.41 L* mean and +5.72 L* peak survive into real rendered output,
against a 0.35 dE noise floor. The loop is closed over the renderer's *model* of
the panel (``palette_measured_rgb``, mixed linearly in sRGB), not over the panel
itself — no measurement feeds back from the glass, so it cannot correct a mixing
error it has no knowledge of.

The warning is still worth heeding, for a different reason. The renderer's net
error is two errors partly cancelling: mixing in gamma-encoded sRGB lightens
midtones, while optical dot gain darkens them. So inverting the open-loop arch
over-corrects by roughly the difference. Two curves are therefore emitted:

  ``naive``   inverse of the open-loop bayer arch. The one findings.md warns
              about. Included so the warning is tested rather than assumed.
  ``closed``  built from the pipeline patches: requested grey -> measured L*,
              inverted. End-to-end by construction, so it cannot double-correct.

Both are 1-D and neutral-only. That is a deliberate first step, not the finished
article: the general case is a 3-D LUT over the dense gamut phase. Get a human
verdict on whether tone correction is wanted at all before building that.

Usage:
  python tools/color_tone_curve.py --data docs/screens/bigme_f7/measurements/data/campaign.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from color_model import fit_n, lab, load_records, primaries

# Requested greys land on the panel's own L* range, not sRGB's. A curve that
# tried to hit L* 100 would ask for a white the ink cannot make and simply clip.
_GREYS = np.arange(256)


def _srgb_grey_to_lab_l(g: float) -> float:
    """L* of a neutral sRGB code value, under D65."""
    c = g / 255.0
    lin = c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return float(lab(np.array([95.047, 100.0, 108.883]) * lin)[0])


def closed_loop_samples(recs: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """(requested grey, measured L*) from every neutral-request pipeline patch.

    Averaged per requested level across configs. The configs disagree on colour
    strategy but all reduce to black/white on a true neutral, so pooling them is
    what gives each level enough reads to sit under the noise floor.
    """
    by_grey: dict[int, list[float]] = {}
    for r in recs:
        if r.get("source") != "pipeline" or "xyz_d65_pct" not in r:
            continue
        rgb = r.get("rgb")
        if not rgb or len(set(rgb)) != 1:
            continue
        by_grey.setdefault(int(rgb[0]), []).append(float(lab(r["xyz_d65_pct"])[0]))
    greys = np.array(sorted(by_grey), dtype=float)
    meas = np.array([float(np.mean(by_grey[int(g)])) for g in greys])
    return greys, meas


def open_loop_samples(recs: list[dict], prim: dict, n: float) -> tuple[np.ndarray, np.ndarray]:
    """(nominal black coverage, measured L*) from the bayer ramp.

    This is the arch itself — coverage imposed, nothing choosing it — and the
    curve findings.md warns against inverting.
    """
    by_k: dict[int, list[float]] = {}
    for r in recs:
        if r.get("source") != "bayer" or "xyz_d65_pct" not in r:
            continue
        k = r.get("bayer_k")
        if k is None:
            continue
        by_k.setdefault(int(k), []).append(float(lab(r["xyz_d65_pct"])[0]))
    ks = np.array(sorted(by_k), dtype=float)
    cov = ks / 64.0
    meas = np.array([float(np.mean(by_k[int(k)])) for k in ks])
    return cov, meas


def _invert_to_lut(req: np.ndarray, achieved_l: np.ndarray) -> np.ndarray:
    """Build a 256-entry grey->grey LUT that corrects ONLY the midtone bow.

    ``req`` is what was asked for, ``achieved_l`` what the panel produced.

    The endpoints are deliberately left alone. An earlier version mapped input
    0..255 across the panel's achievable L* range, which linearised the tone
    response but also performed range compression — and range compression is
    DRC's job, freshly fixed there via ``drc_anchor_l``. A curve doing both would
    make any A/B a test of range mapping rather than of dot gain, which is the
    confound this whole line of work keeps tripping over.

    So: find the request span over which the panel is still responding (outside
    it the ink has saturated and no curve can help), hold both ends fixed, and
    correct only the bow between them. Input 0 stays 0 and input 255 stays 255.
    """
    order = np.argsort(req)
    r_sorted = req[order].astype(float)
    l_sorted = achieved_l[order].astype(float)

    # Enforce monotonic lightness before inverting. Neighbouring levels disagree
    # by up to 0.5 L* here, which is noise (floor 0.35 dE) but is enough to make
    # a naive inverse fold back on itself and produce a non-monotonic curve —
    # visible as banding, not as the subtle error it actually is.
    l_mono = np.maximum.accumulate(l_sorted)

    l_min, l_max = float(l_mono[0]), float(l_mono[-1])
    # Usable span: last request still at the floor -> first request at the ceiling.
    tol = 0.35  # the measured noise floor; anything inside it is not a response
    lo_idx = int(np.max(np.flatnonzero(l_mono <= l_min + tol)))
    hi_idx = int(np.min(np.flatnonzero(l_mono >= l_max - tol)))
    g_lo, g_hi = float(r_sorted[lo_idx]), float(r_sorted[hi_idx])

    grid = np.arange(256, dtype=float)
    # Target: equal input steps produce equal L* steps, between the two fixed
    # endpoints only. Linear in L* because that is the perceptually uniform axis.
    target_l = np.interp(grid, [g_lo, g_hi], [l_min, l_max])
    # Invert the measured response to find the request that lands on that target.
    corrected = np.interp(target_l, l_mono, r_sorted)
    # Outside the responding span the panel has saturated; pass through so the
    # curve never fights DRC over territory DRC owns.
    corrected = np.where(grid <= g_lo, grid, corrected)
    corrected = np.where(grid >= g_hi, grid, corrected)
    return np.clip(np.rint(corrected), 0, 255).astype(np.uint8)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--data",
        type=Path,
        default=Path("docs/screens/bigme_f7/measurements/data/campaign.jsonl"),
    )
    ap.add_argument("--out", type=Path, default=Path("build/colorcal/tone_curves.json"))
    args = ap.parse_args(argv)

    recs = load_records(args.data)
    prim = primaries(recs)
    n, resid = fit_n(recs, prim, spectral=False)

    print(f"fitted n = {n:.3f} (residual {resid:.2f} dE)\n")

    # --- closed loop: what the renderer really delivers -------------------
    req, meas = closed_loop_samples(recs)
    closed_lut = _invert_to_lut(req, meas)
    print(f"closed-loop samples: {len(req)} requested levels")
    print("  requested -> measured L*")
    for g, m in zip(req, meas):
        print(f"    {int(g):3d} -> {m:6.2f}")

    # --- open loop: the panel's dot-gain arch -----------------------------
    cov, cov_l = open_loop_samples(recs, prim, n)
    # Put the arch on the same grey axis as the closed-loop curve: coverage c of
    # black ink stands in for the request that a linear-palette mix would have
    # produced it from, so the two curves are inverted by identical machinery and
    # differ only in WHICH measurement they invert. That is the whole experiment.
    naive_req = np.interp(1.0 - cov, [0.0, 1.0], [0.0, 255.0])
    naive_lut = _invert_to_lut(naive_req, cov_l)
    print(f"\nopen-loop (bayer) samples: {len(cov)} coverage levels")

    # --- how far apart are they? ------------------------------------------
    diff = np.abs(closed_lut.astype(int) - naive_lut.astype(int))
    print(f"\nnaive vs closed curve: mean {diff.mean():.1f} code values, max {diff.max()}")
    print("  (if this were ~0 the two arms would be the same experiment)")

    identity = np.arange(256)
    print(
        f"closed curve vs identity: mean {np.abs(closed_lut - identity).mean():.1f}, "
        f"max {np.abs(closed_lut.astype(int) - identity).max()}"
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "model": "bigme_f7",
                "source_data": str(args.data),
                "fitted_n": float(n),
                "model_residual_de": float(resid),
                "closed_loop": {
                    "lut": closed_lut.tolist(),
                    "requested": req.tolist(),
                    "measured_l": meas.tolist(),
                    "note": "derived from pipeline patches; end-to-end, cannot double-correct",
                },
                "naive_open_loop": {
                    "lut": naive_lut.tolist(),
                    "coverage": cov.tolist(),
                    "measured_l": cov_l.tolist(),
                    "note": "inverse of the bayer dot-gain arch; findings.md predicts over-correction",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
