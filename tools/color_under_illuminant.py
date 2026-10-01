#!/usr/bin/env python3
"""What the panel looks like under a light that is not D65.

The campaign recorded a full 380-730 nm reflectance curve for every patch, which
is what makes it re-usable here: reflectance is a property of the ink, not of the
lamp, so any illuminant can be applied afterwards without touching hardware. The
XYZ stored alongside it is just one projection of that curve through D65.

The question this answers is whether illuminant-adaptive rendering is worth
building. It is easy to overestimate: a viewer chromatically adapts to the room,
so most of a warm lamp's cast is cancelled by the eye before it reaches
judgement. Rendering a "correction" for it would double-correct and make things
worse — the same shape of mistake as double-correcting dot gain.

So every comparison here is made AFTER adaptation, each illuminant against its
own white. What survives that is the part the eye cannot fix for itself:

  - **gamut change** — an ink that has no energy to reflect under a given lamp
    goes dark and desaturated no matter what the eye does;
  - **metamerism** — inks whose reflectance curves cross can swap relative
    positions under a different SPD, which changes which ink is nearest to a
    requested colour, and no adaptation undoes that.

Usage:
  python tools/color_under_illuminant.py --ambient build/colorcal/ambient.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from color_model import INK_NAMES, load_records

# `colour` is imported lazily, and that is not a style preference. On import it
# MOCKS its missing optional dependencies into sys.modules — scipy is absent
# here — and numba then reads scipy.__version__ off the mock and dies. So a
# module-level `import colour` in a tools module silently breaks the render
# pipeline for anything that imports this file, with a traceback pointing at
# numba and no mention of colour anywhere in it.
_CMFS_CACHE: dict[str, Any] = {}


def _cmfs() -> Any:
    if "cmfs" not in _CMFS_CACHE:
        import colour  # noqa: PLC0415 — see module note: import order matters

        _CMFS_CACHE["cmfs"] = colour.MSDS_CMFS["CIE 1931 2 Degree Standard Observer"]
    return _CMFS_CACHE["cmfs"]


def cmfs_at(wl: np.ndarray) -> np.ndarray:
    """(N,3) x-bar/y-bar/z-bar at the given wavelengths.

    Selected, never interpolated: the CMF table is 1 nm and our data is on 10 nm
    centres, so every wavelength we need is present exactly. Interpolation would
    also drag in scipy, which is not installed here.
    """
    cmfs = _cmfs()
    table = {int(w): v for w, v in zip(cmfs.wavelengths, cmfs.values)}
    missing = [int(w) for w in wl if int(w) not in table]
    if missing:
        raise ValueError(f"CMF table has no entry for {missing} nm")
    return np.vstack([table[int(w)] for w in wl])


def illuminant_at(name: str, wl: np.ndarray) -> np.ndarray:
    """Standard illuminant SPD sampled at the given wavelengths."""
    import colour  # noqa: PLC0415 — see module note: import order matters

    sd = colour.SDS_ILLUMINANTS[name]
    table = {int(w): float(v) for w, v in zip(sd.wavelengths, sd.values)}
    missing = [int(w) for w in wl if int(w) not in table]
    if missing:
        raise ValueError(f"illuminant {name} has no entry for {missing} nm")
    return np.array([table[int(w)] for w in wl])


def reflectance_to_xyz(refl_pct: np.ndarray, spd: np.ndarray, cmf: np.ndarray) -> np.ndarray:
    """Reflectance (%) under an SPD -> XYZ, normalised so a perfect diffuser is Y=100."""
    k = 100.0 / float(np.sum(spd * cmf[:, 1]))
    return k * (spd * refl_pct / 100.0) @ cmf


def white_point(spd: np.ndarray, cmf: np.ndarray) -> np.ndarray:
    """XYZ of a perfect diffuser under this SPD — the eye's adaptation target."""
    k = 100.0 / float(np.sum(spd * cmf[:, 1]))
    return k * spd @ cmf


def lab_rel(xyz: np.ndarray, white: np.ndarray) -> np.ndarray:
    """CIELAB relative to a given white. This is where adaptation is applied.

    Using each illuminant's OWN white is a von Kries adaptation in disguise, and
    it is the honest comparison: it asks what remains after the viewer has
    adjusted, rather than reporting the lamp's cast as if it were a panel defect.
    """
    t = np.asarray(xyz, dtype=float) / np.asarray(white, dtype=float)
    f = np.where(t > 216 / 24389, np.cbrt(np.maximum(t, 0)), (24389 / 27 * t + 16) / 116)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])


