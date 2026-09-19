#!/usr/bin/env python3
"""Choose what to photograph for a judging session, by measuring first.

Panel time is the scarce resource — roughly a minute per candidate — so the
choice of what to put on glass decides what a session can possibly teach. Two
selections happen here, and both are made by measurement rather than by taste,
because taste is the thing being measured and using it twice would be circular.

**Which images.** Farthest-point selection over the library's measured features,
so the set spans easy-to-hard rather than clustering on whatever was recently
interesting. Seeded with the single hardest image, so the worst case is present
by construction instead of by luck.

**Which configs.** A large pool of candidates is generated and scored offline
first — cheap, about 1.3 s each — and the ones actually photographed are chosen
to span the *metric* space by farthest-point selection. This is the part that
matters. Fifty random configs mostly render near-identically, and a session spent
on near-identical pairs teaches nothing: a preference model needs trials whose
outcomes genuinely differ, and ties are only informative when they are ties
between things that measurably differ. The shipped baseline is always included
so every candidate has something to be judged against.

    python tools/session_plan.py --images 6 --candidates 8 \\
        --out build/camcal/session1_plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import config_space
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_config import ImageConfig
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku_server import DEFAULT_BASE, image_config
from render_bank import evaluate_many

# The axes a candidate can differ along that a person would actually notice.
# Selection standardises these and spreads over them; metrics outside this list
# still get recorded, they just do not drive which candidates are photographed.
SPREAD_METRICS = (
    "yn_adapted_dL",
    "yn_dC",
    "yn_dhue",
    "detail_l",
    "detail_c",
    "chroma_use",
    "ink_neutral_leak",
    "ink_warm_blue",
    "ink_error_roughness",
)

IMAGE_FEATURES = ("mean_l", "mean_c", "oog_frac", "detail_energy", "p95_c")


def farthest_point(feats: np.ndarray, count: int, seed_index: int) -> list[int]:
    """Greedy max-min selection: each pick is the furthest from all previous."""
    chosen = [seed_index]
    dist = np.linalg.norm(feats - feats[seed_index], axis=1)
    while len(chosen) < min(count, len(feats)):
        k = int(np.argmax(dist))
        if k in chosen:
            break
        chosen.append(k)
        dist = np.minimum(dist, np.linalg.norm(feats - feats[k], axis=1))
    return chosen


def pick_images(features: pd.DataFrame, count: int, must: list[str]) -> list[str]:
    """Spread over difficulty, with any named images forced in."""
    frame = features.dropna(subset=list(IMAGE_FEATURES)).reset_index(drop=True)
    raw = frame[list(IMAGE_FEATURES)].to_numpy(dtype=float)
    norm = (raw - raw.mean(0)) / np.maximum(raw.std(0), 1e-9)
    # Seed on the most out-of-gamut image: it is the one the current pipeline
    # handles worst, so it must be in any set used to judge a replacement.
    seed = int(np.argmax(frame["oog_frac"].to_numpy()))
    picked = [str(frame.loc[i, "name"]) for i in farthest_point(norm, count, seed)]
    for name in reversed(must):
        if name and name not in picked:
            picked.insert(0, name)
    return picked[: max(count, len(must))]


def candidate_pool(base: ImageConfig, size: int, seed: int) -> list[tuple[str, ImageConfig]]:
    """The configs worth considering: presets, deliberate extremes, then samples.

    the extremes are hand-listed rather than sampled because a random walk in 21
    dimensions rarely lands on a clean single-axis change, and a session that
    cannot separate "more sharpening" from "more saturation" cannot attribute a
    preference to either.
    """
    pool: list[tuple[str, ImageConfig]] = [("baseline", base)]
    for name in ("default_general", "default_face", "default_bw"):
        pool.append((f"preset_{name}", PRESET_IMAGE_CONFIGS[name]))

    axes: list[tuple[str, str, object]] = [
        ("clahe_off", "clahe_clip_limit", 0.0),
        ("clahe_high", "clahe_clip_limit", 3.0),
        ("usm_off", "prepare_usm_amount", 0.0),
        ("usm_high", "prepare_usm_amount", 220.0),
        ("sat_off", "adaptive_saturate_space", "off"),
        ("sat_cielab", "adaptive_saturate_space", "cielab"),
        ("sat_high", "saturate_max_enhance", 1.6),
        ("colour_high", "color_enhance", 1.6),
        ("colour_low", "color_enhance", 0.9),
        ("gamma_low", "prepare_gamma", 0.70),
        ("gamma_high", "prepare_gamma", 1.15),
        ("contrast_high", "prepare_contrast", 1.35),
        ("fs", "dither.algorithm", "floyd_steinberg"),
        ("stucki", "dither.algorithm", "stucki"),
        ("lut_weighted", "dither.lut_name", "hue_aware_weighted"),
        ("lut_oklab", "dither.lut_name", "oklab_hue_aware"),
        ("lut_cam16", "dither.lut_name", "cam16ucs_hue_aware"),
        ("nchroma_high", "dither.neutral_chroma", 18.0),
        ("huegate_wide", "dither.hue_cutoff_deg", 130.0),
        ("huegate_tight", "dither.hue_cutoff_deg", 60.0),
        ("noise_off", "dither_noise", 0.0),
        ("vivid_off", "adaptive_vivid", False),
    ]
    for tag, knob, value in axes:
        pool.append((f"axis_{tag}", config_space.set_one(base, knob, value)))

    rng = np.random.default_rng(seed)
    for i in range(max(0, size - len(pool))):
        pool.append((f"rand{i:03d}", config_space.sample(base, rng, knobs=3)))
    return pool


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--features", type=Path, default=Path("build/camcal/library_features.csv"))
    ap.add_argument("--imagedir", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--images", type=int, default=6)
    ap.add_argument("--must", nargs="*", default=["20080529_035054000_iOS.jpg"])
    ap.add_argument("--candidates", type=int, default=8, help="photographed per image")
    ap.add_argument("--pool", type=int, default=60, help="scored offline per image")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--server", default=DEFAULT_BASE)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--cache", type=Path, default=Path("build/camcal/bank_seeded.sqlite"))
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    try:
        base = image_config(base=args.server)
        base_source = "live server"
    except Exception as exc:
        base = PRESET_IMAGE_CONFIGS["default_general"]
        base_source = f"repo default_general (server unreachable: {type(exc).__name__}: {exc})"
    print(f"  baseline: {base_source}")

    features = pd.read_csv(args.features)
    names = pick_images(features, args.images, args.must)
    print(f"  images ({len(names)}):")
    for name in names:
        row = features.loc[features["name"] == name]
        if len(row):
            print(
                f"    {name[:44]:44s} oog {100 * float(row['oog_frac'].iloc[0]):3.0f}%"
                f"  L {float(row['mean_l'].iloc[0]):5.1f}  C {float(row['mean_c'].iloc[0]):5.1f}"
                f"  faces {int(row['n_faces'].iloc[0])}"
            )

    pool = candidate_pool(base, args.pool, args.seed)
    print(f"  scoring {len(pool)} candidates x {len(names)} images offline...")
    jobs, index = [], []
    for name in names:
        path = args.imagedir / name
        if not path.exists():
            print(f"    (missing {name}, skipped)")
            continue
        for tag, cfg in pool:
            jobs.append((path, cfg))
            index.append((name, tag))
    results = evaluate_many(jobs, args.model, args.workers or None, args.cache, verbose=True)

    candidates = []
    for name in names:
        rows = [
            (tag, m)
            for (nm, tag), m in zip(index, results, strict=True)
            if nm == name and "error" not in m and m
        ]
        if len(rows) < 2:
            print(f"    {name}: too few scored candidates, skipped")
            continue
        available = [k for k in SPREAD_METRICS if all(k in m for _t, m in rows)]
        raw = np.array([[m[k] for k in available] for _t, m in rows], dtype=float)
        norm = (raw - raw.mean(0)) / np.maximum(raw.std(0), 1e-9)
        seed_index = next((i for i, (t, _m) in enumerate(rows) if t == "baseline"), 0)
        for i in farthest_point(norm, args.candidates, seed_index):
            tag, metrics = rows[i]
            cfg = next(c for t, c in pool if t == tag)
            candidates.append(
                {
                    "tag": f"{Path(name).stem[:22]}__{tag}",
                    "image": str(args.imagedir / name),
                    "image_name": name,
                    "config_tag": tag,
                    "config": asdict(cfg),
                    "metrics": metrics,
                    "knobs": config_space.describe(cfg, base),
                }
            )
        print(f"    {name[:40]:40s} -> {args.candidates} candidates")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "model": args.model,
                "baseline_source": base_source,
                "baseline_config": asdict(base),
                "images": names,
                "candidates": candidates,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    minutes = len(candidates) * 75 / 60
    print(f"\n  {len(candidates)} candidates -> {args.out}")
    print(f"  estimated panel time ~{minutes:.0f} min at 75 s each")
    return 0


if __name__ == "__main__":
    sys.exit(main())
