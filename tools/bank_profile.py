#!/usr/bin/env python3
"""Where does an evaluation actually spend its time?

Throughput came in at 0.7 renders/s across 8 workers against a render that takes
1.3 s on its own, which is an order of magnitude unaccounted for. Guessing at it
cost one wrong fix already, so this times each stage directly.

    python tools/bank_profile.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from cam_compare import block_mean, delta_e00, detail_ratio, model_lab, source_canvas
from color_validate_photos import lab_img, srgb_img_to_xyz
from dither_search import warm_blue_fractions
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_quality import image_compare
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from render_bank import (
    BLOCK,
    _region_masks,
    adapted_reference,
    load_model,
    render,
    render_seed,
    renderer_for,
)


class Timer:
    def __init__(self):
        self.marks: list[tuple[str, float]] = []
        self.t = time.perf_counter()

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.marks.append((name, now - self.t))
        self.t = now

    def report(self) -> None:
        total = sum(v for _n, v in self.marks)
        print(f"  {'stage':34s} {'seconds':>9s} {'share':>7s}")
        print("  " + "-" * 54)
        for name, value in sorted(self.marks, key=lambda kv: -kv[1]):
            print(f"  {name:34s} {value:9.3f} {100 * value / total:6.1f}%")
        print(f"  {'TOTAL':34s} {total:9.3f}")


def main() -> int:
    display = DISPLAY_REGISTRY["huessen_epf1301"]
    cfg = PRESET_IMAGE_CONFIGS["default_general"]
    images = sorted(p for p in Path("build/camcal/server_images").iterdir() if p.is_file())
    image = images[0]

    model = load_model()
    renderer_for(display)  # pay the JIT before timing anything
    with Image.open(image) as raw:
        render(display, raw.convert("RGB"), cfg, seed=1)

    t = Timer()
    canvas = source_canvas(image, display, "x")
    t.mark("source_canvas (cached per image)")
    src_lab = lab_img(block_mean(srgb_img_to_xyz(canvas), BLOCK))
    t.mark("source Lab + block mean (cached)")
    ref_lab = adapted_reference(src_lab)
    masks = _region_masks(src_lab, model["ceiling"])
    t.mark("adapted ref + masks (cached)")

    with Image.open(image) as raw:
        img = raw.convert("RGB")
    t.mark("PIL open + convert")
    idx = render(display, img, cfg, seed=render_seed(image, cfg))
    t.mark("render (the actual pipeline)")

    yn = model_lab(idx, model["prim_mat"], model["n"], BLOCK)
    t.mark("model_lab (Yule-Nielsen)")
    delta_e00(src_lab, yn)
    delta_e00(ref_lab, yn)
    t.mark("delta_e00 x2 (block level)")
    for mask in masks.values():
        if mask.sum() >= 50:
            delta_e00(src_lab, yn)[mask].mean()
    t.mark("per-region stats")
    detail_ratio(src_lab, yn, np.ones(src_lab.shape[:2], bool))
    t.mark("detail_ratio")

    ink_rgb = np.asarray(display.palette_measured_rgb, dtype=np.float64)[idx]
    t.mark("palette lookup (full res)")
    image_compare(canvas, ink_rgb)
    t.mark("image_compare (FULL res)")
    warm_blue_fractions(canvas, idx, np.ones(idx.shape, bool))
    t.mark("warm_blue_fractions (full res)")

    t.report()
    print("\n  stages marked (cached) are paid once per image, not per config.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
