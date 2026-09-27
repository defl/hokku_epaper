#!/usr/bin/env python3
"""Head-to-head over the gamut-mapping dial, on the images that need it most.

Every setting tested so far lives downstream of the gamut problem: dither
algorithm, palette LUT, tone curve, sharpening. Across six arms, forty images and
three judging formats the spread between best and worst was inside noise, a third
of comparisons were invisible, and four photographs had no acceptable rendering
at all — all four at 63-75 % out of gamut against a library median of 55 %.

That is the binding constraint, and none of those knobs touch it. This tests the
one that does. `gamut_clamp.py --blend` decides what an unreachable colour gives
up: at 0 it keeps the source's lightness and lets chroma go pale; at 1 it keeps
the chroma and lets the picture go dark. Both extremes have been measured on
glass and both were wrong in opposite directions, which is why it is a dial and
why a person has to look at it.

Two things to be clear about. These arms *replace* the shipped correction LUT
with the campaign's cusp mapping rather than blending with it, so an arm differs
from the baseline by the whole correction, not only by its gamut intent. And the
LUT is a property of the Display, not of ImageConfig — so each arm renders
through a registered display variant, which is also why each needs its own
model_id: the LUT cache memoises on that id and a shared one would silently hand
every arm the same LUT.

    python tools/gamut_plan.py --images 20 --out build/camcal/gamut_plan.json

Arms and image ranking are overridable, for follow-ups that test one LUT against
the shipped one on a different slice of the library. An arm with an empty path
renders through the shipped LUT; giving it a name other than ``baseline`` makes
it a fresh capture in the same sitting rather than reusing an older one:

    python tools/gamut_plan.py --arm ref= --arm red_100=build/camcal/lut_red_100.npy \\
        --rank-by hue000_frac --images 10 --include-plan build/camcal/gamut_plan.json \\
        --out build/camcal/red_plan.json
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

import production
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku_server import DEFAULT_BASE, image_config

# blend 0 keeps lightness and lets colour go pale; blend 1 keeps colour and lets
# the picture go dark. None is the shipped correction LUT, unchanged.
ARMS: dict[str, str | None] = {
    "baseline": None,
    "gamut_00": "build/camcal/lut_b0.0.npy",
    "gamut_25": "build/camcal/lut_b0.25.npy",
    "gamut_50": "build/camcal/lut_b0.5.npy",
    "gamut_100": "build/camcal/lut_b1.0.npy",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--features", type=Path, default=Path("build/camcal/library_features.csv"))
    ap.add_argument("--imagedir", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--images", type=int, default=20)
    ap.add_argument(
        "--must",
        nargs="*",
        default=[
            "DSC03405.JPEG",
            "DSC03986.JPG",
            "IMG_0389(5).JPG",
            "IMG_3775 (1).JPEG",
        ],
        help="always include these — by default the four with no acceptable render",
    )
    ap.add_argument(
        "--arm",
        action="append",
        default=None,
        metavar="NAME=LUT[|knob=value;...]",
        help="replace the default arms; an empty LUT means the shipped one. Config "
        "changes after '|' are applied with config_space.set_one; values are JSON "
        '(true, 1.0, "oklab") or bare strings',
    )
    ap.add_argument("--rank-by", default="oog_frac", help="library feature to pick images by")
    ap.add_argument(
        "--include-plan",
        type=Path,
        action="append",
        default=[],
        help="also take every image from this plan, so earlier captures line up",
    )
    ap.add_argument(
        "--production",
        action="store_true",
        help="start each photo from production's own decision (tools/production.py): "
        "its preset, face keep-out boxes and crop threshold, from the live config",
    )
    ap.add_argument("--server", default=DEFAULT_BASE)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    arms: dict[str, str | None] = ARMS
    changes: dict[str, list[tuple[str, object]]] = {}
    entry_fields: dict[str, dict] = {}
    if args.arm:
        arms = {}
        for spec in args.arm:
            name, _, rest = spec.partition("=")
            lut, _, knobs = rest.partition("|")
            arms[name] = lut or None
            changes[name] = []
            entry_fields[name] = {}
            for item in filter(None, knobs.split(";")):
                knob, _, raw = item.partition("=")
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    value = raw
                knob = knob.strip()
                # "@field" is a plan-entry field the capture tool reads, not an
                # ImageConfig knob — e.g. @autocontrast=preserve_tone.
                if knob.startswith("@"):
                    entry_fields[name][knob[1:]] = value
                else:
                    changes[name].append((knob, value))
    for arm, lut in arms.items():
        if lut and not Path(lut).exists():
            print(f"  missing LUT for {arm}: {lut}")
            return 1

    if args.include_plan:
        # Same config as the plan being extended, or its captures and these
        # would differ by more than the LUT under test. The live server may have
        # been retuned since, or be down and fall back to the repo preset.
        from hokku.webserver.image_config import (  # noqa: PLC0415
            image_config_from_dict_strict,
        )

        first = json.loads(args.include_plan[0].read_text(encoding="utf-8"))
        blob = dict(first["baseline_config"])
        # Plans predating `prepare_autocontrast` rendered with PIL's per-channel
        # stretch; replaying one has to say so explicitly now the field exists.
        blob.setdefault("prepare_autocontrast", "per_channel")
        base = image_config_from_dict_strict(blob)
        print(f"  baseline: config of {args.include_plan[0]}")
    else:
        try:
            base = image_config(base=args.server)
            print("  baseline: live server config")
        except Exception:
            base = PRESET_IMAGE_CONFIGS["default_general"]
            print("  baseline: repo default_general (server unreachable)")

    # By default sorted by how much of the picture the panel cannot reach: this
    # dial only does anything where there is out-of-gamut colour to map.
    # Greyscale photographs are never picked by ranking — a LUT that moves only
    # colour cannot change them, so they would cost panel time and show nothing.
    features = pd.read_csv(args.features)
    rows: list[tuple[float, str]] = [
        (value, name)
        for value, name, is_bw in zip(
            features[args.rank_by].astype(float).tolist(),
            features["name"].astype(str).tolist(),
            features["is_bw"].astype(bool).tolist(),
            strict=True,
        )
        if not is_bw and (args.imagedir / name).exists()
    ]
    rows.sort(reverse=True)
    included: list[str] = []
    for plan_path in args.include_plan:
        earlier = json.loads(plan_path.read_text(encoding="utf-8"))
        included += [n for n in earlier["images"] if n not in included]

    if included:
        # Ranked images are *additional* to the carried-over set.
        picked = included + [n for _v, n in rows if n not in included][: args.images]
    else:
        picked = [n for _v, n in rows[: args.images]]
        for name in args.must:
            if name not in picked and any(name == n for _v, n in rows):
                picked.insert(0, name)
        picked = picked[: max(args.images, len(args.must))]

    value = {n: v for v, n in rows}
    ranked = [value[n] for n in picked if n in value]
    print(
        f"  {len(picked)} images ({len(included)} carried over), {args.rank_by} "
        f"{min(ranked):.2f}-{max(ranked):.2f}"
    )

    import config_space  # noqa: PLC0415 — only arms with config changes need it

    configs = {}
    for arm in arms:
        cfg = base
        for knob, value in changes.get(arm, []):
            cfg = config_space.set_one(cfg, knob, value)
        configs[arm] = cfg
        if changes.get(arm):
            print(f"    {arm}: {config_space.describe(cfg, base)}")

    decisions: dict = {}
    app = None
    if args.production:
        app = production.live_app_config()
        clf = production.classifier(app)
        decisions = {n: production.decision_for(clf, args.imagedir / n) for n in picked}
        presets = [production.preset_of(app, d) for d in decisions.values()]
        print(
            "  production decisions: "
            + ", ".join(f"{p} {presets.count(p)}" for p in sorted(set(presets)))
            + f"; crop-to-fill threshold {app.crop_to_fill_threshold}"
        )

    candidates = []
    for name in picked:
        for arm, lut in arms.items():
            extra: dict = {}
            cfg = configs[arm]
            if app is not None:
                cfg = decisions[name].image_config
                for knob, value in changes.get(arm, []):
                    cfg = config_space.set_one(cfg, knob, value)
                extra = production.plan_fields(app, decisions[name])
            candidates.append(
                {
                    "tag": production.plan_tag(name, arm),
                    "image": str(args.imagedir / name),
                    "image_name": name,
                    "config_tag": arm,
                    "config": asdict(cfg),
                    "lut": lut,
                    **extra,
                    **entry_fields.get(arm, {}),
                }
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "model": args.model,
                "baseline_config": asdict(base),
                "arms": {
                    a: {
                        "lut": arms[a],
                        "changes": changes.get(a, []),
                        "fields": entry_fields.get(a, {}),
                    }
                    for a in arms
                },
                "images": picked,
                "candidates": candidates,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"  {len(candidates)} cells ({len(arms)} arms) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
