#!/usr/bin/env python3
"""Turn colorimeter readings of a calibration target into panel palette data.

Companion to ``color_target.py``. Walks the patch manifest in measurement
order, collects one tristimulus reading per patch, normalises them against a
white reference, and prints:

  1. the ``palette_measured_rgb`` block to paste into the model's
     ``display.py``, alongside how far the current in-repo values were off;
  2. the panel's **tone-response curve** — measured lightness at each known
     black-ink area coverage, against what the render pipeline assumes.

Instrument independence
-----------------------
This script does not talk to the meter. It prompts, you paste. That keeps it
working with any instrument and any vendor software, and it sidesteps the
driver swap that ArgyllCMS needs on Windows being a prerequisite for getting
*any* numbers out. Paste either three bare numbers or a whole ArgyllCMS
``spotread`` result line — both parse.

Emissive vs reflective
----------------------
A colorimeter (Calibrite ColorChecker Display Plus, X-Rite i1Display Pro, …)
has **no lamp**: it can only report the light arriving at its aperture. On a
reflective medium like e-paper you therefore supply the light yourself and
read in *emissive* mode, and the raw numbers carry your lamp's brightness and
colour cast. Dividing every patch by a reading of a **white reference of known
Lab**, taken under identical geometry, removes both and lands the result on
the D65 axis the rest of the codebase uses.

Without a white reference the script can still normalise against the panel's
own white ink (``--relative-to-panel-white``), but that *defines* panel white
as neutral — the white ink's real slight blue-grey cast is thrown away, and
so is the absolute lightness the DRC stage depends on. It is a fallback, not
the intended path.

Usage:
    # Interactive, with a known white reference (recommended)
    python tools/color_read.py build/colorcal/colorcal_bigme_f7.json \\
        --white-ref-lab 96.5,-0.4,1.2

    # Re-analyse a finished session without re-metering
    python tools/color_read.py build/colorcal/colorcal_bigme_f7.json \\
        --from-file build/colorcal/readings_bigme_f7.json

    # No reference standard available (degraded — see above)
    python tools/color_read.py build/colorcal/colorcal_bigme_f7.json \\
        --relative-to-panel-white
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import rgb_to_lab, xyz_to_lab

# The codebase's own Lab→sRGB inverse. Imported rather than re-derived so a
# measured anchor round-trips through exactly the transform the dither LUTs
# will later use to read it back.
from hokku.webserver.image_renderer import ImageRenderer

_lab_to_rgb = ImageRenderer._lab_to_rgb

# D65, Y = 1.0 — matches dither_streaming.xyz_to_lab.
D65_XYZ = np.array([0.95047, 1.00000, 1.08883])

_NUM = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"


def parse_reading(text: str) -> np.ndarray | None:
    """Parse a pasted measurement into a 3-vector, or None if unparseable.

    Accepts bare ``X Y Z`` / ``X,Y,Z`` and ArgyllCMS ``spotread`` result lines
    such as ``Result is XYZ: 24.31 25.55 21.37, D50 Lab: ...`` — in which case
    the XYZ triple is taken and any Lab on the same line ignored (spotread
    reports Lab against D50; we do our own normalisation).
    """
    text = text.strip()
    if not text:
        return None
    m = re.search(rf"XYZ:?\s*({_NUM})[,\s]+({_NUM})[,\s]+({_NUM})", text, re.IGNORECASE)
    if m:
        return np.array([float(m.group(i)) for i in (1, 2, 3)])
    nums = re.findall(_NUM, text)
    if len(nums) >= 3:
        return np.array([float(n) for n in nums[:3]])
    return None


def normalise(raw: np.ndarray, white_raw: np.ndarray, white_lab: np.ndarray | None) -> np.ndarray:
    """Raw instrument XYZ → D65-referenced XYZ (Y = 1.0 for a perfect white).

    Componentwise division by the white reading is a von Kries adaptation: it
    cancels the lamp's intensity *and* its colour cast in one step. Scaling
    back up by the reference's own known XYZ is what preserves the difference
    between "the reference is perfectly white" and "the reference is a real
    object that is slightly off-white" — which is exactly the information the
    panel's white-ink tint lives in.
    """
    ratio = raw / white_raw
    if white_lab is None:
        # Fallback: the reference *is* panel white, declared neutral.
        return ratio * D65_XYZ
    return ratio * lab_to_xyz(white_lab)


def lab_to_xyz(lab: np.ndarray) -> np.ndarray:
    """Inverse of dither_streaming.xyz_to_lab (D65, Y = 1.0)."""
    L, a, b = (float(v) for v in lab)
    fy = (L + 16.0) / 116.0
    fx = fy + a / 500.0
    fz = fy - b / 200.0
    eps, kappa = 0.008856, 903.3
    xr = fx**3 if fx**3 > eps else (116.0 * fx - 16.0) / kappa
    yr = ((L + 16.0) / 116.0) ** 3 if L > kappa * eps else L / kappa
    zr = fz**3 if fz**3 > eps else (116.0 * fz - 16.0) / kappa
    return np.array([xr, yr, zr]) * D65_XYZ


def delta_e76(lab_a: np.ndarray, lab_b: np.ndarray) -> float:
    return float(np.sqrt(np.sum((np.asarray(lab_a) - np.asarray(lab_b)) ** 2)))


def prompt_readings(patches: list[dict], white_needed: bool) -> dict[str, list[float]]:
    """Walk the patch list, collecting one reading each. Enter skips a patch."""
    readings: dict[str, list[float]] = {}
    print()
    print("Paste a reading per patch (three numbers, or a whole spotread line).")
    print("Enter alone skips a patch; Ctrl-C aborts and keeps what you have.")
    print()

    if white_needed:
        while True:
            raw = parse_reading(input("WHITE REFERENCE (the standard, not the panel): "))
            if raw is not None:
                readings["white_ref"] = raw.tolist()
                break
            print("  ! could not parse — expected three numbers")

    try:
        for p in patches:
            label = f"[{p['order']:>2}/{len(patches)}] {p['name']:<16}"
            where = f"row {p['row']} col {p['col']}, centre {tuple(p['center_px'])}"
            while True:
                text = input(f"{label} ({where}): ")
                if not text.strip():
                    print("  · skipped")
                    break
                raw = parse_reading(text)
                if raw is None:
                    print("  ! could not parse — expected three numbers")
                    continue
                readings[str(p["order"])] = raw.tolist()
                break
    except KeyboardInterrupt:
        print("\n  (aborted — analysing the readings collected so far)")
    return readings


def analyse(manifest: dict, readings: dict, white_lab: np.ndarray | None) -> dict:
    """Normalise every reading and derive palette anchors + tone response."""
    patches = manifest["patches"]
    by_order = {str(p["order"]): p for p in patches}

    if "white_ref" in readings:
        white_raw = np.array(readings["white_ref"])
    else:
        # Normalising against the panel's own white ink.
        white_orders = [
            str(p["order"])
            for p in patches
            if p["kind"] == "ink" and p["ink_index"] == 1 and str(p["order"]) in readings
        ]
        if not white_orders:
            raise SystemExit("No white reading available — cannot normalise.")
        white_raw = np.mean([readings[o] for o in white_orders], axis=0)

    measured: dict[str, dict] = {}
    for order, raw_list in readings.items():
        if order == "white_ref":
            continue
        xyz = normalise(np.array(raw_list), white_raw, white_lab)
        lab = xyz_to_lab(xyz)
        measured[order] = {
            "patch": by_order[order],
            "xyz": xyz.tolist(),
            "lab": lab.tolist(),
            "rgb": np.rint(np.clip(_lab_to_rgb(lab), 0, 255)).astype(int).tolist(),
        }

    # ── Ink anchors ─────────────────────────────────────────────────────────
    n_ink = len(manifest["palette_measured_rgb"])
    anchors: list[dict | None] = [None] * n_ink
    for ink in range(n_ink):
        hits = [m for m in measured.values() if m["patch"]["ink_index"] == ink]
        if not hits:
            continue
        # Repeat patches (white, black) average; the spread is the drift check.
        labs = np.array([h["lab"] for h in hits])
        mean_lab = labs.mean(axis=0)
        current_lab = rgb_to_lab(np.array(manifest["palette_measured_rgb"][ink])).tolist()
        anchors[ink] = {
            "ink": ink,
            "name": manifest["ink_names"][ink],
            "n": len(hits),
            "lab": mean_lab.tolist(),
            "spread_de": (max(delta_e76(lab, mean_lab) for lab in labs) if len(hits) > 1 else 0.0),
            "rgb": np.rint(np.clip(_lab_to_rgb(mean_lab), 0, 255)).astype(int).tolist(),
            "current_rgb": manifest["palette_measured_rgb"][ink],
            "current_lab": current_lab,
            "shift_de": delta_e76(mean_lab, np.array(current_lab)),
        }

    # ── Tone response ───────────────────────────────────────────────────────
    black, white = anchors[0], anchors[1]
    ramp: list[dict] = []
    if black and white:
        y_black, y_white = (
            lab_to_xyz(np.array(black["lab"]))[1],
            lab_to_xyz(np.array(white["lab"]))[1],
        )
        rgb_black = np.array(black["rgb"], dtype=float)
        rgb_white = np.array(white["rgb"], dtype=float)
        for m in sorted(
            (m for m in measured.values() if m["patch"]["kind"] == "ramp"),
            key=lambda m: m["patch"]["actual_black_fraction"],
        ):
            f = m["patch"]["actual_black_fraction"]
            # Physics: area mixing is linear in reflectance (luminance). L*
            # depends only on Y/Yn, so a neutral of that luminance suffices.
            y_lin = f * y_black + (1 - f) * y_white
            l_reflectance = float(xyz_to_lab(D65_XYZ * y_lin)[0])
            # Pipeline: error diffusion averages in sRGB, so it *behaves* as if
            # coverage mixed linearly there. The gap between this and the
            # measurement is the mid-tone error the renderer is making.
            l_srgb = float(rgb_to_lab(f * rgb_black + (1 - f) * rgb_white)[0])
            ramp.append(
                {
                    "black_fraction": f,
                    "measured_l": m["lab"][0],
                    "predicted_l_reflectance": l_reflectance,
                    "predicted_l_srgb": l_srgb,
                    "error_vs_srgb": m["lab"][0] - l_srgb,
                    "measured_lab": m["lab"],
                }
            )

    return {"anchors": anchors, "ramp": ramp, "measured": measured}


def report(manifest: dict, result: dict, white_lab: np.ndarray | None) -> None:
    model = manifest["model"]
    anchors, ramp = result["anchors"], result["ramp"]

    print()
    print("=" * 74)
    print(f"  Ink anchors — {model}")
    print("=" * 74)
    if white_lab is None:
        print("  ! Normalised against panel white: the white ink's own tint has been")
        print("    forced to neutral and absolute lightness is not meaningful.")
    print()
    print(
        f"  {'ink':<8} {'measured L*a*b*':<26} {'measured RGB':<16} {'ΔE vs repo':>10}  {'repeat':>7}"
    )
    print("  " + "-" * 70)
    for a in anchors:
        if a is None:
            continue
        lab = f"{a['lab'][0]:6.2f} {a['lab'][1]:7.2f} {a['lab'][2]:7.2f}"
        rgb = f"({a['rgb'][0]:3d},{a['rgb'][1]:3d},{a['rgb'][2]:3d})"
        spread = f"{a['spread_de']:5.2f}" if a["n"] > 1 else "   —"
        print(f"  {a['name']:<8} {lab:<26} {rgb:<16} {a['shift_de']:10.2f}  {spread:>7}")
    missing = [manifest["ink_names"][i] for i, a in enumerate(anchors) if a is None]
    if missing:
        print(f"\n  ! not measured: {', '.join(missing)} — palette block below is incomplete")

    print()
    print(f"  Paste into python/hokku/screens/{model}/display.py:")
    print()
    print("    palette_measured_rgb = np.array(")
    print("        [")
    for i, a in enumerate(anchors):
        if a is None:
            cur = manifest["palette_measured_rgb"][i]
            vals = f"[{int(cur[0])}, {int(cur[1])}, {int(cur[2])}]"
            print(
                f"            {vals},  # {i} {manifest['ink_names'][i]} (NOT MEASURED — unchanged)"
            )
        else:
            vals = f"[{a['rgb'][0]}, {a['rgb'][1]}, {a['rgb'][2]}]"
            print(f"            {vals},  # {i} {a['name']}")
    print("        ],")
    print("        dtype=np.float32,")
    print("    )")

    if not ramp:
        print("\n  (no tone-response data — need black, white and at least one ramp patch)")
        return

    print()
    print("=" * 74)
    print("  Tone response — measured vs. what the pipeline assumes")
    print("=" * 74)
    print()
    print(
        f"  {'black area':>10} {'measured L*':>12} {'pred L* (sRGB)':>15} {'error':>8} {'pred L* (refl)':>15}"
    )
    print("  " + "-" * 66)
    for r in ramp:
        print(
            f"  {r['black_fraction'] * 100:9.1f}% {r['measured_l']:12.2f} "
            f"{r['predicted_l_srgb']:15.2f} {r['error_vs_srgb']:+8.2f} "
            f"{r['predicted_l_reflectance']:15.2f}"
        )
    worst = max(ramp, key=lambda r: abs(r["error_vs_srgb"]))
    mean_abs = float(np.mean([abs(r["error_vs_srgb"]) for r in ramp]))
    print()
    print(f"  Mean |error| vs the pipeline's assumption : {mean_abs:5.2f} L*")
    print(
        f"  Worst                                     : {worst['error_vs_srgb']:+5.2f} L* "
        f"at {worst['black_fraction'] * 100:.1f}% black"
    )
    print()
    if mean_abs < 2.0:
        print("  → Under ~2 L*, the linear-mixing assumption holds. No tone curve needed.")
    else:
        print("  → The panel does not mix linearly. A transfer curve applied before")
        print("    dithering would remove a systematic mid-tone error from every image.")
        direction = "darker" if worst["error_vs_srgb"] < 0 else "lighter"
        print(f"    Mid-tones currently land {direction} than the renderer intends.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("manifest", type=Path, help="the .json written by color_target.py")
    ap.add_argument("--from-file", type=Path, help="re-analyse saved readings instead of prompting")
    ap.add_argument(
        "--save", type=Path, help="where to write the readings (default: alongside the manifest)"
    )
    ap.add_argument("--white-ref-lab", help="L,a,b of the white reference standard under D65")
    ap.add_argument(
        "--relative-to-panel-white",
        action="store_true",
        help="no reference standard: normalise against the panel's own white ink (degraded)",
    )
    args = ap.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest["model"] not in DISPLAY_REGISTRY:
        raise SystemExit(f"Unknown model {manifest['model']!r} in manifest")

    if not args.white_ref_lab and not args.relative_to_panel_white:
        raise SystemExit(
            "Give --white-ref-lab L,a,b for the white standard you are measuring against,\n"
            "or pass --relative-to-panel-white to accept the degraded fallback."
        )
    white_lab = None
    if args.white_ref_lab:
        parts = [float(v) for v in args.white_ref_lab.split(",")]
        if len(parts) != 3:
            raise SystemExit("--white-ref-lab needs exactly three comma-separated numbers")
        white_lab = np.array(parts)

    if args.from_file:
        readings = json.loads(args.from_file.read_text(encoding="utf-8"))["readings"]
    else:
        readings = prompt_readings(manifest["patches"], white_needed=white_lab is not None)
        out = args.save or args.manifest.with_name(f"readings_{manifest['model']}.json")
        out.write_text(
            json.dumps(
                {
                    "model": manifest["model"],
                    "white_ref_lab": white_lab.tolist() if white_lab is not None else None,
                    "readings": readings,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nReadings saved to {out}")

    if not readings:
        raise SystemExit("No readings collected.")

    result = analyse(manifest, readings, white_lab)
    report(manifest, result, white_lab)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
