"""Screen candidate rendering arms for visibility before spending panel time.

Every capture costs ~34 seconds of e-paper refresh, so an arm whose render is
indistinguishable from the baseline spends a minute to produce a coin flip. The
hard lesson of the previous rounds was that the ratings resolve differences of
about 1.5 dE00 and no smaller; the dither's own noise is ~0.7.

So each candidate is rendered against the live config on a spread of photographs
and reported by its median block dE00. What ships into the capture plan is chosen
from this table, with coverage across the themes the rating notes actually name —
saturation, local contrast, palette mapping, detail — rather than by magnitude
alone, since the largest change is not automatically the most informative one.

    python build/camcal/analysis/screen_arms.py --images 12
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "tools")
sys.path.insert(0, "python")

import config_space
import production
from cam_compare import delta_e00, model_lab
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_renderer import open_image_for_render
from render_bank import load_model, render

CAM = Path("build/camcal")

# Candidate arms: a name, and what it changes about the live config. Single-knob
# arms are here so a verdict is attributable to one thing; the combinations at
# the end are the configs that might plausibly be shipped.
CANDIDATES: dict[str, list[tuple[str, object]]] = {
    # local contrast and tone
    "clahe_off": [("clahe_clip_limit", 0.0)],
    "clahe_low": [("clahe_clip_limit", 1.0)],
    "gamma_lo": [("prepare_gamma", 0.85)],
    "gamma_hi": [("prepare_gamma", 1.10)],
    "contrast_up": [("prepare_contrast", 1.25)],
    "contrast_dn": [("prepare_contrast", 0.95)],
    "midtone_up": [("prepare_midtone", 1.06)],
    "darker": [("prepare_brightness", 0.95)],
    # colour
    "desat": [("adaptive_saturate_space", "off"), ("color_enhance", 0.85)],
    "saturate": [("adaptive_saturate_space", "off"), ("color_enhance", 1.20)],
    "vivid": [("adaptive_vivid", True)],
    "satmax": [("saturate_max_enhance", 1.20)],
    "sat_cielab": [("adaptive_saturate_space", "cielab")],
    "sat_off": [("adaptive_saturate_space", "off")],
    "scale_chroma": [("scale_chroma", True)],
    # palette mapping and dithering
    "oklab": [("dither.lut_name", "oklab_hue_aware")],
    "weighted": [("dither.lut_name", "hue_aware_weighted")],
    "cam16": [("dither.lut_name", "cam16ucs_hue_aware")],
    "nchroma_lo": [("dither.neutral_chroma", 4.0)],
    "nchroma_hi": [("dither.neutral_chroma", 14.0)],
    "stucki": [("dither.algorithm", "stucki")],
    "huegate_wide": [("dither.hue_cutoff_deg", 120.0)],
    # detail
    "sharper": [("prepare_usm_amount", 170)],
    "softer": [("prepare_usm_amount", 60)],
    # plausible shipped combinations
    "calm": [
        ("clahe_clip_limit", 1.0),
        ("dither.lut_name", "oklab_hue_aware"),
        ("color_enhance", 0.95),
    ],
    "rich": [
        ("clahe_clip_limit", 1.0),
        ("adaptive_vivid", True),
        ("dither.lut_name", "hue_aware_weighted"),
    ],
    "clean": [
        ("clahe_clip_limit", 0.0),
        ("dither.neutral_chroma", 4.0),
        ("adaptive_saturate_space", "cielab"),
    ],
}


def build(base, changes):
    cfg = base
    for knob, value in changes:
        cfg = config_space.set_one(cfg, knob, value)
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--images", type=int, default=12)
    ap.add_argument("--crop", type=float, default=0.14)
    ap.add_argument("--out", type=Path, default=CAM / "arm_screen.json")
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY["huessen_epf1301"]
    model = load_model()
    # Production's own decision per photograph, not one global config: 204 of the
    # 245 library images get the face preset, whose saturation stage differs from
    # the default's, so an arm screened against the default can be inert on most
    # of the library.
    app = production.live_app_config()
    clf = production.classifier(app)

    names = [
        n
        for n in (CAM / "selected_images.txt").read_text(encoding="utf-8").splitlines()
        if n.strip()
    ]
    # Evenly spaced through the farthest-point order, so the screen sees the
    # same easy-to-hard spread the capture plan will.
    step = max(1, len(names) // args.images)
    probe = names[::step][: args.images]
    print(f"screening {len(CANDIDATES)} arms on {len(probe)} photographs\n")

    def raster(path: Path, cfg):
        with open_image_for_render(path) as img:
            return render(display, img, cfg, seed=0, crop_to_fill_threshold=args.crop)

    results: dict[str, list[float]] = {name: [] for name in CANDIDATES}
    presets: list[str] = []
    for i, name in enumerate(probe, 1):
        path = CAM / "server_images" / name
        base = production.decision_for(clf, path).image_config
        presets.append(production.preset_of(app, production.decision_for(clf, path)))
        live = model_lab(raster(path, base), model["prim_mat"], model["n"], 8)
        for arm, changes in CANDIDATES.items():
            got = model_lab(raster(path, build(base, changes)), model["prim_mat"], model["n"], 8)
            results[arm].append(float(delta_e00(live, got).mean()))
        print(f"  {i}/{len(probe)} {name[:40]}", flush=True)

    print(f"\n{'arm':14s} {'median dE':>10s} {'min':>7s} {'max':>7s}   verdict")
    table = {}
    for arm, values in sorted(results.items(), key=lambda kv: -float(np.median(kv[1]))):
        med, lo, hi = float(np.median(values)), float(min(values)), float(max(values))
        table[arm] = {"median": med, "min": lo, "max": hi}
        verdict = "strong" if med >= 2.0 else ("usable" if med >= 1.0 else "TOO SMALL")
        print(f"{arm:14s} {med:10.2f} {lo:7.2f} {hi:7.2f}   {verdict}")

    args.out.write_text(json.dumps(table, indent=1), encoding="utf-8")
    print(f"\nwrote {args.out}  (dither noise ~0.7 dE00; ratings resolve ~1.5)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
