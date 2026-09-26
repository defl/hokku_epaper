#!/usr/bin/env python3
"""Build a two-arm page from captures that already exist, spanning a chosen axis.

Two arms is the most sensitive comparison available, and when the captures are
already on disk it costs no panel time at all. This exists because a fifteen-arm
page could not answer whether a palette-LUT change is judged on its colour or on
the dither grain it perturbs: pooled splits confounded arm identity with colour
magnitude, and within-arm splits ran to n=6.

Photographs are picked to span the baseline-to-arm colour difference, so if the
verdict changes with how much the colour moved, that shows up as a gradient
rather than as one number.

    python tools/camcal/focus_pair.py --arm oklab --images 12
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, "tools")
sys.path.insert(0, "python")

from cam_compare import delta_e00, model_lab
from hokku.screens.registry import DISPLAY_REGISTRY
from render_bank import load_model

BLOCK = 8


def ink_raster(path: Path, palette: np.ndarray) -> np.ndarray | None:
    bgr = cv2.imread(str(path))
    if bgr is None:
        return None
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    idx = np.zeros(rgb.shape[:2], np.uint8)
    seen = np.zeros(rgb.shape[:2], bool)
    for i, ink in enumerate(palette):
        hit = np.all(rgb == ink, axis=-1)
        idx[hit] = i
        seen |= hit
    return idx if seen.all() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", type=Path, default=Path("build/camcal/bigrun_plan.json"))
    ap.add_argument("--session", type=Path, default=Path("build/camcal/session6"))
    ap.add_argument("--arm", default="oklab")
    ap.add_argument("--ref", default="live")
    ap.add_argument("--images", type=int, default=12)
    ap.add_argument("--out", type=Path, default=Path("build/camcal/focus_plan.json"))
    args = ap.parse_args(argv)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    by_image: dict[str, dict[str, dict]] = {}
    for c in plan["candidates"]:
        by_image.setdefault(c["image_name"], {})[c["config_tag"]] = c
    pairs = {n: v for n, v in by_image.items() if args.arm in v and args.ref in v}
    print(f"{len(pairs)} photographs carry both {args.ref} and {args.arm}")

    display = DISPLAY_REGISTRY["huessen_epf1301"]
    model = load_model()
    palette = np.rint(display.palette_measured_rgb).clip(0, 255).astype(np.uint8)

    scored = []
    for n, (name, arms) in enumerate(sorted(pairs.items()), 1):
        a = ink_raster(args.session / f"{arms[args.ref]['tag']}__expected.png", palette)
        b = ink_raster(args.session / f"{arms[args.arm]['tag']}__expected.png", palette)
        if a is None or b is None:
            continue
        de = float(
            delta_e00(
                model_lab(a, model["prim_mat"], model["n"], BLOCK),
                model_lab(b, model["prim_mat"], model["n"], BLOCK),
            ).mean()
        )
        scored.append((name, de))
        if n % 15 == 0:
            print(f"  measured {n}/{len(pairs)}", flush=True)

    scored.sort(key=lambda r: r[1])
    # Evenly spaced through the sorted order: the full range of colour change,
    # not just the loudest examples.
    step = max(1, len(scored) // args.images)
    picked = scored[::step][: args.images]
    print(
        f"\npicked {len(picked)} photographs spanning {picked[0][1]:.2f}..{picked[-1][1]:.2f} dE00:"
    )
    for name, de in picked:
        print(f"  {de:5.2f}  {name[:46]}")

    names = [n for n, _ in picked]
    args.out.write_text(
        json.dumps(
            {
                "model": plan["model"],
                "arms": {args.ref: {"lut": None}, args.arm: {"lut": None}},
                "images": names,
                "colour_de": dict(picked),
                "candidates": [by_image[n][a] for n in names for a in (args.ref, args.arm)],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {args.out}: {len(names) * 2} captures, all already on disk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
