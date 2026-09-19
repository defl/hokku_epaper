#!/usr/bin/env python3
"""Score named presets, or sampled candidates, across a set of images.

The driver for `render_bank`. It exists as a file rather than a snippet because
Windows multiprocessing uses spawn: every worker re-imports the main module, and
a script piped in on stdin cannot be re-imported, so a pool started that way
hangs producing nothing. That cost an hour once; it is written down here so it
does not cost another.

    python tools/bank_sweep.py --presets default_general default_face default_bw
    python tools/bank_sweep.py --sample 40 --seed 1 --limit 12
"""

from __future__ import annotations

import argparse
import sys
import time
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

# The metrics worth printing in a summary. The bank returns far more; these are
# the ones that have historically disagreed with each other, which is exactly why
# they are shown side by side rather than collapsed.
SUMMARY = (
    "yn_de00",
    "yn_adapted_de00",
    "yn_dL",
    "yn_adapted_dL",
    "yn_dC",
    "yn_dhue",
    "detail_l",
    "detail_c",
    "chroma_use",
    "chroma_vs_source",
    "skin_dhue",
    "oog_dhue",
    "ink_neutral_leak",
    "ink_warm_blue",
    "ink_lips_blue",
    "ink_error_roughness",
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--images", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--presets", nargs="*", default=["default_general"])
    ap.add_argument(
        "--live-base",
        action="store_true",
        help="add the server's live config as a variant — it has drifted from the repo",
    )
    ap.add_argument("--server", default=DEFAULT_BASE)
    ap.add_argument("--sample", type=int, default=0, help="also score N sampled candidates")
    ap.add_argument("--sample-knobs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--cache", type=Path, default=Path("build/camcal/bank_seeded.sqlite"))
    ap.add_argument("--out", type=Path, default=Path("build/camcal/bank_sweep.csv"))
    args = ap.parse_args(argv)

    images = sorted(p for p in args.images.iterdir() if p.is_file())
    if args.limit:
        images = images[: args.limit]
    if not images:
        print(f"  no images under {args.images}")
        return 1

    variants: list[tuple[str, ImageConfig]] = [(n, PRESET_IMAGE_CONFIGS[n]) for n in args.presets]
    if args.live_base:
        try:
            variants.append(("live_server", image_config(base=args.server)))
        except Exception as exc:
            print(f"  (live config unavailable: {type(exc).__name__}: {exc})")
    if args.sample:
        rng = np.random.default_rng(args.seed)
        base = variants[0][1]
        for i in range(args.sample):
            variants.append((f"sample{i:03d}", config_space.sample(base, rng, args.sample_knobs)))

    jobs, tags = [], []
    for name, cfg in variants:
        for img in images:
            jobs.append((img, cfg))
            tags.append(name)

    print(f"  {len(images)} images x {len(variants)} variants = {len(jobs)} evaluations")
    started = time.time()
    results = evaluate_many(jobs, args.model, args.workers or None, args.cache, verbose=True)
    print(f"  wall {time.time() - started:.1f}s")

    rows = []
    for (img, _cfg), tag, metrics in zip(jobs, tags, results, strict=True):
        if "error" in metrics:
            print(f"  ERROR {tag} {img.name}: {metrics['error']}")
            continue
        rows.append({"image": img.name, "variant": tag, **metrics})
    if not rows:
        print("  every evaluation failed")
        return 1

    frame = pd.DataFrame(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.out, index=False)

    names = [n for n, _ in variants]
    shown = [n for n in names if n in set(frame["variant"])][:8]
    print()
    print(f"  {'metric':22s}" + "".join(f"{n[:16]:>17s}" for n in shown))
    print("  " + "-" * (22 + 17 * len(shown)))
    for key in SUMMARY:
        if key not in frame:
            continue
        line = f"  {key:22s}"
        for name in shown:
            line += f"{frame.loc[frame['variant'] == name, key].mean():17.3f}"
        print(line)

    if args.sample:
        base = variants[0][1]
        print("\n  sampled candidates (knobs changed from the first variant):")
        for name, cfg in variants[len(args.presets) + int(args.live_base) :][:12]:
            print(f"    {name}: {config_space.describe(cfg, base)}")
    print(f"\n  {len(frame)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
