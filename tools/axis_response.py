#!/usr/bin/env python3
"""Does each setting do the same thing to every picture, or different things?

This tests the premise of the whole per-image exercise. If moving a knob has the
same effect on every photograph in the library, then there is nothing for a
per-image model to learn about it — the right answer is a better global default,
and a model would only add machinery. A per-image model earns its place exactly
where a knob's effect *depends on the image*, and only then.

So: take the baseline the server actually runs, move one knob at a time, and
measure the change on every image in the library. Then ask, for each knob, how
much of the variation in its effect is explained by measurable properties of the
image.

  uniform      the average effect, in units of the render noise floor
  varies       the spread of that effect across images, same units
  driven by    the image feature whose correlation with the effect is strongest

**What this cannot say.** Without a fitted objective there is no notion of
"better" here, only "different" — an axis that changes a picture a lot might be
changing it for the worse. That is what the judging session decides. What this
does establish is where per-image treatment could *possibly* pay, which bounds
what the model can achieve before a single verdict is collected.

    python tools/axis_response.py --out build/camcal/axis_response.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from fit_preference import CANDIDATE_METRICS
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_config import ImageConfig
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku_server import DEFAULT_BASE, image_config
from render_bank import close_pool, evaluate, evaluate_many
from session_plan import candidate_pool

# Image properties a model could actually condition on.
FEATURES = (
    "mean_l",
    "std_l",
    "range_l",
    "mean_c",
    "p90_c",
    "oog_frac",
    "detail_energy",
    "skin_frac",
    "n_faces",
    "face_area",
    "above_white",
    "below_black",
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--features", type=Path, default=Path("build/camcal/library_features.csv"))
    ap.add_argument("--imagedir", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--images", type=int, default=0, help="0 = the whole library")
    ap.add_argument("--server", default=DEFAULT_BASE)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--noise-repeats", type=int, default=5)
    ap.add_argument("--cache", type=Path, default=Path("build/camcal/bank_seeded.sqlite"))
    ap.add_argument("--out", type=Path, default=Path("build/camcal/axis_response.csv"))
    args = ap.parse_args(argv)

    try:
        base = image_config(base=args.server)
        print("  baseline: live server config")
    except Exception:
        base = PRESET_IMAGE_CONFIGS["default_general"]
        print("  baseline: repo default_general (server unreachable)")

    features = pd.read_csv(args.features)
    if "error" in features:
        features = features[features["error"].isna()]
    names = [str(n) for n in features["name"]]
    if args.images:
        names = names[: args.images]
    images = [args.imagedir / n for n in names]
    keep = [i for i, p in enumerate(images) if p.exists()]
    images = [images[i] for i in keep]
    names = [names[i] for i in keep]
    print(f"  {len(images)} images")

    # Only the single-knob candidates: an effect has to be attributable.
    axes = [(t[5:], c) for t, c in candidate_pool(base, 0, 0) if t.startswith("axis_")]
    print(f"  {len(axes)} single-knob axes")

    # The floor, so effects are expressed in units of "could this be noise".
    floor_rows = [
        evaluate(images[i], base, args.model, seed=None)
        for i in range(min(3, len(images)))
        for _ in range(args.noise_repeats)
    ]
    metrics = [m for m in CANDIDATE_METRICS if all(m in r for r in floor_rows)]
    raw = np.array([[r[m] for m in metrics] for r in floor_rows])
    noise = raw.reshape(-1, args.noise_repeats, len(metrics)).std(axis=1).mean(axis=0)
    noise = np.maximum(noise, 1e-9)

    jobs: list[tuple[Path, ImageConfig]] = [(p, base) for p in images]
    owner: list[tuple[str, str]] = [("__base__", n) for n in names]
    for tag, cfg in axes:
        for p, n in zip(images, names, strict=True):
            jobs.append((p, cfg))
            owner.append((tag, n))
    print(f"  {len(jobs)} renders...")
    results = evaluate_many(jobs, args.model, args.workers or None, args.cache, verbose=True)

    baseline = {n: r for (tag, n), r in zip(owner, results, strict=True) if tag == "__base__" and r}
    rows = []
    for (tag, name), got in zip(owner, results, strict=True):
        if tag == "__base__" or not got or "error" in got:
            continue
        ref = baseline.get(name)
        if not ref or not all(m in got and m in ref for m in metrics):
            continue
        delta = np.array([got[m] - ref[m] for m in metrics]) / noise
        rows.append(
            {
                "axis": tag,
                "image": name,
                "effect": float(np.linalg.norm(delta) / np.sqrt(len(metrics))),
                **{f"d_{m}": float(d) for m, d in zip(metrics, delta, strict=True)},
            }
        )
    if not rows:
        print("  nothing measured")
        return 1

    # Attach the image features by plain lookup rather than a merge: the frame
    # has been filtered above, which is enough to defeat the type checker.
    columns = {f: [float(v) for v in features[f]] for f in FEATURES}
    lookup = {
        str(n): {f: columns[f][i] for f in FEATURES}
        for i, n in enumerate(str(x) for x in features["name"])
    }
    for row in rows:
        row.update(lookup.get(row["image"], dict.fromkeys(FEATURES, float("nan"))))
    table = pd.DataFrame(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False)

    print()
    print("  Effects are in noise-floor units. 'varies' is the spread across images:")
    print("  large relative to 'uniform' means the same setting does different things")
    print("  to different pictures — which is the only thing a per-image model can use.")
    print()
    print(f"  {'axis':22s} {'uniform':>9s} {'varies':>8s} {'ratio':>7s}  driven by")
    print("  " + "-" * 76)
    summary = []
    for tag, _cfg in axes:
        sub = table[table["axis"] == tag]
        if len(sub) < 20:
            continue
        # Plain lists rather than pandas accessors: the type checker cannot
        # follow a filtered frame's columns, and this is only a mean and a
        # correlation.
        eff = np.asarray(list(sub["effect"]), dtype=float)
        uniform, varies = float(eff.mean()), float(eff.std())
        best_feat, best_r = "", 0.0
        for feat in FEATURES:
            col = np.asarray(list(sub[feat]), dtype=float)
            if np.isnan(col).any():
                continue
            if np.std(col) < 1e-9 or np.std(eff) < 1e-9:
                continue
            r = float(np.corrcoef(eff, col)[0, 1])
            if abs(r) > abs(best_r):
                best_feat, best_r = feat, r
        ratio = varies / max(uniform, 1e-9)
        summary.append((ratio, tag, uniform, varies, best_feat, best_r))
    summary.sort(reverse=True)
    for ratio, tag, uniform, varies, feat, r in summary:
        driver = f"{feat} (r={r:+.2f})" if abs(r) > 0.3 else "-"
        print(f"  {tag:22s} {uniform:9.1f} {varies:8.1f} {ratio:7.2f}  {driver}")

    strong = [s for s in summary if s[0] > 0.5 and abs(s[5]) > 0.3]
    print()
    print(f"  {len(strong)} of {len(summary)} axes have an image-dependent effect")
    print("  (spread at least half the average effect, and predictable from a feature).")
    if strong:
        print("  These are where a per-image model has something real to learn:")
        for _ratio, tag, _u, _v, feat, r in strong:
            print(f"    {tag:22s} tracks {feat} (r={r:+.2f})")
    print(f"\n  {len(table)} rows -> {args.out}")
    close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
