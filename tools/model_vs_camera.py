#!/usr/bin/env python3
"""Check the offline panel model against photographs of the real glass.

Everything downstream of the metric bank rests on one assumption: that the
Yule-Nielsen prediction of a dithered raster is close enough to what the panel
actually does that a search can optimise against it without looking. That
assumption has only ever been spot-checked on a handful of frames.

A judging session produces the material to check it properly — every captured
candidate is a photograph of a raster whose prediction is already known. This
compares the two across all of them.

What it can and cannot say: the camera itself carries a leave-one-out error of
4.35 dE (dL* 0.90, dC* 5.39, dh 11.9 deg), so agreement much tighter than that
cannot be resolved and disagreement below it is not evidence of anything. What
matters for a *search* is weaker than absolute agreement anyway — the model has
to **rank** candidates the way the panel does. So the headline number here is the
correlation of predicted against measured differences, not their absolute gap.

    python tools/model_vs_camera.py --session build/camcal/session1 \\
        --plan build/camcal/session16_plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from scipy.stats import rankdata

from cam_compare import block_mean, delta_e00, model_lab
from color_validate_photos import lab_img, srgb_img_to_xyz
from hokku.screens.registry import DISPLAY_REGISTRY
from render_bank import BLOCK, load_model


def _spearman(a: list[float], b: list[float]) -> float:
    """Rank correlation. Pearson on ranks, with scipy's tie-averaging."""
    return float(np.corrcoef(rankdata(a), rankdata(b))[0, 1])


def indices_from_expected(path: Path, display) -> np.ndarray | None:
    """Recover the ink raster from the saved `__expected.png`.

    That image is the palette painted with `palette_measured_rgb`, and those six
    colours are distinct, so the mapping inverts exactly. Cheaper and safer than
    re-rendering, which would have to reproduce the same dither noise draw.
    """
    img = cv2.imread(str(path))
    if img is None:
        return None
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.int16)
    palette = np.rint(display.palette_measured_rgb).astype(np.int16)
    dist = np.abs(rgb.reshape(-1, 1, 3) - palette[None, :, :]).sum(axis=2)
    if float(np.mean(dist.min(axis=1) == 0)) < 0.99:
        return None
    return np.argmin(dist, axis=1).astype(np.uint8).reshape(rgb.shape[:2])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--session", type=Path, default=Path("build/camcal/session1"))
    ap.add_argument("--plan", type=Path, default=Path("build/camcal/session16_plan.json"))
    ap.add_argument("--out", type=Path, default=Path("build/camcal/model_vs_camera.json"))
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    model = load_model(args.model)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))

    rows = []
    for entry in plan["candidates"]:
        tag = entry["tag"]
        measured_path = args.session / f"{tag}__measured.png"
        expected_path = args.session / f"{tag}__expected.png"
        if not (measured_path.exists() and expected_path.exists()):
            continue
        idx = indices_from_expected(expected_path, display)
        photo = cv2.imread(str(measured_path))
        if idx is None or photo is None:
            continue

        predicted = model_lab(idx, model["prim_mat"], model["n"], BLOCK)
        measured = lab_img(
            block_mean(srgb_img_to_xyz(cv2.cvtColor(photo, cv2.COLOR_BGR2RGB)), BLOCK)
        )
        if predicted.shape != measured.shape:
            continue
        # The rectified photograph has dark borders where the camera did not
        # cover the panel; they are not measurements of anything.
        keep = measured[..., 0] > 2.0
        if keep.sum() < 1000:
            continue
        pc = np.hypot(predicted[..., 1], predicted[..., 2])
        mc = np.hypot(measured[..., 1], measured[..., 2])
        rows.append(
            {
                "tag": tag,
                "image": entry["image_name"],
                "config": entry["config_tag"],
                "de00": float(delta_e00(predicted, measured)[keep].mean()),
                "dL": float((measured[..., 0] - predicted[..., 0])[keep].mean()),
                "dC": float((mc - pc)[keep].mean()),
                "pred_L": float(predicted[..., 0][keep].mean()),
                "meas_L": float(measured[..., 0][keep].mean()),
                "pred_C": float(pc[keep].mean()),
                "meas_C": float(mc[keep].mean()),
            }
        )

    if len(rows) < 8:
        print(f"  only {len(rows)} comparable captures — run the session first")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=1), encoding="utf-8")

    de = np.array([r["de00"] for r in rows])
    dl = np.array([r["dL"] for r in rows])
    dc = np.array([r["dC"] for r in rows])
    print(f"  {len(rows)} photographed candidates over {len({r['image'] for r in rows})} images")
    print("\n  absolute agreement, model against camera (camera's own LOO error is 4.35 dE):")
    print(f"    dE00     mean {de.mean():6.2f}   p90 {np.percentile(de, 90):6.2f}")
    print(f"    dL*      mean {dl.mean():+6.2f}   sd {dl.std():5.2f}   (camera dL* rms 0.90)")
    print(f"    dC*      mean {dc.mean():+6.2f}   sd {dc.std():5.2f}   (camera dC* rms 5.39)")

    # The question a search actually cares about: within one image, does the
    # model order the candidates the way the panel does?
    print("\n  ranking agreement within each image (what a search depends on):")
    print(f"    {'image':40s} {'n':>3s} {'L* rho':>8s} {'C* rho':>8s}")
    lr, cr = [], []
    for name in sorted({r["image"] for r in rows}):
        sub = [r for r in rows if r["image"] == name]
        if len(sub) < 4:
            continue
        rho_l = _spearman([r["pred_L"] for r in sub], [r["meas_L"] for r in sub])
        rho_c = _spearman([r["pred_C"] for r in sub], [r["meas_C"] for r in sub])
        lr.append(rho_l)
        cr.append(rho_c)
        print(f"    {name[:40]:40s} {len(sub):3d} {rho_l:8.2f} {rho_c:8.2f}")
    if lr:
        print(f"\n    median Spearman rho: L* {np.median(lr):.2f}, C* {np.median(cr):.2f}")
        print(
            "    rho near 1 means the model ranks candidates as the panel does,\n"
            "    which is what an offline search needs — absolute agreement is a\n"
            "    stronger claim than the search actually requires."
        )
    print(f"\n  -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
