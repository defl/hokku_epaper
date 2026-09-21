#!/usr/bin/env python3
"""Turn per-image search results into a capture plan: the live config, and tuned.

`param_search.py` optimises each photograph offline against an objective fitted
to real verdicts. This builds the plan that puts those versions on the glass so
they can be judged the way every other change in this campaign was — blind, side
by side, on the panel rather than on a monitor.

**One arm per search result.** `--arm NAME=results.json`, repeated. The `live`
arm is always included and is production's own decision for that photograph, so
every arm differs from it only by the searched knobs.

**Why a ladder is worth the extra arms.** Two arms resolve best, and that is the
right shape for testing one change: the trust region was worth +0.71 rating
points per photograph measured exactly that way. But the open question now is not
*whether* to constrain the search — it is *how far past the judged evidence is
safe*. Nothing was ever rated out there, so the bands in that region rest on
absence rather than verdicts, and the fence is demonstrably too tight for the
photographs whose defect is severe ("Both are worse than previous, orange is red
again"). A ladder of relaxation levels, judged on one page, is what replaces the
absence with data.

**The live config is the baseline, not the repo preset.** The Foyer's stored
config has drifted from `presets.py` (CLAHE 1.75 against the repo's 0.0), and the
point is to beat what is on the wall, not what is in the tree.

    python tools/search_plan.py --arm r0=build/camcal/per_image_best_trust.json \\
        --arm r15=build/camcal/per_image_relax015.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import production
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_config import image_config_from_dict_strict


def load_arm(spec: str) -> tuple[str, dict[str, dict]]:
    """``NAME=path`` -> (name, {image: result row}), refusing an unfitted search."""
    name, _, path = spec.partition("=")
    blob = json.loads(Path(path).read_text(encoding="utf-8"))
    if not blob.get("objective_fitted"):
        raise SystemExit(f"REFUSING {name}: that search ran on the provisional objective")
    moved = [r for r in blob["results"] if r["changed"] != "(baseline)"]
    print(
        f"  {name:6s} relax {blob.get('trust_relax', 0.0):.0%}, "
        f"moved {len(moved)} of {len(blob['results'])}"
    )
    return name, {r["image"]: r for r in moved}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", action="append", default=[], metavar="NAME=RESULTS.json")
    ap.add_argument("--search", type=Path, default=None, help="shorthand for --arm tuned=FILE")
    ap.add_argument("--imagedir", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--out", type=Path, default=Path("build/camcal/search_plan.json"))
    ap.add_argument("--images", type=int, default=0, help="cap the number of photographs")
    ap.add_argument(
        "--only",
        type=Path,
        default=None,
        help="file of image names — use exactly these, so a re-run is comparable "
        "with the round before it on the same photographs",
    )
    ap.add_argument(
        "--require-distinct",
        action="store_true",
        help="skip photographs whose tuned arms all chose the same config: a "
        "ladder whose rungs coincide costs panel time and resolves nothing",
    )
    args = ap.parse_args(argv)

    specs = [*args.arm, *([f"tuned={args.search}"] if args.search else [])]
    if not specs:
        raise SystemExit("nothing to build: pass --arm NAME=results.json")
    arms = dict(load_arm(spec) for spec in specs)

    names = sorted({name for rows in arms.values() for name in rows})
    if args.only:
        wanted = {
            line.strip()
            for line in args.only.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        missing = sorted(wanted - set(names))
        if missing:
            print(f"  {len(missing)} photograph(s) no arm moved: {missing}")
        names = [n for n in names if n in wanted]

    if args.require_distinct and len(arms) > 1:

        def rungs(name: str) -> int:
            return len(
                {
                    json.dumps(rows[name]["config"], sort_keys=True)
                    for rows in arms.values()
                    if name in rows
                }
            )

        distinct = [n for n in names if rungs(n) > 1]
        if len(distinct) < len(names):
            print(f"  {len(names) - len(distinct)} photograph(s) dropped: the rungs coincide")
        names = distinct

    names.sort(key=lambda n: -max(rows[n]["gain"] for rows in arms.values() if n in rows))
    if args.images:
        names = names[: args.images]

    # Production's own decision per photograph: preset, face keep-out boxes and
    # the crop threshold. Every arm shares them, so only the searched knobs vary.
    app = production.live_app_config()
    clf = production.classifier(app)

    candidates, used = [], []
    for name in names:
        path = args.imagedir / name
        decision = production.decision_for(clf, path)
        extra = production.plan_fields(app, decision)
        configs = {"live": decision.image_config}
        for arm, rows in arms.items():
            if name in rows:
                configs[arm] = image_config_from_dict_strict(rows[name]["config"])
        for arm, cfg in configs.items():
            candidates.append(
                {
                    "tag": production.plan_tag(name, arm),
                    "image": str(path),
                    "image_name": name,
                    "config_tag": arm,
                    "config": asdict(cfg),
                    "lut": None,
                    **extra,
                }
            )
        used.append(name)
        gains = "  ".join(
            f"{arm} {rows[name]['gain']:+.2f}" for arm, rows in arms.items() if name in rows
        )
        print(f"    {name[:32]:32s} {gains}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "model": args.model,
                "arms": {arm: {"lut": None} for arm in ["live", *arms]},
                "images": used,
                "candidates": candidates,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n  wrote {args.out}: {len(candidates)} captures over {len(used)} photographs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
