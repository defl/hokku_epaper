#!/usr/bin/env python3
"""Measure what each knob actually does, across the real library.

The search space has 21 dimensions and no evidence that all of them matter. Some
settings plausibly do nothing on this panel — a saturation boost is not visible
where the gamut has already run out, and a chroma threshold is inert if the space
it applies to is switched off. Searching an inert dimension costs the same as a
live one and finds noise.

So each knob is moved on its own, across a spread of real images, and the effect
is measured in units of the pipeline's own **noise floor**: the shipped renderer
draws its dither noise from an unseeded RNG (`image_renderer.py:800`), so
repeated renders of one config already differ, and a knob that moves a picture
less than that cannot be said to do anything at all.

Reported per knob:

  effect     distance moved in standardised metric space, in noise-floor units.
  moves      which metrics it moves most, and in which direction.

The threshold for "inert" is sqrt(2), not 1. The floor is measured as the spread
of repeated renders of ONE config, but a knob is judged by comparing TWO configs,
each carrying its own independent draw of the dither noise — so even a knob that
changes nothing scores about sqrt(2) times the single-render spread.

This prunes the search and, separately, says which of the shipped preset
differences are real. Two of the three presets differ in `drc_l_space` and
`drc_chroma_space`; if those turn out inert, that difference is decoration.

    python tools/knob_sensitivity.py --images 24 --out build/camcal/sensitivity.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import config_space
from fit_preference import CANDIDATE_METRICS
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_config import ImageConfig
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku_server import DEFAULT_BASE, image_config
from render_bank import evaluate, evaluate_many
from session_plan import farthest_point


def knob_values(base, name: str, kind: str, spec: tuple) -> list:
    """The values to probe: every category, or low/mid/high for a number."""
    current = config_space.get(base, name)
    if kind == "cat":
        return [v for v in spec if v != current]
    low, high, _step = spec
    mid = (low + high) / 2
    return [v for v in (low, mid, high) if abs(float(v) - float(current)) > 1e-9]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--features", type=Path, default=Path("build/camcal/library_features.csv"))
    ap.add_argument("--imagedir", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--images", type=int, default=24)
    ap.add_argument("--server", default=DEFAULT_BASE)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--noise-repeats", type=int, default=5)
    ap.add_argument("--cache", type=Path, default=Path("build/camcal/bank_seeded.sqlite"))
    ap.add_argument("--out", type=Path, default=Path("build/camcal/sensitivity.csv"))
    args = ap.parse_args(argv)

    try:
        base = image_config(base=args.server)
        print("  baseline: live server config")
    except Exception:
        base = PRESET_IMAGE_CONFIGS["default_general"]
        print("  baseline: repo default_general (server unreachable)")

    features = pd.read_csv(args.features)
    feats = ("mean_l", "mean_c", "oog_frac", "detail_energy")
    frame = features.dropna(subset=list(feats)).reset_index(drop=True)
    raw = frame[list(feats)].to_numpy(dtype=float)
    norm = (raw - raw.mean(0)) / np.maximum(raw.std(0), 1e-9)
    picked = farthest_point(norm, args.images, int(np.argmax(frame["oog_frac"].to_numpy())))
    images = [args.imagedir / str(frame.loc[i, "name"]) for i in picked]
    images = [p for p in images if p.exists()]
    print(f"  {len(images)} images spanning the library")

    # 1. the floor: how far apart two renders of the SAME config land
    print(f"  measuring the noise floor ({args.noise_repeats} unseeded renders x 4 images)...")
    floor_rows = []
    for image in images[:4]:
        for _ in range(args.noise_repeats):
            floor_rows.append(evaluate(image, base, args.model, seed=None))
    metrics = [m for m in CANDIDATE_METRICS if all(m in r for r in floor_rows)]
    floor_raw = np.array([[r[m] for m in metrics] for r in floor_rows])
    # Spread within each image, pooled — differences between images are not noise.
    per_image = floor_raw.reshape(-1, args.noise_repeats, len(metrics))
    noise_sd = per_image.std(axis=1).mean(axis=0)

    # 2. baseline for every image, then one knob at a time
    jobs: list[tuple[Path, ImageConfig]] = [(p, base) for p in images]
    index: list[tuple[str, str, object]] = [("__base__", "", None) for _ in images]
    for name, kind, spec in config_space.KNOBS:
        for value in knob_values(base, name, kind, spec):
            cfg = config_space.set_one(base, name, value)
            for image in images:
                jobs.append((image, cfg))
                index.append((name, str(value), image))
    print(f"  {len(jobs)} renders ({len(config_space.KNOBS)} knobs)...")
    results = evaluate_many(jobs, args.model, args.workers, args.cache, verbose=True)

    base_by_image = {
        str(images[i]): results[i] for i in range(len(images)) if "error" not in results[i]
    }
    # Two configs, two independent noise draws.
    inert_threshold = float(np.sqrt(2.0))
    scale = np.maximum(noise_sd, 1e-9)

    rows = []
    for (name, value, image), got in zip(index, results, strict=True):
        if name == "__base__" or not got or "error" in got:
            continue
        ref = base_by_image.get(str(image))
        if not ref or not all(m in got and m in ref for m in metrics):
            continue
        delta = np.array([got[m] - ref[m] for m in metrics]) / scale
        rows.append(
            {
                "knob": name,
                "value": value,
                "image": Path(str(image)).name,
                "effect": float(np.linalg.norm(delta) / np.sqrt(len(metrics))),
                **{f"d_{m}": float(d) for m, d in zip(metrics, delta, strict=True)},
            }
        )

    if not rows:
        print("  nothing measured")
        return 1
    table = pd.DataFrame(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False)

    # Plain dicts rather than pandas group objects: the reporting is simple and
    # the type checker cannot follow a groupby result usefully.
    knobs = sorted({r["knob"] for r in rows})
    stats = []
    for knob in knobs:
        sub = [r for r in rows if r["knob"] == knob]
        effects = np.array([r["effect"] for r in sub])
        moved = {m: float(np.mean([r[f"d_{m}"] for r in sub])) for m in metrics}
        strongest = sorted(moved, key=lambda m: -abs(moved[m]))[:3]
        stats.append(
            {
                "knob": knob,
                "mean": float(effects.mean()),
                "max": float(effects.max()),
                "moves": ", ".join(f"{m}{'+' if moved[m] > 0 else '-'}" for m in strongest),
            }
        )
    stats.sort(key=lambda s: -s["mean"])

    print()
    print(
        f"  effect per knob, in noise-floor units "
        f"(below {inert_threshold:.2f} = nothing measurable)"
    )
    print(f"  {'knob':34s} {'mean':>7s} {'max':>7s}   moves most")
    print("  " + "-" * 78)
    for s in stats:
        flag = "" if s["mean"] >= inert_threshold else "   <- inert"
        print(f"  {s['knob']:34s} {s['mean']:7.2f} {s['max']:7.2f}   {s['moves']}{flag}")

    inert = [s["knob"] for s in stats if s["mean"] < inert_threshold]
    print()
    print(f"  {len(inert)} of {len(stats)} knobs move less than the render noise")
    if inert:
        print(f"    {', '.join(inert)}")

    print(f"\n  {len(table)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
