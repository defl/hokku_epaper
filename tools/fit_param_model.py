#!/usr/bin/env python3
"""Learn image features -> render settings, and check it beats the 3-way preset.

The current model is a two-question classifier: near-greyscale, then any face
detected, else general. Measured across this library that routes 204 of 245
images to one preset, so in practice it is not a three-way choice at all. It also
cannot express prominence — a headshot filling the frame and a crowd scene with
twenty incidental background faces get identical treatment.

This replaces it with a regression from measured image features to a point in the
config space, trained to imitate the per-image search.

**Evaluated by rendering, not by parameter distance.** A model can predict a
config vector close to the optimum in parameter space and still render badly,
because the knobs are not equally consequential. So the predicted config is
actually rendered and scored, and reported against two references that bracket
what is achievable:

    baseline   what ships today
    model      what this predicts for an image it never saw
    optimum    what the search found with that image in hand

The gap from baseline to optimum is what the whole exercise is worth. The gap
from model to optimum is what generalisation costs. Both get reported, including
when the first is small.

**Leave-one-image-out**, because with 245 images and a model this size, a random
split would train and test on the same photograph's neighbours.

    python tools/fit_param_model.py --best build/camcal/per_image_best.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.model_selection import LeaveOneOut

import config_space
from hokku.webserver.image_config import image_config_from_dict_strict
from param_search import Objective
from render_bank import close_pool, evaluate_many

# Features the model may use. Deliberately excludes the image name and anything
# that could act as an identifier — a model that memorises which picture it is
# looking at would validate perfectly and generalise not at all.
DROP = {"name", "error"}


def feature_matrix(features: pd.DataFrame, names: list[str]) -> tuple[np.ndarray, list[str]]:
    columns = [c for c in features.columns if c not in DROP]
    indexed = features.set_index("name")
    rows = [indexed.loc[n, columns].to_numpy(dtype=float) for n in names]
    return np.nan_to_num(np.array(rows, dtype=float)), columns


def fit_predict_loo(x: np.ndarray, targets: np.ndarray, kinds: list[str]) -> np.ndarray:
    """Leave-one-out predictions for every knob, classifier or regressor per kind."""
    predicted = np.zeros_like(targets, dtype=float)
    splitter = LeaveOneOut()
    for train, test in splitter.split(x):
        for j, kind in enumerate(kinds):
            column = targets[train, j]
            if len(np.unique(column)) < 2:
                predicted[test, j] = column[0]
                continue
            if kind == "cat":
                model = RandomForestClassifier(
                    n_estimators=60, max_depth=4, random_state=0, n_jobs=1
                )
                model.fit(x[train], column.astype(int))
                predicted[test, j] = model.predict(x[test])
            else:
                model = RandomForestRegressor(
                    n_estimators=60, max_depth=4, random_state=0, n_jobs=1
                )
                model.fit(x[train], column)
                predicted[test, j] = model.predict(x[test])
    return predicted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301")
    ap.add_argument("--features", type=Path, default=Path("build/camcal/library_features.csv"))
    ap.add_argument("--best", type=Path, default=Path("build/camcal/per_image_best.json"))
    ap.add_argument("--imagedir", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--objective", type=Path, default=None)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--cache", type=Path, default=Path("build/camcal/bank_seeded.sqlite"))
    ap.add_argument("--out", type=Path, default=Path("build/camcal/param_model.json"))
    args = ap.parse_args(argv)

    blob = json.loads(args.best.read_text(encoding="utf-8"))
    if not blob.get("objective_fitted"):
        print("  NOTE: the search used a provisional objective; treat all of this as a dry run")
    objective = Objective.load(args.objective)
    base = image_config_from_dict_strict(blob["baseline_config"])
    results = blob["results"]
    names = [r["image"] for r in results]
    print(f"  {len(results)} images with a searched optimum")

    features = pd.read_csv(args.features)
    have = set(features["name"])
    keep = [i for i, n in enumerate(names) if n in have]
    results = [results[i] for i in keep]
    names = [names[i] for i in keep]
    if len(results) < 20:
        print(f"  only {len(results)} usable images — too few to fit anything trustworthy")
        return 1

    x, columns = feature_matrix(features, names)
    configs = [image_config_from_dict_strict(r["config"]) for r in results]
    targets = np.array([config_space.to_vector(c) for c in configs])
    kinds = [k for _n, k, _s in config_space.KNOBS]
    print(f"  {x.shape[1]} features -> {targets.shape[1]} knobs")

    predicted = fit_predict_loo(x, targets, kinds)
    predicted_configs = [config_space.from_vector(base, v) for v in predicted]

    # How often the model picks the same value the search did, per knob. Useful
    # for reading the model, but never the headline: agreement in parameter space
    # is not the same as agreeing on the picture.
    print("\n  per-knob agreement with the search (held out):")
    for j, (name, kind, _spec) in enumerate(config_space.KNOBS):
        if kind == "cat":
            agree = float(np.mean(np.round(predicted[:, j]) == np.round(targets[:, j])))
            print(f"    {name:34s} {100 * agree:5.0f}% exact")
        else:
            spread = targets[:, j].std()
            err = float(np.mean(np.abs(predicted[:, j] - targets[:, j])))
            note = "  (search never moved it)" if spread < 1e-9 else ""
            print(f"    {name:34s} mean |error| {err:.3f} of a 0..1 range{note}")

    # The honest test: render what the model predicts and score it.
    images = [args.imagedir / n for n in names]
    jobs = [(p, c) for p, c in zip(images, predicted_configs, strict=True)]
    print(f"\n  rendering {len(jobs)} held-out predictions...")
    scored = evaluate_many(jobs, args.model, args.workers or None, args.cache, verbose=True)

    rows = []
    for name, result, metrics in zip(names, results, scored, strict=True):
        if not metrics or "error" in metrics:
            continue
        rows.append(
            {
                "image": name,
                "baseline": result["baseline_score"],
                "model": objective(metrics) if objective.fitted else result["baseline_score"],
                "optimum": result["best_score"],
            }
        )
    if not rows:
        print("  nothing scored")
        return 1

    baseline = np.array([r["baseline"] for r in rows])
    model_score = np.array([r["model"] for r in rows])
    optimum = np.array([r["optimum"] for r in rows])
    total = optimum - baseline
    captured = model_score - baseline
    share = float(np.sum(captured) / np.sum(total)) if np.sum(total) > 1e-12 else float("nan")

    print(f"\n  scores over {len(rows)} held-out images")
    print(f"    baseline (ships today)   {baseline.mean():+8.3f}")
    print(f"    model    (never saw it)  {model_score.mean():+8.3f}")
    print(f"    optimum  (search)        {optimum.mean():+8.3f}")
    print(f"\n  the search finds {total.mean():+.3f} per image over the baseline")
    print(f"  the model captures {captured.mean():+.3f} of that = {100 * share:.0f}%")
    print(f"  model beats baseline on {int((captured > 0).sum())} of {len(rows)} images")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "features": columns,
                "knobs": list(config_space.KNOB_NAMES),
                "baseline_config": asdict(base),
                "held_out_scores": rows,
                "share_of_available_gain": share,
                "objective_fitted": bool(blob.get("objective_fitted")),
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n  -> {args.out}")
    close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
