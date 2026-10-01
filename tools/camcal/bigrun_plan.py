"""Build the big capture plan: 100 photographs, 16 renderings, 8 shown per photograph.

**Why 8 per photograph and not 16.** A capture is ~34 s of e-paper refresh, so
ten hours buys about a thousand frames. 100 photographs x 16 renderings is 1600
frames — roughly fifteen hours, which does not fit. 100 x 8 is 800 frames and
about seven and a half hours, which does. So all 16 renderings are judged, but
each photograph shows 8 of them: the live config on every photograph as the
common anchor, and 7 of the other 15 rotating.

**Balanced by usage, not by a fixed window.** Taking 7 consecutive arms from a
cycle of 15 balances how often each arm appears but guarantees that arms more
than six apart in the cycle never meet on the same photograph, so those pairs are
never compared. Instead each photograph takes the 7 least-used arms so far, ties
broken by a seeded shuffle: usage stays even and every pair co-occurs somewhere.

**Every arm is defined against the photograph's own production config**, not
against one global one. Production gives 204 of the 245 library photographs the
face preset, whose saturation stage differs from the default's — `color_enhance`
is live there and ignored on the default, `adaptive_vivid` the other way round.
An arm defined against the wrong base is simply inert on most of the library.

    python build/camcal/analysis/bigrun_plan.py
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, "tools")
sys.path.insert(0, "python")

import config_space
import production

CAM = Path("build/camcal")

# Chosen from build/camcal/analysis/screen_arms.py: every one renders at least
# ~1.2 dE00 from live on a typical photograph (ratings resolve ~1.5, the dither's
# own noise is ~0.7), and between them they span the themes the rating notes
# name — saturation in both directions, local contrast, palette mapping, tone.
# Deliberately dropped: `vivid`, `satmax`, `sat_off`, `sat_cielab`, `nchroma_*`,
# `huegate_wide`, `sharper`, `softer` (all under 1.0, most inert on the face
# preset), and `clean`, which measured identically to `clahe_off` because its
# other two knobs do nothing when saturation is already off.
ARMS: dict[str, list[tuple[str, object]]] = {
    "calm": [
        ("clahe_clip_limit", 1.0),
        ("dither.lut_name", "oklab_hue_aware"),
        ("color_enhance", 0.95),
    ],
    "oklab": [("dither.lut_name", "oklab_hue_aware")],
    "stucki": [("dither.algorithm", "stucki")],
    "clahe_off": [("clahe_clip_limit", 0.0)],
    "cam16": [("dither.lut_name", "cam16ucs_hue_aware")],
    "rich": [
        ("clahe_clip_limit", 1.0),
        ("adaptive_vivid", True),
        ("dither.lut_name", "hue_aware_weighted"),
    ],
    "gamma_hi": [("prepare_gamma", 1.10)],
    "weighted": [("dither.lut_name", "hue_aware_weighted")],
    "scale_chroma": [("scale_chroma", True)],
    "clahe_low": [("clahe_clip_limit", 1.0)],
    "saturate": [("adaptive_saturate_space", "off"), ("color_enhance", 1.20)],
    "contrast_up": [("prepare_contrast", 1.25)],
    "contrast_dn": [("prepare_contrast", 0.95)],
    "desat": [("adaptive_saturate_space", "off"), ("color_enhance", 0.85)],
    "darker": [("prepare_brightness", 0.95)],
}
ANCHOR = "live"


def assign(names: list[str], per_image: int, seed: int) -> dict[str, list[str]]:
    """Give each photograph the least-used arms so far, ties broken at random."""
    rng = random.Random(seed)  # noqa: S311 — shuffling arms, not keying anything
    used: Counter[str] = Counter(dict.fromkeys(ARMS, 0))
    out: dict[str, list[str]] = {}
    for name in names:
        pool = list(ARMS)
        rng.shuffle(pool)
        chosen = sorted(pool, key=lambda a: used[a])[: per_image - 1]
        for arm in chosen:
            used[arm] += 1
        out[name] = [ANCHOR, *sorted(chosen)]
    print("arm usage:", dict(sorted(used.items(), key=lambda kv: -kv[1])))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--images", type=Path, default=CAM / "selected_images.txt")
    ap.add_argument("--imagedir", type=Path, default=CAM / "server_images")
    ap.add_argument("--per-image", type=int, default=8)
    ap.add_argument("--seed", type=int, default=20260923)
    ap.add_argument("--out", type=Path, default=CAM / "bigrun_plan.json")
    args = ap.parse_args(argv)

    names = [n for n in args.images.read_text(encoding="utf-8").splitlines() if n.strip()]
    print(f"{len(names)} photographs, {len(ARMS) + 1} renderings, {args.per_image} per photograph")

    app = production.live_app_config()
    clf = production.classifier(app)

    layout = assign(names, args.per_image, args.seed)
    candidates, presets = [], Counter()
    for name in names:
        path = args.imagedir / name
        decision = production.decision_for(clf, path)
        presets[production.preset_of(app, decision)] += 1
        extra = production.plan_fields(app, decision)
        for arm in layout[name]:
            cfg = decision.image_config
            for knob, value in ARMS.get(arm, []):
                cfg = config_space.set_one(cfg, knob, value)
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
    print("production presets:", dict(presets))

    tags = {c["tag"] for c in candidates}
    if len(tags) != len(candidates):
        raise SystemExit(f"tag collision: {len(candidates)} captures but {len(tags)} tags")

    args.out.write_text(
        json.dumps(
            {
                "model": "huessen_epf1301",
                "arms": {a: {"lut": None} for a in [ANCHOR, *ARMS]},
                "images": names,
                "layout": layout,
                "candidates": candidates,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    hours = len(candidates) * 34 / 3600
    print(f"\nwrote {args.out}: {len(candidates)} captures, ~{hours:.1f} h of panel time")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
