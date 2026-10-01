#!/usr/bin/env python3
"""Capture plan for the gamut-correction LUT axis: one ImageConfig, many LUTs.

Every arm here renders with the photograph's own production ImageConfig,
unchanged. The only thing that differs is the correction LUT, which is a property
of the Display rather than of the config — which is why the per-image search
could never reach it and why none of the sixteen renderings judged so far varied
it at all.

`live` carries no LUT override, so it renders through the deployed 33-step asset.
`b50_t85` is the same blend and trim rebuilt at 17 steps like every other arm
here: it is the control for LUT resolution, not a variant. If those two do not
rate alike, every other comparison on the page is confounded by resolution and
should be read that way.

    python tools/camcal/lut_plan.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, "tools")
sys.path.insert(0, "python")

import production

CAM = Path("build/camcal")
LUTS = CAM / "luts"

# name -> LUT file, or None for the deployed asset. Order is the page order.
ARMS: dict[str, str | None] = {
    "live": None,
    "b50_t85": "b50_t85.npy",
    "b00_t85": "b00_t85.npy",
    "b25_t85": "b25_t85.npy",
    "b100_t85": "b100_t85.npy",
    "b50_t70": "b50_t70.npy",
    "b50_t100": "b50_t100.npy",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--images", type=Path, default=CAM / "oog_images.txt")
    ap.add_argument("--imagedir", type=Path, default=CAM / "server_images")
    ap.add_argument("--out", type=Path, default=CAM / "lut_plan.json")
    args = ap.parse_args(argv)

    missing = [n for n, f in ARMS.items() if f and not (LUTS / f).exists()]
    if missing:
        print(f"ABORT: LUTs not built yet: {missing}")
        return 1

    names = [n for n in args.images.read_text(encoding="utf-8").splitlines() if n.strip()]
    app = production.live_app_config()
    clf = production.classifier(app)

    candidates, presets = [], Counter()
    for name in names:
        path = args.imagedir / name
        decision = production.decision_for(clf, path)
        presets[production.preset_of(app, decision)] += 1
        extra = production.plan_fields(app, decision)
        for arm, lut in ARMS.items():
            candidates.append(
                {
                    "tag": production.plan_tag(name, arm),
                    "image": str(path),
                    "image_name": name,
                    "config_tag": arm,
                    # Identical config on every arm — the LUT is the only variable.
                    "config": asdict(decision.image_config),
                    "lut": str(LUTS / lut) if lut else None,
                    **extra,
                }
            )
    print(f"{len(names)} photographs x {len(ARMS)} LUTs; presets {dict(presets)}")

    tags = {c["tag"] for c in candidates}
    if len(tags) != len(candidates):
        raise SystemExit(f"tag collision: {len(candidates)} captures, {len(tags)} tags")

    args.out.write_text(
        json.dumps(
            {
                "model": "huessen_epf1301",
                "arms": {a: {"lut": str(LUTS / f) if f else None} for a, f in ARMS.items()},
                "images": names,
                "candidates": candidates,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"wrote {args.out}: {len(candidates)} captures, ~{len(candidates) * 34 / 3600:.1f} h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
