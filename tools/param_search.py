#!/usr/bin/env python3
"""Find the best config for each image, against a fitted objective.

Coordinate descent from the shipped baseline: try each knob's neighbouring
values, keep whatever scores best, repeat until nothing improves. The space is
about 10^18 points, so enumeration is out, and descent from a known-good starting
point is both cheap and interpretable — the result can always be read as "the
baseline, except these three settings".

**Batched across images, not per image.** The obvious loop optimises one picture
to convergence before starting the next, which leaves a 12-worker pool idle
between short bursts. Instead every image advances one knob at a time together,
so each batch is (images x neighbours) renders and the pool stays saturated. For
the whole library that is roughly 500 renders per batch instead of two.

**The objective must be fitted.** Passing `--objective` a file from
`fit_preference.py` is the intended use. There is a provisional fallback so the
machinery can be exercised before a judging session exists, but it is a guess of
exactly the kind this project has been burned by three times — a minimum-dE gamut
map once won every colour metric and was rejected on sight. Results produced with
the fallback are labelled provisional and should not be believed.

    python tools/param_search.py --objective build/camcal/session1/objective.json
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

import config_space
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_config import ImageConfig
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku_server import DEFAULT_BASE, image_config
from render_bank import close_pool, evaluate_many
from session_plan import farthest_point

# A stand-in objective, used only when no fitted one is supplied. Every term is a
# guess. It exists so the search can be tested end to end, not so it can be
# trusted: the whole point of the judging session is to replace this.
PROVISIONAL = {
    "yn_adapted_de00": -1.0,
    "detail_l": 8.0,
    "chroma_vs_source": 3.0,
    "ink_error_roughness": -0.15,
}


class Objective:
    """Metrics -> one number, higher is better."""

    def __init__(self, weights: dict[str, float], fitted: bool, note: str = ""):
        self.weights = weights
        self.fitted = fitted
        self.note = note

    @classmethod
    def load(cls, path: Path | None) -> Objective:
        if path is None:
            return cls(dict(PROVISIONAL), False, "provisional guess — NOT fitted to any verdict")
        blob = json.loads(path.read_text(encoding="utf-8"))
        weights = dict(zip(blob["metrics"], blob["weights"], strict=True))
        weights = {k: v for k, v in weights.items() if abs(v) > 1e-12}
        note = (
            f"fitted: leave-one-image-out accuracy {blob['loio_accuracy']:.3f} "
            f"against a self-agreement ceiling of {blob['self_agreement']:.3f}"
        )
        return cls(weights, True, note)

    def __call__(self, metrics: dict) -> float:
        if not metrics or "error" in metrics:
            return -np.inf
        return sum(w * metrics.get(k, 0.0) for k, w in self.weights.items())


def knob_order(sensitivity: Path | None) -> list[str]:
    """Knobs worth searching, strongest first, inert ones dropped.

    A knob that moves the picture less than the renderer's own noise cannot be
    optimised — the descent would just follow the dither's random draw.
    """
    names = list(config_space.KNOB_NAMES)
    if sensitivity is None or not sensitivity.exists():
        return names
    table = pd.read_csv(sensitivity)
    # Plain aggregation: a groupby result is opaque to the type checker and this
    # is a two-line mean.
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for knob, value in zip(table["knob"].tolist(), table["effect"].tolist(), strict=True):
        totals[str(knob)] = totals.get(str(knob), 0.0) + float(value)
        counts[str(knob)] = counts.get(str(knob), 0) + 1
    effect = {k: totals[k] / counts[k] for k in totals}
    live = [n for n in names if effect.get(n, np.inf) >= np.sqrt(2.0)]
    dropped = [n for n in names if n not in live]
    if dropped:
        print(f"  skipping {len(dropped)} inert knob(s): {', '.join(dropped)}")
    return sorted(live, key=lambda n: -effect.get(n, 0.0))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--features", type=Path, default=Path("build/camcal/library_features.csv"))
    ap.add_argument("--imagedir", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--images", type=int, default=0, help="0 = the whole library")
    ap.add_argument("--objective", type=Path, default=None)
    ap.add_argument("--sensitivity", type=Path, default=Path("build/camcal/sensitivity.csv"))
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--server", default=DEFAULT_BASE)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--cache", type=Path, default=Path("build/camcal/bank_seeded.sqlite"))
    ap.add_argument("--out", type=Path, default=Path("build/camcal/per_image_best.json"))
    args = ap.parse_args(argv)

    objective = Objective.load(args.objective)
    print(f"  objective: {objective.note}")
    if not objective.fitted:
        print("  WARNING: results are provisional and should not drive any decision")

    try:
        base = image_config(base=args.server)
        print("  baseline: live server config")
    except Exception:
        base = PRESET_IMAGE_CONFIGS["default_general"]
        print("  baseline: repo default_general (server unreachable)")

    features = pd.read_csv(args.features)
    names = [str(n) for n in features["name"]]
    if args.images:
        feats = ("mean_l", "mean_c", "oog_frac", "detail_energy")
        frame = features.dropna(subset=list(feats)).reset_index(drop=True)
        raw = frame[list(feats)].to_numpy(dtype=float)
        norm = (raw - raw.mean(0)) / np.maximum(raw.std(0), 1e-9)
        picked = farthest_point(norm, args.images, int(np.argmax(frame["oog_frac"].to_numpy())))
        names = [str(frame.loc[i, "name"]) for i in picked]
    images = [args.imagedir / n for n in names]
    images = [p for p in images if p.exists()]
    print(f"  {len(images)} images, {args.rounds} rounds")

    knobs = knob_order(args.sensitivity)
    print(f"  {len(knobs)} knobs in play")

    state: dict[str, ImageConfig] = {str(p): base for p in images}
    baseline = evaluate_many(
        [(p, base) for p in images], args.model, args.workers or None, args.cache, verbose=False
    )
    score = {str(p): objective(m) for p, m in zip(images, baseline, strict=True)}
    start_score = dict(score)
    start_metrics = {str(p): m for p, m in zip(images, baseline, strict=True)}

    improved_total = 0
    for round_no in range(1, args.rounds + 1):
        moved_this_round = 0
        for knob in knobs:
            jobs: list[tuple[Path, ImageConfig]] = []
            owner: list[str] = []
            for p in images:
                for cfg in config_space.neighbours(state[str(p)], knob):
                    jobs.append((p, cfg))
                    owner.append(str(p))
            if not jobs:
                continue
            results = evaluate_many(
                jobs, args.model, args.workers or None, args.cache, verbose=False
            )
            for (p, cfg), key, metrics in zip(jobs, owner, results, strict=True):
                value = objective(metrics)
                if value > score[key] + 1e-9:
                    score[key], state[key] = value, cfg
                    moved_this_round += 1
                    del p
        improved_total += moved_this_round
        gains = np.array([score[str(p)] - start_score[str(p)] for p in images])
        print(
            f"  round {round_no}: {moved_this_round} accepted moves, "
            f"mean gain {gains.mean():+.3f}, best {gains.max():+.3f}",
            flush=True,
        )
        if moved_this_round == 0:
            break

    final = evaluate_many(
        [(p, state[str(p)]) for p in images],
        args.model,
        args.workers or None,
        args.cache,
        verbose=False,
    )
    rows = []
    for p, metrics in zip(images, final, strict=True):
        key = str(p)
        rows.append(
            {
                "image": p.name,
                "baseline_score": start_score[key],
                "best_score": score[key],
                "gain": score[key] - start_score[key],
                "changed": config_space.describe(state[key], base),
                "config": asdict(state[key]),
                "baseline_metrics": start_metrics[key],
                "best_metrics": metrics,
            }
        )
    rows.sort(key=lambda r: -r["gain"])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "objective": objective.weights,
                "objective_fitted": objective.fitted,
                "objective_note": objective.note,
                "baseline_config": asdict(base),
                "results": rows,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    gains = np.array([r["gain"] for r in rows])
    unchanged = sum(1 for r in rows if r["changed"] == "(baseline)")
    print(f"\n  {improved_total} accepted moves over {len(rows)} images")
    print(
        f"  gain: mean {gains.mean():+.3f}, median {np.median(gains):+.3f}, max {gains.max():+.3f}"
    )
    print(f"  {unchanged} image(s) kept the baseline unchanged")
    print("\n  biggest movers:")
    for row in rows[:8]:
        print(f"    {row['image'][:34]:34s} {row['gain']:+7.3f}  {row['changed'][:70]}")

    # Which knobs the search actually reached for, across the library. If one
    # setting wins nearly everywhere, that is a global default rather than a
    # per-image decision, and worth knowing before building a model.
    counts: dict[str, int] = {}
    for row in rows:
        if row["changed"] == "(baseline)":
            continue
        for part in row["changed"].split(", "):
            counts[part.split("=")[0]] = counts.get(part.split("=")[0], 0) + 1
    print("\n  how often each knob moved away from the baseline:")
    for knob, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {knob:30s} {n:4d} / {len(rows)}  ({100 * n / len(rows):.0f}%)")
    print(f"\n  -> {args.out}")
    close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
