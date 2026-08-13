#!/usr/bin/env python3
"""Export requested-vs-measured colour pairs as JSON, for the 3-D LUT viewer.

Each row that carries both a requested sRGB and a measurement is one sample of
the transfer function a correction LUT would have to invert: "you asked for this
colour, the glass produced that one". Pairing them in CIELAB makes the shape of
the problem visible — where the panel can follow a request, and where it simply
cannot go.

Two framings are exported, because neither answers the whole question:

``absolute``
    Both sides in CIELAB against D65, as measured. Honest about the physical
    gap, but it charges the panel for something no reflective display can do:
    paper white is L* 68 against sRGB's L* 100, so every sample looks like a
    failure even where the rendering is faithful.

``adapted``
    The *measurement* re-scaled so paper white reads as reference white, then
    compared against the request unchanged. This is the fair question — a viewer
    looking at paper adapts to the paper, so this asks whether the colour is
    right relative to the brightest thing in the scene.

Note the adaptation is applied to the measurement, not to the request. Scaling
the request down into the panel's range instead would look symmetrical and is
not: it asks what a gamut-mapped request should have become, which is a question
about the compressor's intent rather than about the panel, and it silently
credits the pipeline for its own tone mapping.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from color_model import lab, load_records, primaries, xyz_to_srgb8

# sRGB (D65) -> XYZ, scaled so Y = 100 for white.
SRGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)


def srgb_to_xyz_pct(rgb: np.ndarray) -> np.ndarray:
    """8-bit sRGB -> XYZ percent (Y=100 for white), undoing the sRGB transfer curve."""
    v = np.asarray(rgb, dtype=float) / 255.0
    lin = np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
    return (SRGB_TO_XYZ @ lin) * 100.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--data",
        type=pathlib.Path,
        default=pathlib.Path("docs/screens/bigme_f7/measurements/data/campaign.jsonl"),
    )
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args(argv)

    recs = load_records(args.data)
    prim = primaries(recs)
    white = prim["white"]["xyz"]

    # Per-channel scaling of the measurement so paper white lands on reference
    # white — a von-Kries-style adaptation to the paper rather than a full
    # chromatic adaptation transform, which is enough for "is the colour right
    # relative to the brightest thing here?" without pretending to more rigour
    # than the question needs.
    scale = white / srgb_to_xyz_pct(np.array([255, 255, 255]))

    samples = []
    for r in recs:
        rgb = r.get("rgb")
        if not rgb or "xyz_d65_pct" not in r:
            continue
        want_xyz = srgb_to_xyz_pct(np.array(rgb))
        got_xyz = np.array(r["xyz_d65_pct"])
        got_lab = lab(got_xyz)
        samples.append(
            {
                "rgb": [int(c) for c in rgb],
                # Screen colour for the measured point, adapted so paper white
                # displays as screen white. Drawing absolute reflectance instead
                # would render every measured colour as a dark sludge — accurate
                # as a number, but a viewer looking at paper adapts to the paper,
                # so the adapted version is what the eye actually reports.
                "got_rgb": list(xyz_to_srgb8(got_xyz / scale)),
                "want": [round(float(x), 2) for x in lab(want_xyz)],
                "got": [round(float(x), 2) for x in got_lab],
                "got_adapted": [round(float(x), 2) for x in lab(got_xyz / scale)],
                "phase": r.get("phase"),
                "config": r.get("config_name"),
                "inherited": bool(r.get("duplicate_of")),
            }
        )

    inks = {
        name: {
            "lab": [round(float(x), 2) for x in lab(prim[name]["xyz"])],
            "reads": prim[name]["n"],
        }
        for name in prim
    }

    payload = {
        "samples": samples,
        "inks": inks,
        "white_lab": [round(float(x), 2) for x in lab(white)],
        "black_lab": [round(float(x), 2) for x in lab(prim["black"]["xyz"])],
        "source": str(args.data),
    }
    args.out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    def mean_de(key: str) -> tuple[float, float]:
        d = [float(np.linalg.norm(np.array(s["want"]) - np.array(s[key]))) for s in samples]
        return float(np.mean(d)), float(np.median(d))

    print(f"{len(samples)} pairs -> {args.out}")
    for label, key in (("absolute", "got"), ("adapted ", "got_adapted")):
        m, md = mean_de(key)
        print(f"  {label} dE76: mean {m:.2f}  median {md:.2f}")
    print(f"  panel white L* {payload['white_lab'][0]:.2f}, black L* {payload['black_lab'][0]:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
