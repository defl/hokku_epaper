#!/usr/bin/env python3
"""Describe every image in the real library, as the panel will actually see it.

The current renderer picks one of three presets from a two-question classifier:
is it near-greyscale, and does it contain a face. That is the whole model. It has
no way to say "this one is 70 % out of gamut in the reds" or "this one is flat
and starved of local contrast", so images needing different treatment get the
same treatment.

This is the input side of a replacement. One row per image, measuring the things
that plausibly determine what treatment an image wants — including the two the
old classifier asked, so whatever is fitted on top can rediscover the 3-way split
if the 3-way split was right, and depart from it where it was not.

Two decisions worth stating:

**Features are measured on the render canvas, not the original file.** Every
image is put through the pipeline's own crop-and-fit geometry first
(``cam_compare.source_canvas``), so a feature describes the pixels that will
reach the glass. A portrait cropped to landscape can lose the face the file was
named for, and a feature computed on the file would not know.

**Out-of-gamut is split by hue, not just totalled.** A picture 60 % out of gamut
in the blues and one 60 % out in the reds are different problems with different
answers — error diffusion has plenty of blue ink and very little red — and a
single scalar cannot tell them apart. Twelve 30-degree sectors, measured against
the campaign's own reachable-chroma ceiling.

    python tools/library_features.py --out build/camcal/library_features.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import ndimage

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from cam_compare import source_canvas
from color_validate_photos import lab_img, srgb_img_to_xyz
from gamut_clamp import chroma_ceiling, lookup_ceiling, reachable_lab
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_classifier import ImageClassifier
from hokku_server import DEFAULT_BASE, fetch_original, pool
from render_bank import PANEL_BLACK_L, PANEL_WHITE_L, campaign_path

HUE_SECTORS = 12
SECTOR_DEG = 360 // HUE_SECTORS

_CEILING: np.ndarray | None = None
_DETECTOR = None


def ceiling_for(model: str) -> np.ndarray:
    global _CEILING
    if _CEILING is None:
        from color_model import load_records  # noqa: PLC0415 — worker-local import

        _CEILING = chroma_ceiling(reachable_lab(load_records(campaign_path(model))))
    return _CEILING


def detector():
    """The production face detector, loaded lazily — the graph costs ~57 MB."""
    global _DETECTOR
    if _DETECTOR is None:
        from hokku.webserver.face_detect_yunet_opencv import (  # noqa: PLC0415
            OpenCVYuNetFaceDetector,
        )

        _DETECTOR = OpenCVYuNetFaceDetector()
    return _DETECTOR


def features_for(path: Path, model: str = "huessen_epf1301") -> dict:
    """One row: everything measurable about how hard this image will be."""
    display = DISPLAY_REGISTRY[model]
    canvas = source_canvas(path, display, "x")
    lab = lab_img(srgb_img_to_xyz(canvas))
    lightness, chroma = lab[..., 0], np.hypot(lab[..., 1], lab[..., 2])
    hue = np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) % 360.0
    ceiling = lookup_ceiling(ceiling_for(model), lab)
    oog = chroma > ceiling
    chromatic = chroma > 8.0

    row: dict[str, float | str | bool] = {"name": path.name}

    # The two questions the old classifier asked, answered exactly as the server
    # answers them, so a fitted model can reproduce or overrule the 3-way split.
    row["is_bw"] = bool(ImageClassifier._check_grayscale(path))
    faces = detector().detect(path) or ()
    row["n_faces"] = float(len(faces))
    row["face_area"] = float(sum(f.w * f.h for f in faces)) if faces else 0.0

    row["mean_l"] = float(lightness.mean())
    row["std_l"] = float(lightness.std())
    row["p05_l"] = float(np.percentile(lightness, 5))
    row["p95_l"] = float(np.percentile(lightness, 95))
    row["range_l"] = row["p95_l"] - row["p05_l"]
    row["mean_c"] = float(chroma.mean())
    row["p90_c"] = float(np.percentile(chroma, 90))
    # The statistic the production B&W test thresholds at 8.0, kept continuous so
    # a model can find its own cutoff instead of inheriting that one.
    row["p95_c"] = float(np.percentile(chroma, 95))

    row["oog_frac"] = float(oog.mean())
    row["oog_excess"] = float((chroma - ceiling)[oog].mean()) if oog.any() else 0.0
    row["headroom"] = float((ceiling - chroma)[~oog].mean()) if (~oog).any() else 0.0

    for s in range(HUE_SECTORS):
        lo, hi = s * SECTOR_DEG, (s + 1) * SECTOR_DEG
        sel = chromatic & (hue >= lo) & (hue < hi)
        row[f"hue{lo:03d}_frac"] = float(sel.mean())
        row[f"hue{lo:03d}_oog"] = float(oog[sel].mean()) if sel.any() else 0.0

    # Skin, by the same definition cam_compare and color_validate_photos use.
    # On a picture of a red wall this also fires — it is a warm-chromatic mask,
    # not a face detector, and n_faces above is what actually knows about faces.
    row["skin_frac"] = float((((hue > 320) | (hue < 70)) & (chroma > 12) & (lightness > 20)).mean())

    # Detail: how much local structure there is to lose. Sobel via scipy rather
    # than a hand-rolled difference, and normalised by the lightness range so a
    # flat dark picture is not called detailed merely for being noisy.
    grad = np.hypot(ndimage.sobel(lightness, 0), ndimage.sobel(lightness, 1))
    row["detail_energy"] = float(grad.mean())
    row["detail_p90"] = float(np.percentile(grad, 90))
    hist, _ = np.histogram(lightness, bins=64, range=(0, 100), density=True)
    hist = hist[hist > 0]
    row["entropy_l"] = float(-(hist * np.log2(hist)).sum() / 6.0)

    # How much of the picture the panel simply cannot place: outside the
    # reachable lightness band, before any question of colour.
    row["above_white"] = float((lightness > PANEL_WHITE_L).mean())
    row["below_black"] = float((lightness < PANEL_BLACK_L).mean())
    return row


def _worker(args: tuple[str, str]) -> dict:
    path, model = args
    try:
        return features_for(Path(path), model)
    except Exception as exc:  # a single unreadable file must not kill the sweep
        return {"name": Path(path).name, "error": f"{type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--server", default=DEFAULT_BASE)
    ap.add_argument("--cache", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--out", type=Path, default=Path("build/camcal/library_features.csv"))
    ap.add_argument("--limit", type=int, default=0, help="first N images only (smoke test)")
    ap.add_argument("--workers", type=int, default=0)
    args = ap.parse_args(argv)

    names = pool(args.server)
    if args.limit:
        names = names[: args.limit]
    print(f"  library: {len(names)} images")

    paths: list[Path] = []
    for i, name in enumerate(names, 1):
        try:
            paths.append(fetch_original(name, args.cache, base=args.server))
        except Exception as exc:
            print(f"  [{i}] {name}: download failed ({type(exc).__name__})")
        if i % 25 == 0:
            print(f"    fetched {i}/{len(names)}", flush=True)

    import multiprocessing as mp  # noqa: PLC0415 — only needed here

    workers = args.workers or min(mp.cpu_count(), max(1, len(paths)))
    rows = []
    with mp.Pool(workers) as p:
        for done, row in enumerate(
            p.imap(_worker, [(str(x), args.model) for x in paths], chunksize=2), 1
        ):
            rows.append(row)
            if done % 25 == 0 or done == len(paths):
                print(f"    featured {done}/{len(paths)}", flush=True)

    frame = pd.DataFrame(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.out, index=False)
    good = [r for r in rows if "error" not in r]
    print()
    print(f"  {len(frame)} rows ({len(rows) - len(good)} failed) -> {args.out}")

    if good:
        oog = np.array([float(r["oog_frac"]) for r in good])
        is_bw = np.array([bool(r["is_bw"]) for r in good])
        has_face = np.array([float(r["n_faces"]) > 0 for r in good])
        print(
            f"  out of gamut: median {100 * np.median(oog):.0f} %, "
            f"p90 {100 * np.percentile(oog, 90):.0f} %, max {100 * oog.max():.0f} %"
        )
        # What the current 3-way classifier would do with this library. Its order
        # matters: B&W wins over faces, so a greyscale portrait routes bw.
        print(
            f"  current classifier would route: bw {int(is_bw.sum())}, "
            f"face {int((~is_bw & has_face).sum())}, "
            f"general {int((~is_bw & ~has_face).sum())}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
