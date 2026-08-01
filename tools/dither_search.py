#!/usr/bin/env python3
"""Search the existing dither settings for the best config per pipeline.

The server keeps three pipelines — general, B&W and faces — and each is a full
``ImageConfig``. This walks the existing knob space, renders the real test
corpus through the production renderer, scores the result, and ranks it. No new
settings are invented: every candidate is expressible in ``config.json`` today,
so a winner can be adopted by editing the preset and nothing else.

Why this exists
---------------
Warm mid-tones (lips, skin) pick up blue ink through an error-diffusion cascade:
the nearest ink for a warm mid-tone is white, the residual is cold, and after a
few pixels the accumulated value lands where blue is genuinely nearest. The
existing metrics do not catch it — ``neutral_blue_fraction`` only looks at
source-*neutral* pixels, while this failure is in source-*saturated* warm ones.
So the current default can score well and still put 22 % blue ink in lips.

This adds ``warm_blue`` to close that hole, and otherwise scores with the
repo's own ``image_compare`` so numbers stay comparable to
``test_dither_quality_metrics``.

Search shape
------------
A full cross product is combinatorially silly, so it runs in two stages:

  stage 1  algorithm x LUT, everything else at the pipeline's current preset
  stage 2  the stage-1 winners x the tonal/chromatic knobs

Staging assumes the algorithm/LUT choice dominates and the rest refines it —
true here because the blue cascade is a geometry problem, not a tonal one.

Usage:
    python tools/dither_search.py --profile faces
    python tools/dither_search.py --all --canvas 800x600
    python tools/dither_search.py --all --stage 1        # quick pass
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import rgb_to_lab
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.image_quality import image_compare
from hokku.webserver.image_renderer import ImageRenderer, open_image_for_render
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS

REPO = Path(__file__).resolve().parent.parent
TEST_IMAGES = REPO / "images" / "test"

BLUE_INK = 4

ALGORITHMS = ("floyd_steinberg", "atkinson", "stucki")
COLOUR_LUTS = (
    "euclidean",
    "euclidean_weighted",
    "hue_aware",
    "hue_aware_weighted",
    "oklab",
    "oklab_hue_aware",
    "cam16ucs",
    "cam16ucs_hue_aware",
)

# Images too large to be worth rendering in a search sweep.
SKIP = {"synth_black_10000x10000", "Albi_Panorama_Sunset_Panini_General"}


# ── the metric the existing set is missing ───────────────────────────────────


def warm_blue_fractions(src_rgb: np.ndarray, idx: np.ndarray, valid: np.ndarray) -> dict:
    """Blue-ink leakage into warm source pixels, split by chroma.

    Splitting matters. A single "warm" band spanning hue -40..70 deg with
    C* > 20 is dominated by **skin** on any portrait, so averaging over it hides
    the artifact people actually complain about, which happens in the small,
    highly-saturated regions — lips, lipstick, blush. Measured on flat patches,
    lip colour goes 30 % blue under CIELAB LUTs and ~5 % under OKLAB; averaged
    across all warm pixels that 6x difference disappears into the skin count.

    So two numbers:
      warm_blue      all warm pixels, C* > 20  — the broad view (skin-dominated)
      lips_blue      warm pixels, C* > 38      — the artifact being complained about
    """
    lab = rgb_to_lab(np.asarray(src_rgb, dtype=np.float64))
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    hue = np.degrees(np.arctan2(lab[..., 2], lab[..., 1]))
    warm_hue = valid & (hue > -40.0) & (hue < 70.0)

    out = {}
    for key, floor in (("warm_blue", 20.0), ("lips_blue", 38.0)):
        sel = warm_hue & (chroma > floor)
        out[key] = float((idx[sel] == BLUE_INK).mean()) if sel.any() else 0.0
    return out


# ── corpus selection, using the server's own classifiers ─────────────────────


def classify_corpus() -> dict[str, list[Path]]:
    """Split the test corpus the way the server would: B&W, faces, general."""
    # Lazy: loading the YuNet DNN costs ~57 MB and several seconds, and a
    # --profile bw run never needs it.
    from hokku.webserver.face_detect_yunet_opencv import (  # noqa: PLC0415
        OpenCVYuNetFaceDetector,
    )

    detector = OpenCVYuNetFaceDetector()
    bw: list[Path] = []
    faces: list[Path] = []
    general: list[Path] = []
    for p in sorted(TEST_IMAGES.iterdir()):
        if p.suffix.lower() in {".md"} or p.stem in SKIP or not p.is_file():
            continue
        try:
            # Same B&W test the server's classifier uses, so the split here
            # matches the pipeline each image would really be routed to.
            if ImageClassifier._check_grayscale(p):
                bw.append(p)
                continue
            general.append(p)
            if detector.detect(p):
                faces.append(p)
        except Exception as e:
            print(f"  (skipping {p.name}: {type(e).__name__}: {e})")
    return {"general": general, "bw": bw, "faces": faces}


# ── scoring ──────────────────────────────────────────────────────────────────
#
# Each profile reduces the metric dict to one number, lower = better. Weights
# encode what that pipeline is *for*; they are the opinion in this tool and the
# thing to argue with.

PROFILE_WEIGHTS = {
    # General: overall accuracy first, keep saturation, punish blue in warm tones.
    "general": {
        "overall_dE2000": 1.0,
        "sat_hit": -12.0,
        "neutral_leak": 0.6,
        "warm_blue": 60.0,
        "lips_blue": 260.0,
    },
    # B&W: any colour at all is the failure mode. Neutral leak dominates.
    "bw": {
        "overall_dE2000": 1.0,
        "neutral_leak": 4.0,
        "neutral_blue_fraction": 300.0,
        "warm_blue": 150.0,
        "lips_blue": 150.0,
    },
    # Faces: skin is the whole job — blue lips are the loudest artifact, and hue
    # fidelity matters more than raw dE.
    "faces": {
        "overall_dE2000": 0.8,
        "sat_hit": -8.0,
        "hue_error": 0.35,
        "warm_blue": 90.0,
        "lips_blue": 450.0,
    },
}


def score(metrics: dict[str, float], profile: str) -> float:
    return sum(w * metrics.get(k, 0.0) for k, w in PROFILE_WEIGHTS[profile].items())


# ── rendering ────────────────────────────────────────────────────────────────


def evaluate(cfg, images: list[Path], renderer, display, canvas: tuple[int, int]) -> dict:
    """Mean metrics for *cfg* across *images*."""
    acc: dict[str, list[float]] = {}
    for path in images:
        with open_image_for_render(path) as img:
            idx = renderer.render_indices(
                img, cfg, Orientation.LANDSCAPE, canvas[0], canvas[1], crop_to_fill_threshold=0.0
            )
        # Compare against what the panel will actually show: measured inks.
        derived = display.palette_measured_rgb[idx]
        with open_image_for_render(path) as img:
            src = np.asarray(
                img.convert("RGB").resize((idx.shape[1], idx.shape[0])), dtype=np.uint8
            )
        m = image_compare(src, derived)
        m.update(warm_blue_fractions(src, idx, np.ones(idx.shape, dtype=bool)))
        for k, v in m.items():
            acc.setdefault(k, []).append(float(v))
    return {k: float(np.mean(v)) for k, v in acc.items()}


def stage1_candidates(base):
    for algo in ALGORITHMS:
        for lut in COLOUR_LUTS:
            yield (
                f"{algo}+{lut}",
                replace(base, dither=replace(base.dither, algorithm=algo, lut_name=lut)),
            )


def stage2_candidates(name, cfg):
    """Refine a stage-1 winner over the tonal/chromatic knobs."""
    for sat in ("off", "cielab", "oklab"):
        for vivid in (True, False):
            for drc in ("cielab", "oklab"):
                for serp in (True, False):
                    yield (
                        f"{name} sat={sat} vivid={int(vivid)} drc={drc} serp={int(serp)}",
                        replace(
                            cfg,
                            adaptive_saturate_space=sat,
                            adaptive_vivid=vivid,
                            drc_l_space=drc,
                            drc_chroma_space=drc,
                            dither=replace(cfg.dither, serpentine=serp),
                        ),
                    )


BASE_PRESET = {
    "general": "atkinson_hue_aware",
    "bw": "floyd_steinberg_bw",
    "faces": "atkinson_hue_aware",
}


def run_profile(profile: str, corpus, args, renderer, display, canvas) -> dict:
    images = corpus[profile]
    base = PRESET_IMAGE_CONFIGS[BASE_PRESET[profile]]
    print("=" * 92)
    print(f"  {profile.upper()}  — {len(images)} image(s), base preset {BASE_PRESET[profile]}")
    print("=" * 92)
    if not images:
        print("  (no images in this class)")
        return {}

    # Baseline: what ships today.
    t0 = time.monotonic()
    base_m = evaluate(base, images, renderer, display, canvas)
    base_s = score(base_m, profile)
    print(
        f"  baseline {BASE_PRESET[profile]:<26} score {base_s:8.3f}"
        f"  dE2000 {base_m['overall_dE2000']:6.2f}  warm {100 * base_m['warm_blue']:5.2f}%"
        f"  lips {100 * base_m['lips_blue']:6.2f}%"
        f"  ({time.monotonic() - t0:.1f}s/config)"
    )
    print()

    results = []
    cands = list(stage1_candidates(base))
    # The B&W pipeline also gets the bw LUT, which is the point of that pipeline.
    if profile == "bw":
        for algo in ALGORITHMS:
            cands.append(
                (
                    f"{algo}+bw",
                    replace(base, dither=replace(base.dither, algorithm=algo, lut_name="bw")),
                )
            )
    print(f"  stage 1: {len(cands)} algorithm x LUT combinations")
    for i, (name, cfg) in enumerate(cands, 1):
        m = evaluate(cfg, images, renderer, display, canvas)
        s = score(m, profile)
        results.append({"name": name, "score": s, "metrics": m, "stage": 1})
        print(
            f"    [{i:>2}/{len(cands)}] {name:<32} {s:8.3f}"
            f"  dE2000 {m['overall_dE2000']:6.2f}  warm {100 * m['warm_blue']:5.2f}%"
            f"  lips {100 * m['lips_blue']:6.2f}%"
        )

    results.sort(key=lambda r: r["score"])
    if args.stage >= 2:
        print()
        top = results[: args.refine]
        print(f"  stage 2: refining the top {len(top)}")
        for entry in top:
            cfg = dict(cands)[entry["name"]]
            for name, cand in stage2_candidates(entry["name"], cfg):
                m = evaluate(cand, images, renderer, display, canvas)
                s = score(m, profile)
                results.append({"name": name, "score": s, "metrics": m, "stage": 2})
        results.sort(key=lambda r: r["score"])

    print()
    print(f"  --- best for {profile} ---")
    for r in results[:8]:
        m = r["metrics"]
        print(
            f"    {r['score']:8.3f}  {r['name']:<52} dE2000 {m['overall_dE2000']:6.2f}"
            f"  sat {m['sat_hit']:.3f}  lips_blue {100 * m['lips_blue']:6.2f}%"
        )
    win = results[0]
    print(
        f"\n  baseline score {base_s:.3f} -> best {win['score']:.3f}"
        f"  ({100 * (base_s - win['score']) / abs(base_s) if base_s else 0:.1f}% better)"
    )
    return {
        "baseline": {"name": BASE_PRESET[profile], "score": base_s, "metrics": base_m},
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--profile", choices=("general", "bw", "faces"))
    g.add_argument("--all", action="store_true")
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--canvas", default="800x600", help="render canvas WxH (smaller = faster)")
    ap.add_argument("--stage", type=int, default=2, choices=(1, 2))
    ap.add_argument("--refine", type=int, default=2, help="how many stage-1 winners to refine")
    ap.add_argument("--out", default="build/dither_search", help="where to write the JSON")
    args = ap.parse_args(argv)

    cw, ch = (int(v) for v in args.canvas.lower().split("x"))
    display = DISPLAY_REGISTRY[args.model]
    renderer = ImageRenderer(NumbaStreamingDither(display), display=display)

    print(f"model {args.model}, canvas {cw}x{ch}, corpus {TEST_IMAGES}")
    corpus = classify_corpus()
    for k, v in corpus.items():
        print(f"  {k:<8} {len(v):>2} image(s): {', '.join(p.stem[:22] for p in v)}")
    print()

    profiles = ("general", "bw", "faces") if args.all else (args.profile,)
    out: dict = {"model": args.model, "canvas": [cw, ch], "profiles": {}}
    for p in profiles:
        out["profiles"][p] = run_profile(p, corpus, args, renderer, display, (cw, ch))
        print()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "search.json"
    dest.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"full results -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
