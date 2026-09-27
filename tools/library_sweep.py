#!/usr/bin/env python3
"""Score a spread of the server's real library against the render candidates.

A setting tuned on one photograph is how this project has gone wrong before, so
this picks its sample by measuring rather than by taste, then scores every
candidate on every picked image.

Selection runs on the server's THUMBNAILS — a few kB each, so the whole library
can be characterised in seconds — and the statistic it selects on is the fraction
of pixels the panel cannot reproduce, computed against the measured gamut. That
is the quantity that drives the failure being investigated: error diffusion leaks
hue-opposite ink in proportion to how much of the picture is out of reach, so a
sample spread over that axis is a sample spread over how hard each image is.

Scoring is offline, against the campaign's Yule-Nielsen model. That model has
matched photographs of the glass to within 1-2 L* every time it has been checked
here, which is inside the camera rig's own error bar — so twenty images can be
scored in minutes instead of an hour of panel refreshes, and only the finalists
need real glass.

Three numbers per image, because no single one has survived contact with a human:

  dL*     how much darker the render is than the source. The dominant error.
  dC*     how much colour is lost. Optimising this alone produced a picture a
          human rejected on sight.
  detail  how much of the source's local modulation survives. Added after a
          candidate scored well on the first two and flattened a wall into a slab.

    python tools/library_sweep.py --count 20 --blends 0.25 0.5 0.75
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import panel_arms
from cam_compare import block_mean, delta_e00, detail_ratio, source_canvas
from color_model import INK_NAMES, fit_n, load_records, primaries, yn_mix
from color_validate_photos import lab_img, srgb_img_to_xyz
from gamut_clamp import chroma_ceiling, lookup_ceiling, reachable_lab
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver import image_renderer
from hokku_server import DEFAULT_BASE, fetch_original, image_config, pool
from panel_arms import arm_config, arm_display, parse_arm, render_arm, to_visible

ARM = "palette+gmap+nblack"
BLOCK = 8
PANEL_BLACK_L, PANEL_WHITE_L = 10.86, 66.94  # measured on this glass, campaign findings


def adapted_reference(src: np.ndarray) -> np.ndarray:
    """The source rescaled into the panel's own lightness range, chroma untouched.

    Scoring against the raw source conflates two different things. This panel
    cannot exceed L* 66.9 or go below 10.9, so a bright photograph is guaranteed
    to measure "too dark" however well the pipeline works — and a viewer looking
    at a print adapts to its white anyway, so that part of the deficit is not
    what anyone is complaining about.

    Rescaling linearly into the reachable range keeps RELATIVE contrast identical
    to the source and asks the only question the pipeline can answer: given a
    range of 56 L*, is it being used well? Deviation from this reference is
    fixable; the gap between this reference and the source is not.

    Chroma is deliberately left alone, so dC* against this reference still
    measures the gamut shortfall honestly rather than hiding it.
    """
    out = src.copy()
    out[..., 0] = PANEL_BLACK_L + src[..., 0] / 100.0 * (PANEL_WHITE_L - PANEL_BLACK_L)
    return out


def thumbnail_stats(name: str, ceiling: np.ndarray, base: str) -> dict | None:
    """Mean lightness, mean chroma and out-of-gamut fraction, from the thumbnail."""
    url = f"{base}/hokku/api/thumbnail/" + urllib.parse.quote(name)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read()
        img = np.array(Image.open(io.BytesIO(raw)).convert("RGB"), dtype=np.uint8)
    except Exception:
        return None
    lab = lab_img(srgb_img_to_xyz(img))
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    return {
        "name": name,
        "mean_l": float(lab[..., 0].mean()),
        "mean_c": float(chroma.mean()),
        "oog": float(np.mean(chroma > lookup_ceiling(ceiling, lab))),
    }


def pick_spread(stats: list[dict], count: int) -> list[dict]:
    """Farthest-point selection over (lightness, chroma, out-of-gamut fraction).

    Normalised per axis so no one of them dominates, and seeded with the single
    hardest image so the worst case is always in the sample rather than being
    left to chance.
    """
    feats = np.array([[s["mean_l"] / 100.0, s["mean_c"] / 60.0, s["oog"]] for s in stats])
    chosen = [int(np.argmax(feats[:, 2]))]
    dist = np.linalg.norm(feats - feats[chosen[0]], axis=1)
    while len(chosen) < min(count, len(stats)):
        k = int(np.argmax(dist))
        chosen.append(k)
        dist = np.minimum(dist, np.linalg.norm(feats - feats[k], axis=1))
    return [stats[i] for i in chosen]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--server", default=DEFAULT_BASE)
    ap.add_argument("--outdir", type=Path, default=Path("build/camcal"))
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--blends", type=float, nargs="*", default=[0.25, 0.5, 0.75])
    ap.add_argument(
        "--arms",
        nargs="*",
        default=None,
        help="arm labels to score instead of the blend sweep. The FIRST is the "
        "baseline. Use what the server actually runs — verified by matching its own "
        "ink raster — not what the repo defaults to; those differ.",
    )
    ap.add_argument("--names", nargs="*", default=None, help="skip selection, use these")
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    records = load_records(Path("docs/screens") / args.model / "measurements/data/campaign.jsonl")
    prim = primaries(records)
    n_yn, _ = fit_n([r for r in records if r.get("source") != "ink"], prim, spectral=False)
    prim_mat = np.array([prim[k]["xyz"] for k in INK_NAMES])
    ceiling = chroma_ceiling(reachable_lab(records))

    if args.names:
        picked = [{"name": n, "mean_l": 0.0, "mean_c": 0.0, "oog": 0.0} for n in args.names]
    else:
        names = pool(args.server)
        print(f"  library: {len(names)} images — characterising thumbnails...")
        stats = [s for s in (thumbnail_stats(n, ceiling, args.server) for n in names) if s]
        print(f"  usable thumbnails: {len(stats)}")
        oog = np.array([s["oog"] for s in stats])
        print(
            f"  out-of-gamut fraction across the library: median {100 * np.median(oog):.0f} %, "
            f"p90 {100 * np.percentile(oog, 90):.0f} %, max {100 * oog.max():.0f} %"
        )
        picked = pick_spread(stats, args.count)

    cfg = image_config(base=args.server)
    cache = args.outdir / "server_images"

    def predicted(vis: np.ndarray) -> np.ndarray:
        h, w = vis.shape[0] // BLOCK * BLOCK, vis.shape[1] // BLOCK * BLOCK
        cov = np.stack([(vis[:h, :w] == i).astype(np.float32) for i in range(6)], -1)
        frac = block_mean(cov, BLOCK)
        frac /= np.maximum(frac.sum(axis=-1, keepdims=True), 1e-9)
        mixed = yn_mix(frac.reshape(-1, 6), prim_mat, n_yn)
        return lab_img(mixed.reshape(frac.shape[0], frac.shape[1], 3))

    if args.arms:
        variants = [(a, a, None) for a in args.arms]
    else:
        variants = [("ships now", "none", None)] + [(f"blend {b:g}", ARM, b) for b in args.blends]
    rows = []
    for n, entry in enumerate(picked, 1):
        name = entry["name"]
        try:
            path = fetch_original(name, cache, base=args.server)
            img = Image.open(path).convert("RGB")
            src = lab_img(block_mean(srgb_img_to_xyz(source_canvas(path, display, "x")), BLOCK))
        except Exception as exc:
            print(f"  [{n:2d}/{len(picked)}] {name[:38]:38s} SKIPPED ({type(exc).__name__})")
            continue

        ref = adapted_reference(src)
        src_c = np.hypot(src[..., 1], src[..., 2])
        mid = (src[..., 0] > 40) & (src[..., 0] < 70)
        everywhere = np.ones(src.shape[:2], bool)
        if mid.sum() < 200:
            mid = everywhere

        line = f"  [{n:2d}/{len(picked)}] {name[:36]:36s} oog {100 * entry['oog']:3.0f}% |"
        for _label, arm, blend in variants:
            if blend is not None:
                shutil.copy(args.outdir / f"lut_b{blend}.npy", panel_arms.GAMUT_LUT_PATH)
                image_renderer._cached_correction_lut.cache_clear()
            toggles = parse_arm(arm)
            idx = render_arm(
                arm_display(display, toggles),
                img,
                arm_config(cfg, toggles),
                display.panel_w,
                display.panel_h,
            )
            got = predicted(to_visible(idx, display))
            d_l = float((got[..., 0] - src[..., 0])[mid].mean())
            d_l_adapted = float((got[..., 0] - ref[..., 0])[mid].mean())
            rms_adapted = float(np.sqrt(np.mean((got[..., 0] - ref[..., 0])[mid] ** 2)))
            d_c = float((np.hypot(got[..., 1], got[..., 2]) - src_c)[mid].mean())
            keep_l, _keep_c = detail_ratio(src, got, everywhere)
            rows.append(
                {
                    "image": name,
                    "oog": entry["oog"],
                    "variant": _label,
                    "dL": d_l,
                    "dC": d_c,
                    "detail": float(keep_l),
                    "dL_adapted": d_l_adapted,
                    "rms_adapted": rms_adapted,
                    "de00": float(delta_e00(src, got).mean()),
                }
            )
            line += f" {d_l:+6.1f}/{d_c:+6.1f}"
        print(line, flush=True)

    out = args.outdir / "library_sweep.json"
    out.write_text(json.dumps(rows, indent=1), encoding="utf-8")

    print(
        f"\n  {'variant':12s} | {'dL*':>7s} {'|dL*|':>7s} {'dC*':>7s} | {'detail':>7s} {'dE00':>6s}"
    )
    print("  " + "-" * 58)
    for label, _arm, _b in variants:
        sel = [r for r in rows if r["variant"] == label]
        if not sel:
            continue
        d_l = np.array([r["dL"] for r in sel])
        print(
            f"  {label:20s} | {d_l.mean():+8.1f} | "
            f"{np.mean([r['dL_adapted'] for r in sel]):+9.1f} "
            f"{np.mean([r['rms_adapted'] for r in sel]):9.1f} | "
            f"{np.mean([r['dC'] for r in sel]):+7.1f} {np.mean([r['detail'] for r in sel]):7.2f}"
        )
    print(f"\n  per-image rows -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
