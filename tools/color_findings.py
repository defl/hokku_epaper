#!/usr/bin/env python3
"""Regenerate every headline number in `measurements/findings.md` from the data.

The findings document was written incrementally while the campaign was still
running, so its numbers came from whatever subset existed on the day each section
was written. That is how it ended up asserting a gamut both 55 % of sRGB and
18.5 % of the visible locus, which cannot both be true — the two are related by a
fixed ratio. Numbers that are re-derived on demand cannot drift like that.

Everything printed here comes from the committed dataset. Run it, and paste or
check the output against the document; no figure in findings.md should exist that
this script cannot produce.
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from color_model import (
    INK_NAMES,
    delta_e76,
    fit_n,
    lab,
    load_records,
    primaries,
    yn_mix,
)

# CIE 1931 2° spectral locus area in xy, by the shoelace formula over the
# standard observer's chromaticity outline. A constant rather than a recomputed
# value because it is a property of the observer, not of anything measured here.
VISIBLE_LOCUS_AREA = 0.1952

# sRGB primaries (IEC 61966-2-1).
SRGB_XY = np.array([[0.64, 0.33], [0.30, 0.60], [0.15, 0.06]])


def xy_of(xyz: np.ndarray) -> np.ndarray:
    """CIE xy chromaticity from XYZ."""
    s = float(np.sum(xyz))
    return np.array([xyz[0] / s, xyz[1] / s])


def polygon_area(pts: np.ndarray) -> float:
    """Shoelace area of the convex hull of `pts`, ordered by angle about centroid."""
    c = pts.mean(axis=0)
    order = np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))
    p = pts[order]
    return float(
        0.5 * abs(np.dot(p[:, 0], np.roll(p[:, 1], -1)) - np.dot(p[:, 1], np.roll(p[:, 0], -1)))
    )


def section_panel(recs: list[dict], prim: dict) -> None:
    print("\n## The panel\n")
    print(f"  {'ink':7s} {'reads':>5} {'L*':>7} {'a*':>7} {'b*':>7} {'Y%':>7} {'spread dE':>10}")
    for name in INK_NAMES:
        if name not in prim:
            continue
        p = prim[name]
        lb = lab(p["xyz"])
        print(
            f"  {name:7s} {p['n']:5d} {lb[0]:7.2f} {lb[1]:7.2f} {lb[2]:7.2f} "
            f"{p['xyz'][1]:7.2f} {p['spread_de']:10.3f}"
        )

    yw, yb = prim["white"]["xyz"][1], prim["black"]["xyz"][1]
    print(f"\n  contrast  {yw / yb:.1f} : 1   (white Y {yw:.2f} %, black Y {yb:.2f} %)")

    # Gamut is the hull of the six inks in xy. Reported against both references so
    # the two figures are forced to be consistent with each other.
    pts = np.array([xy_of(prim[n]["xyz"]) for n in INK_NAMES if n in prim])
    area = polygon_area(pts)
    srgb = polygon_area(SRGB_XY)
    print(
        f"  gamut     xy area {area:.5f}  =  {100 * area / srgb:.0f} % of sRGB"
        f"  =  {100 * area / VISIBLE_LOCUS_AREA:.0f} % of the visible locus"
    )

    blk = lab(prim["black"]["xyz"])
    print(
        f"  black ink a* {blk[1]:+.2f}  b* {blk[2]:+.2f}   (chroma {np.hypot(blk[1], blk[2]):.1f})"
    )

    # Chromaticity flatters a reflective panel: the saturated inks are also the
    # dark ones, so the xy outline is reached only at low luminance.
    print("\n  where each ink's chromaticity actually sits in luminance:")
    for name in ("red", "blue", "green", "yellow"):
        if name in prim:
            print(f"    {name:7s} Y {prim[name]['xyz'][1]:5.2f} %")


def section_stability(recs: list[dict]) -> None:
    """What the session brackets say about repeatability — the noise floor."""
    print("\n## Stability\n")
    by = collections.defaultdict(list)
    for r in recs:
        if r.get("phase") != "session_anchor" or "xyz_d65_pct" not in r:
            continue
        frac = r.get("ink_fraction") or {}
        solid = [k for k, v in frac.items() if v > 0.999]
        if len(solid) == 1:
            by[solid[0]].append((r.get("session"), r.get("anchor_tag"), np.array(r["xyz_d65_pct"])))

    print(f"  {'ink':7s} {'brackets':>8} {'mean dE to ink mean':>20} {'worst':>8}")
    alld = []
    for name in INK_NAMES:
        rs = by.get(name)
        if not rs:
            continue
        xyz = np.array([x for _, _, x in rs])
        mean = xyz.mean(axis=0)
        des = [delta_e76(lab(x), lab(mean)) for x in xyz]
        alld += des
        print(f"  {name:7s} {len(rs):8d} {np.mean(des):20.3f} {max(des):8.3f}")
    if alld:
        print(
            f"\n  overall anchor spread: mean {np.mean(alld):.3f} dE, p95 {np.percentile(alld, 95):.3f}"
        )
        print("  This is the noise floor. Any conclusion resting on a smaller")
        print("  difference is inside it.")

    # Open-vs-close within a session isolates drift *during* a session from
    # session-to-session variation.
    drift = []
    per = collections.defaultdict(dict)
    for name, rs in by.items():
        for sess, tag, xyz in rs:
            if tag:
                per[(name, sess)][tag] = xyz
    for v in per.values():
        if "open" in v and "close" in v:
            drift.append(delta_e76(lab(v["open"]), lab(v["close"])))
    if drift:
        print(
            f"  open->close within a session: mean {np.mean(drift):.3f} dE over "
            f"{len(drift)} pairs, worst {max(drift):.3f}"
        )


def section_tone(recs: list[dict], prim: dict) -> None:
    """Dot gain: what a requested coverage actually prints as."""
    print("\n## Tone response (dot gain)\n")
    yw, yb = prim["white"]["xyz"][1], prim["black"]["xyz"][1]
    rows = []
    for r in recs:
        if r.get("phase") not in ("tone_fine",) or "xyz_d65_pct" not in r:
            continue
        frac = r.get("ink_fraction") or {}
        k = frac.get("black")
        if k is None or not (0.0 < k < 1.0):
            continue
        if abs(k + frac.get("white", 0.0) - 1.0) > 1e-6:
            continue  # not a pure black/white mix
        y = r["xyz_d65_pct"][1]
        rows.append((k, y, (yw - y) / (yw - yb)))
    if not rows:
        print("  (no black/white tone patches)")
        return
    agg = collections.defaultdict(list)
    for k, y, eff in rows:
        agg[round(k, 4)].append((y, eff))
    print(
        f"  {'nominal':>8} {'meas Y%':>8} {'linear Y%':>10} {'effective':>10} {'dot gain':>9} {'dL*':>7}"
    )
    peak = (0.0, 0.0)
    for k in sorted(agg):
        ys = [y for y, _ in agg[k]]
        effs = [e for _, e in agg[k]]
        y, eff = float(np.mean(ys)), float(np.mean(effs))
        lin = yw + k * (yb - yw)
        gain = eff - k
        dl = abs(lab(np.array([0, y, 0]))[0] - lab(np.array([0, lin, 0]))[0])
        print(f"  {k:8.3f} {y:8.2f} {lin:10.2f} {eff:10.3f} {gain:+9.3f} {dl:7.1f}")
        if gain > peak[1]:
            peak = (k, gain)
    print(
        f"\n  peak dot gain {peak[1]:+.3f} at nominal {peak[0]:.3f} "
        f"(prints like {peak[0] + peak[1]:.3f} coverage)"
    )


def section_model(recs: list[dict], prim: dict) -> None:
    print("\n## The Yule-Nielsen model\n")
    n, err = fit_n(recs, prim, spectral=False)
    pm = np.array([prim[k]["xyz"] for k in INK_NAMES])
    mixed = [
        r
        for r in recs
        if "xyz_d65_pct" in r and r.get("ink_fraction") and max(r["ink_fraction"].values()) <= 0.999
    ]
    lin = []
    for r in mixed:
        f = np.array([r["ink_fraction"].get(k, 0.0) for k in INK_NAMES])
        lin.append(delta_e76(lab(yn_mix(f, pm, 1.0)), lab(np.array(r["xyz_d65_pct"]))))
    print(f"  n = {n:.2f}   mean dE76 {err:.2f}   over {len(mixed)} mixed patches")
    if lin:
        print(
            f"  linear-mixing baseline mean dE76 {np.mean(lin):.2f}"
            f"  -> the model removes {100 * (1 - err / np.mean(lin)):.0f} % of the error"
        )

    print("\n  per config (the ordered-vs-diffused split):")
    by = collections.defaultdict(list)
    for r in mixed:
        if r.get("config_name"):
            by[r["config_name"]].append(r)
    out = []
    for cfg, rs in by.items():
        if len(rs) < 12:
            continue
        cn, ce = fit_n(rs, prim, spectral=False)
        out.append((cn, cfg, len(rs), ce))
    for cn, cfg, cnt, ce in sorted(out, reverse=True):
        print(f"    {cfg:34s} n {cn:5.2f}   dE {ce:5.2f}   ({cnt} patches)")


def section_greys(recs: list[dict], prim: dict) -> None:
    """What a requested neutral grey actually becomes, per LUT."""
    print("\n## Grey neutrality by LUT\n")
    by = collections.defaultdict(list)
    for r in recs:
        if r.get("phase") not in ("plain_lut_grey", "lut_gain", "algo_gain"):
            continue
        if "xyz_d65_pct" not in r or not r.get("config_name"):
            continue
        rgb = r.get("rgb")
        if not rgb or not (max(rgb) - min(rgb) == 0):
            continue  # only true neutrals
        lb = lab(np.array(r["xyz_d65_pct"]))
        by[r["config_name"]].append((float(np.hypot(lb[1], lb[2])), lb[1], lb[2]))
    if not by:
        print("  (no neutral patches)")
        return
    print(f"  {'config':34s} {'n':>3} {'cast (chroma)':>14} {'a*':>7} {'b*':>7}")
    for cfg, v in sorted(by.items(), key=lambda kv: np.mean([c for c, _, _ in kv[1]])):
        cast = np.mean([c for c, _, _ in v])
        a = np.mean([x for _, x, _ in v])
        b = np.mean([x for _, _, x in v])
        print(f"  {cfg:34s} {len(v):3d} {cast:14.2f} {a:+7.2f} {b:+7.2f}")


def section_dedup(recs: list[dict]) -> None:
    """Does an inherited reading actually match a fresh one of the same raster?"""
    print("\n## Dedup validation\n")
    by = collections.defaultdict(list)
    for r in recs:
        if "xyz_d65_pct" in r and r.get("raster_sha1") and not r.get("duplicate_of"):
            if not r.get("is_control"):
                by[r["raster_sha1"]].append(np.array(r["xyz_d65_pct"]))
    des = []
    for xs in by.values():
        for i in range(len(xs)):
            for j in range(i + 1, len(xs)):
                des.append(delta_e76(lab(xs[i]), lab(xs[j])))
    if not des:
        print("  (no repeated rasters measured independently)")
        return
    print(f"  {len(des)} independent pairs of identical rasters")
    print(
        f"  agreement: mean {np.mean(des):.3f} dE, median {np.median(des):.3f}, "
        f"p95 {np.percentile(des, 95):.3f}"
    )
    print("  Compare against the anchor noise floor above: if these match, dedup")
    print("  is inheriting readings no worse than re-measuring would have been.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--data",
        type=pathlib.Path,
        default=pathlib.Path("docs/screens/bigme_f7/measurements/data/campaign.jsonl"),
    )
    args = ap.parse_args(argv)

    recs = load_records(args.data)
    print(f"# Findings, regenerated from {args.data}")
    print(f"\n{len(recs)} usable readings, {len({r.get('session') for r in recs})} sessions")

    prim = primaries(recs)
    section_panel(recs, prim)
    section_stability(recs)
    section_tone(recs, prim)
    section_model(recs, prim)
    section_greys(recs, prim)
    section_dedup(recs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
