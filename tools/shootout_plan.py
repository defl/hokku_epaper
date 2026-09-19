#!/usr/bin/env python3
"""Build a head-to-head plan: a few named configs across many images.

The search-and-judge loop is for discovering what to try. This is for settling a
specific question once a candidate has emerged — here, whether the two settings
that led both judging datasets actually beat what ships, and whether they combine.

Deliberately a factorial rather than a pile of variants. `rand005` and
`lut_weighted` each looked good on their own; running baseline, each alone, and
both together is what distinguishes "they fix different problems and add up" from
"they fix the same problem and the second buys nothing". Four cells across many
photographs answers that far better than four dozen one-off candidates on a few.

Reuses whatever is already captured — the baseline exists for every image from
the previous session — so only the new cells cost panel time.

    python tools/shootout_plan.py --out build/camcal/shootout_plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import config_space
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku_server import DEFAULT_BASE, image_config

# The two changes that led both judging datasets, alone and together.
#
#   rand005        rated "ok" and won 82 % of its 11 pairwise trials — the best
#                  of anything tested. Lowers the hue gate's neutral exemption
#                  and *reduces* contrast.
#   lut_weighted   rated "ok" on both its appearances. Weights the palette
#                  distance 2L^2+a^2+b^2, which stops lighter inks being spent
#                  cancelling a tint in the measured black that is invisible at
#                  that lightness.
#   axis_lut_oklab measured the lowest blue-in-lips of any colour config by a
#                  wide margin — 0.6 % against the baseline's 4.1 %, a sevenfold
#                  reduction — which matches the observation that oklab "is MUCH
#                  better at not getting blue lips". It nonetheless lost overall
#                  (26 % of its pairwise trials) for unrelated reasons: more blue
#                  across the wider face, red ink still at 41 %, and repeated
#                  "washed out" notes. Named to match the earlier session's tag so
#                  its existing captures are reused rather than re-shot.
#   oklab_softer   oklab's palette with rand005's two changes, to test whether the
#                  lips advantage survives once the contrast complaint is treated.
ARMS: dict[str, list[tuple[str, object]]] = {
    "baseline": [],
    "rand005": [("dither.neutral_chroma", 6.0), ("prepare_contrast", 0.9)],
    "lut_weighted": [("dither.lut_name", "hue_aware_weighted")],
    "both": [
        ("dither.neutral_chroma", 6.0),
        ("prepare_contrast", 0.9),
        ("dither.lut_name", "hue_aware_weighted"),
    ],
    "axis_lut_oklab": [("dither.lut_name", "oklab_hue_aware")],
    "oklab_softer": [
        ("dither.lut_name", "oklab_hue_aware"),
        ("dither.neutral_chroma", 6.0),
        ("prepare_contrast", 0.9),
    ],
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--features", type=Path, default=Path("build/camcal/library_features.csv"))
    ap.add_argument("--imagedir", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--source-plan", type=Path, default=Path("build/camcal/session40_plan.json"))
    ap.add_argument("--server", default=DEFAULT_BASE)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    try:
        base = image_config(base=args.server)
        print("  baseline: live server config")
    except Exception:
        base = PRESET_IMAGE_CONFIGS["default_general"]
        print("  baseline: repo default_general (server unreachable)")

    # Same images as the judged session, so the previous captures line up and the
    # spread over the library is already established.
    source = json.loads(args.source_plan.read_text(encoding="utf-8"))
    names = sorted({c["image_name"] for c in source["candidates"]})
    features = pd.read_csv(args.features).set_index("name")
    print(f"  {len(names)} images, {len(ARMS)} arms = {len(names) * len(ARMS)} cells")

    candidates = []
    for name in names:
        if not (args.imagedir / name).exists():
            continue
        for arm, changes in ARMS.items():
            cfg = base
            for knob, value in changes:
                cfg = config_space.set_one(cfg, knob, value)
            candidates.append(
                {
                    "tag": f"{Path(name).stem[:22]}__{arm}",
                    "image": str(args.imagedir / name),
                    "image_name": name,
                    "config_tag": arm,
                    "config": asdict(cfg),
                    "knobs": config_space.describe(cfg, base),
                }
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "model": args.model,
                "baseline_config": asdict(base),
                "arms": {k: [list(c) for c in v] for k, v in ARMS.items()},
                "images": names,
                "candidates": candidates,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    for arm, changes in ARMS.items():
        cfg = base
        for knob, value in changes:
            cfg = config_space.set_one(cfg, knob, value)
        print(f"    {arm:14s} {config_space.describe(cfg, base)}")
    print(f"  {len(candidates)} cells -> {args.out}")
    del features
    return 0


if __name__ == "__main__":
    sys.exit(main())