def ink_spectra(recs: list[dict]) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Median reflectance curve per ink, over every solid-ink reading in the campaign.

    Raises rather than returning None for a dataset with no spectra. Every caller
    needs the wavelength grid immediately, so handing back None only moves the
    failure somewhere less obvious — and a dataset without spectra cannot answer
    any question this module exists to ask.
    """
    wl = None
    acc: dict[str, list[np.ndarray]] = {k: [] for k in INK_NAMES}
    for r in recs:
        if r.get("source") != "ink" or not r.get("median_spectrum_pct"):
            continue
        idx = r.get("ink_index")
        if idx is None or not (0 <= int(idx) < len(INK_NAMES)):
            continue
        acc[INK_NAMES[int(idx)]].append(np.asarray(r["median_spectrum_pct"], dtype=float))
        if wl is None and r.get("wavelengths_nm"):
            wl = np.asarray(r["wavelengths_nm"], dtype=float)
    if wl is None:
        raise ValueError("dataset carries no ink spectra — cannot re-illuminate")
    return wl, {k: np.median(np.vstack(v), axis=0) for k, v in acc.items() if v}


def xyz_to_srgb8(xyz_pct: np.ndarray) -> np.ndarray:
    """XYZ (Y=100 scale, D65) -> sRGB 0-255, clipped."""
    m = np.array(
        [
            [3.2406, -1.5372, -0.4986],
            [-0.9689, 1.8758, 0.0415],
            [0.0557, -0.2040, 1.0570],
        ]
    )
    lin = np.clip(m @ (np.asarray(xyz_pct, dtype=float) / 100.0), 0.0, 1.0)
    srgb = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(np.rint(srgb * 255.0), 0, 255)


def lab_to_xyz(lab: np.ndarray, white: np.ndarray) -> np.ndarray:
    """Inverse of ``lab_rel``."""
    fy = (lab[0] + 16.0) / 116.0
    fx = fy + lab[1] / 500.0
    fz = fy - lab[2] / 200.0
    f = np.array([fx, fy, fz])
    t = np.where(f**3 > 216 / 24389, f**3, (116 * f - 16) / (24389 / 27))
    return t * np.asarray(white, dtype=float)


def ambient_palette_srgb(
    inks: dict[str, np.ndarray], names: tuple[str, ...], spd: np.ndarray, cmf: np.ndarray
) -> np.ndarray:
    """Palette sRGB describing how the inks APPEAR under ``spd``, to a viewer adapted to it.

    The dither machinery reasons in sRGB and CIELAB, which carry an implicit D65
    viewing condition. Handing it raw XYZ measured under a 2700 K lamp would make
    it believe the panel had turned orange, and it would "correct" a cast the eye
    has already cancelled — the classic double-correction.

    So this computes a CORRESPONDING COLOUR: the ink's appearance under the room
    light (its Lab relative to the room's own white), re-expressed as the sRGB
    that would produce that same appearance to a D65-adapted viewer. That is a
    von Kries transform, and it is the only form in which "the inks moved" can be
    handed to a pipeline that assumes D65 without also handing it the lamp's cast.
    """
    w_src = white_point(spd, cmf)
    w_d65 = np.array([95.047, 100.0, 108.883])
    out = []
    for name in names:
        appearance = lab_rel(reflectance_to_xyz(inks[name], spd, cmf), w_src)
        out.append(xyz_to_srgb8(lab_to_xyz(appearance, w_d65)))
    return np.array(out, dtype=np.float32)


def hull_area(xy: np.ndarray) -> float:
    """Convex-hull area by gift wrapping, so no scipy dependency is needed."""
    pts = sorted(map(tuple, xy))

    def half(ps):
        out: list[tuple[float, float]] = []
        for p in ps:
            while len(out) >= 2:
                (x1, y1), (x2, y2) = out[-2], out[-1]
                if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) > 0:
                    break
                out.pop()
            out.append(p)
        return out

    hull = half(pts)[:-1] + half(pts[::-1])[:-1]
    a = 0.0
    for i in range(len(hull)):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % len(hull)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--data",
        type=Path,
        default=Path("docs/screens/bigme_f7/measurements/data/campaign.jsonl"),
    )
    ap.add_argument("--ambient", type=Path, default=Path("build/colorcal/ambient.jsonl"))
    args = ap.parse_args(argv)

    recs = load_records(args.data)
    wl, inks = ink_spectra(recs)
    cmf = cmfs_at(wl)

    amb_recs = [
        json.loads(x) for x in args.ambient.read_text(encoding="utf-8").splitlines() if x.strip()
    ]
    amb = amb_recs[-1]
    if not amb.get("median_spectrum"):
        print(f"{args.ambient} has no spectrum — cannot re-illuminate")
        return 1
    amb_wl = np.asarray(amb["wavelengths_nm"], dtype=float)
    if not np.array_equal(amb_wl, wl):
        print(f"wavelength mismatch: ambient {amb_wl[0]}-{amb_wl[-1]} vs inks {wl[0]}-{wl[-1]}")
        return 1
    amb_spd = np.asarray(amb["median_spectrum"], dtype=float)

    d65 = illuminant_at("D65", wl)

    # Validation before any conclusion: recomputing the inks under D65 from their
    # own spectra must reproduce the XYZ that spotread reported with -i D65. If it
    # does not, the integration is wrong and nothing below can be trusted.
    print("validation — recomputed D65 vs the campaign's own measured D65 XYZ:")
    meas_xyz = {}
    for r in recs:
        if r.get("source") == "ink" and r.get("ink_index") is not None and "xyz_d65_pct" in r:
            meas_xyz.setdefault(INK_NAMES[int(r["ink_index"])], []).append(r["xyz_d65_pct"])
    w_d65 = white_point(d65, cmf)
    worst = 0.0
    for name in INK_NAMES:
        if name not in inks or name not in meas_xyz:
            continue
        recomputed = reflectance_to_xyz(inks[name], d65, cmf)
        measured = np.median(np.vstack(meas_xyz[name]), axis=0)
        de = float(np.linalg.norm(lab_rel(recomputed, w_d65) - lab_rel(measured, w_d65)))
        worst = max(worst, de)
        print(f"  {name:7s} dE {de:5.2f}")
    print(
        f"  worst {worst:.2f} dE — {'OK' if worst < 2.0 else 'TOO HIGH, do not trust the rest'}\n"
    )

    w_amb = white_point(amb_spd, cmf)
    # Recomputed, never read from the record: early ambient readings were stamped
    # with a CCT from a formula whose denominator had the wrong sign, which turned
    # 2700 K tungsten into 10600 K daylight. Deriving it here means a fixed formula
    # fixes every existing record too, instead of leaving stale numbers on disk.
    from color_ambient import cct_mccamy  # noqa: PLC0415 — keeps this a leaf import

    cct = cct_mccamy(np.asarray(amb["median_raw"], dtype=float))
    print(f"ambient: {amb.get('note') or 'unnamed'}")
    if cct:
        print(f"  {amb['t_local']}   CCT {cct:.0f} K   {amb['median_raw'][1]:.0f} lux\n")

    print("ink appearance, each AFTER adaptation to its own illuminant's white:")
    print(
        f"{'ink':8s} {'L* D65':>7s} {'a*':>7s} {'b*':>7s} | {'L* amb':>7s} {'a*':>7s} {'b*':>7s} | {'dE':>6s} {'dC':>7s}"
    )
    print("-" * 78)
    xy_d65, xy_amb, ab_d65, ab_amb = [], [], [], []
    for name in INK_NAMES:
        if name not in inks:
            continue
        x1 = reflectance_to_xyz(inks[name], d65, cmf)
        x2 = reflectance_to_xyz(inks[name], amb_spd, cmf)
        l1 = lab_rel(x1, w_d65)
        l2 = lab_rel(x2, w_amb)
        c1 = float(np.hypot(l1[1], l1[2]))
        c2 = float(np.hypot(l2[1], l2[2]))
        de = float(np.linalg.norm(l1 - l2))
        print(
            f"{name:8s} {l1[0]:7.2f} {l1[1]:7.2f} {l1[2]:7.2f} | "
            f"{l2[0]:7.2f} {l2[1]:7.2f} {l2[2]:7.2f} | {de:6.2f} {c2 - c1:+7.2f}"
        )
        xy_d65.append([x1[0] / x1.sum(), x1[1] / x1.sum()])
        xy_amb.append([x2[0] / x2.sum(), x2[1] / x2.sum()])
        ab_d65.append([l1[1], l1[2]])
        ab_amb.append([l2[1], l2[2]])

    a1, a2 = hull_area(np.array(xy_d65)), hull_area(np.array(xy_amb))
    b1, b2 = hull_area(np.array(ab_d65)), hull_area(np.array(ab_amb))
    print("-" * 78)
    # Two hulls, because they answer different questions and only one of them is
    # about the panel. The xy hull moves largely because the WHITE POINT moved,
    # which the eye cancels; quoting it alone would overstate the effect by
    # counting the lamp's cast as a gamut loss. The a*b* hull is measured after
    # adaptation, so it is what a viewer actually loses.
    print(f"gamut hull, xy chromaticity : {a1:.5f} -> {a2:.5f}  ({100 * (a2 / a1 - 1):+.1f} %)")
    print("  ^ NOT adaptation-corrected — mostly the white point moving")
    print(f"gamut hull, a*b* ADAPTED    : {b1:.0f} -> {b2:.0f}  ({100 * (b2 / b1 - 1):+.1f} %)")
    print("  ^ this is the one that matters: what survives the eye adapting")
    print("\nNote: dE above is also post-adaptation. The lamp's overall cast is")
    print("already divided out, so these are metamerism and gamut effects only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
