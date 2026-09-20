#!/usr/bin/env python3
"""Turn a per-image search result into a capture plan: live config against tuned.

`param_search.py` optimises each photograph offline, against an objective fitted
to real verdicts. This builds the plan that puts both versions on the glass so
the result can be judged the way every other change in this campaign was — blind,
side by side, on the panel rather than on a monitor.

**Two arms, not five.** The ratings are reproducible to +-0.55 points, so of the
spread between two versions only about a third is real and the rest is judging
noise. The way to get a usable answer out of that is fewer comparisons with
bigger differences in them, not more arms: a live-against-tuned pair is the
largest difference the search can offer, and every capture spends its noise
budget on that one question.

**The live config is the baseline, not the repo preset.** The Foyer's stored
config has drifted from `presets.py` (CLAHE 1.75 against the repo's 0.0), and the
point is to beat what is on the wall, not what is in the tree.

    python tools/search_plan.py --search build/camcal/per_image_best.json
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--search", type=Path, default=Path("build/camcal/per_image_best.json"))
    ap.add_argument("--imagedir", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--out", type=Path, default=Path("build/camcal/search_plan.json"))
    ap.add_argument(
        "--min-gain",
        type=float,
        default=0.0,
        help="skip photographs the search could not improve by at least this much",
    )
    ap.add_argument("--images", type=int, default=0, help="cap the number of photographs")
    ap.add_argument(
        "--only",
        type=Path,
        default=None,
        help="file of image names — use exactly these, so a re-run is comparable "
        "with the round before it on the same photographs",
    )
    args = ap.parse_args(argv)

    blob = json.loads(args.search.read_text(encoding="utf-8"))
    if not blob.get("objective_fitted"):
        print("REFUSING: that search ran on the provisional objective, not a fitted one")
        return 1
    print(f"  {blob['objective_note']}")

    results = [r for r in blob["results"] if r["gain"] >= args.min_gain]
    results = [r for r in results if r["changed"] != "(baseline)"]
    if args.only:
        wanted = {
            line.strip()
            for line in args.only.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        kept = [r for r in results if r["image"] in wanted]
        missing = sorted(wanted - {r["image"] for r in kept})
        if missing:
            print(f"  {len(missing)} of those photographs did not move this time: {missing}")
        results = kept
    results.sort(key=lambda r: -r["gain"])
    if args.images:
        results = results[: args.images]
    print(f"  {len(results)} photograph(s) the search moved off the live config")

    # Production's own decision per photograph: which preset, the face keep-out
    # boxes and the crop threshold. The tuned arm differs from the live one only
    # by the searched knobs, so anything else that reaches the panel is shared.
    app = production.live_app_config()
    clf = production.classifier(app)

    candidates = []
    for row in results:
        name = row["image"]
        path = args.imagedir / name
        decision = production.decision_for(clf, path)
        extra = production.plan_fields(app, decision)
        tuned = image_config_from_dict_strict(row["config"])
        for arm, cfg in (("live", decision.image_config), ("tuned", tuned)):
            candidates.append(
                {
                    "tag": f"{Path(name).stem[:22]}__{arm}",
                    "image": str(path),
                    "image_name": name,
                    "config_tag": arm,
                    "config": asdict(cfg),
                    "lut": None,
                    **extra,
                }
            )
        print(f"    {name[:34]:34s} gain {row['gain']:+7.3f}  {row['changed']}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "model": args.model,
                "baseline_config": blob["baseline_config"],
                "arms": {"live": {"lut": None}, "tuned": {"lut": None}},
                "images": [r["image"] for r in results],
                "objective_note": blob["objective_note"],
                "candidates": candidates,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n  wrote {args.out}: {len(candidates)} captures ({len(results)} photographs x 2 arms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
