#!/usr/bin/env python3
"""Split each rendering's difference from the baseline into colour and texture.

`dither.lut_name` and `dither.algorithm` are separate fields and were tested as
separate arms, so nothing is confounded in the *config*. But a palette-LUT change
has two visible consequences at once: it changes which ink represents a colour,
and the quantisation error it leaves behind is what error diffusion spreads, so
the spatial grain moves too. A verdict of "worse" cannot say which one did it.

Both are measurable on the frames that were actually rated:

**Colour** is the Yule-Nielsen block prediction at 8 px — what an eye integrating
over a block sees — as dE00 against the baseline render of the same photograph.

**Texture** is what survives removing that: the change in high-frequency energy
and in error roughness, plus the share of pixels whose ink simply differs. Two
renders can agree on every block and still look entirely different up close.

Then the question the ratings can answer: across photographs, does the verdict
track the colour change or the texture change?

    python tools/camcal/decompose_arms.py --session build/camcal/session6
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, "tools")
sys.path.insert(0, "python")

from cam_compare import block_mean, delta_e00, model_lab
from hokku.screens.registry import DISPLAY_REGISTRY
from render_bank import load_model

BLOCK = 8


def ink_raster(path: Path, palette: np.ndarray) -> np.ndarray | None:
    """The stored frame back as palette indices; None if it is not such a frame."""
    bgr = cv2.imread(str(path))
    if bgr is None:
        return None
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    idx = np.zeros(rgb.shape[:2], np.uint8)
    seen = np.zeros(rgb.shape[:2], bool)
    for i, ink in enumerate(palette):
        hit = np.all(rgb == ink, axis=-1)
        idx[hit] = i
        seen |= hit
    return idx if seen.all() else None


def highfreq(idx: np.ndarray) -> float:
    """Energy left after block-averaging: the dither's own grain."""
    a = idx.astype(np.float32)
    coarse = np.repeat(np.repeat(block_mean(a[..., None], BLOCK)[..., 0], BLOCK, 0), BLOCK, 1)
    h, w = coarse.shape
    return float(np.abs(a[:h, :w] - coarse).mean())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", type=Path, default=Path("build/camcal/session6"))
    ap.add_argument("--pages", nargs="*", default=["page1", "page2"])
    ap.add_argument("--ref", default="live")
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY["huessen_epf1301"]
    model = load_model()
    palette = np.rint(display.palette_measured_rgb).clip(0, 255).astype(np.uint8)

    # Ratings, paired within a photograph, from the archived exports.
    ratings: dict[tuple[str, str], int] = {}
    tags: dict[tuple[str, str], str] = {}
    for page in args.pages:
        key = json.loads((args.session / f"{page}_key.json").read_text(encoding="utf-8"))
        export = args.session / f"{page}_export.json"
        if not export.exists():
            continue
        got = json.loads(export.read_text(encoding="utf-8"))
        for img in key["images"]:
            rec = got.get("ratings", {}).get(img["image"], {})
            for slot in img["slots"]:
                value = rec.get("r", {}).get(str(slot["slot"]))
                tags[img["image"], slot["arm"]] = slot["tag"]
                if value is not None:
                    ratings[img["image"], slot["arm"]] = int(value)

    images = sorted({i for i, _ in tags})
    rows = []
    for n, image in enumerate(images, 1):
        ref_tag = tags.get((image, args.ref))
        if ref_tag is None:
            continue
        ref_idx = ink_raster(args.session / f"{ref_tag}__expected.png", palette)
        if ref_idx is None:
            continue
        ref_lab = model_lab(ref_idx, model["prim_mat"], model["n"], BLOCK)
        ref_hf = highfreq(ref_idx)
        for (img, arm), tag in tags.items():
            if img != image or arm == args.ref:
                continue
            idx = ink_raster(args.session / f"{tag}__expected.png", palette)
            if idx is None:
                continue
            lab = model_lab(idx, model["prim_mat"], model["n"], BLOCK)
            rows.append(
                {
                    "image": image,
                    "arm": arm,
                    "colour_dE": float(delta_e00(ref_lab, lab).mean()),
                    "texture_d": abs(highfreq(idx) - ref_hf),
                    "ink_changed": float((idx != ref_idx).mean()),
                    "rating_delta": (
                        ratings[img, arm] - ratings[img, args.ref]
                        if (img, arm) in ratings and (img, args.ref) in ratings
                        else np.nan
                    ),
                }
            )
        if n % 10 == 0:
            print(f"  {n}/{len(images)} photographs", flush=True)

    df = pd.DataFrame(rows)
    print(f"\n{len(df)} arm-photograph pairs measured\n")

    # Aggregated into plain dicts rather than a chained groupby: the chained
    # result is opaque to the type checker and this is four means per arm.
    def per_arm(column: str) -> dict[str, float]:
        out = {}
        for arm in sorted(set(df["arm"])):
            values = df.loc[df["arm"] == arm, column].to_numpy(dtype=float)
            values = values[np.isfinite(values)]
            out[arm] = float(values.mean()) if values.size else float("nan")
        return out

    colour = per_arm("colour_dE")
    texture = per_arm("texture_d")
    churn = per_arm("ink_changed")
    verdict = per_arm("rating_delta")
    print("how each rendering differs from the baseline, and how it was judged:")
    print(f"  {'arm':14s} {'colour dE':>10s} {'texture':>8s} {'ink chg':>8s} {'rating':>7s}")
    for arm in sorted(colour, key=lambda a: colour[a]):
        print(
            f"  {arm:14s} {colour[arm]:10.2f} {texture[arm]:8.3f} "
            f"{churn[arm]:7.0%} {verdict[arm]:+7.2f}"
        )

    def rho(frame, column: str) -> float:
        """Spearman rho as a plain float.

        Takes arrays rather than the frame's own accessors, and converts the
        result explicitly: both the sliced frame's type and spearmanr's result
        object are opaque to the type checker, and neither is worth a cast.
        """
        result = spearmanr(
            np.asarray(frame[column], dtype=float),
            np.asarray(frame["rating_delta"], dtype=float),
        )
        return float(np.asarray(result)[0])

    judged = df.dropna(subset=["rating_delta"])
    print(f"\nwhat predicts the verdict, over {len(judged)} judged pairs:")
    for col in ("colour_dE", "texture_d", "ink_changed"):
        print(f"  rating delta vs {col:12s} rho {rho(judged, col):+.3f}")

    print("\nsame question, restricted to the palette-LUT arms only:")
    lut_arms = judged[judged["arm"].isin(["oklab", "weighted", "cam16"])]
    if len(lut_arms) >= 10:
        for col in ("colour_dE", "texture_d", "ink_changed"):
            value = rho(lut_arms, col)
            print(f"  rating delta vs {col:12s} rho {value:+.3f}   (n={len(lut_arms)})")

    df.to_csv(args.session / "arm_decomposition.csv", index=False)
    print(f"\nwrote {args.session / 'arm_decomposition.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
