#!/usr/bin/env python3
"""Fit a scalar objective to blind verdicts, and report whether it is worth using.

This is the step the whole plan turns on. Everything downstream — the per-image
search, the fitted config model — optimises *something*, and if that something is
guessed then the search will confidently find what nobody wants. It has happened
here: a minimum-dE gamut map won every colour metric and was rejected on sight.

So the objective is fitted to what a person actually chose, and then judged on
whether it predicts choices it has not seen.

**How ties are used.** A tie carries no sign, so it cannot train a direction and
is excluded from the fit. It is not wasted: a good objective must place ties near
zero, and the separation between |score| on ties and on decided trials is
reported as a check. An objective that ranks decided pairs well but puts ties
just as far from zero has learned something other than what was seen.

**Antisymmetry is imposed, not hoped for.** Every trial is included twice, mirrored
(-x, flipped label), and the model is fitted without an intercept. Otherwise the
fit can buy accuracy with a constant "prefer the right-hand side", which is a
statement about the page layout rather than the panel.

**Validation is leave-one-image-out.** Trials from one photograph are not
independent — they share a subject, an exposure, a gamut problem. Splitting
randomly would train and test on the same picture and report an accuracy that
evaporates on the next one.

**Accuracy is reported against the ceiling, never against 100 %.** The repeat
trials measure how often the judge agrees with themselves. No metric can beat
that, and a fit at 70 % is excellent against a ceiling of 75 % and poor against
95 %.

    python tools/fit_preference.py --session build/camcal/session1 \\
        --verdicts ~/Downloads/verdicts.json
    python tools/fit_preference.py --synthetic     # self-test the machinery
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression

# Candidate metrics for the objective. Deliberately a short list: roughly 65
# trials cannot identify 40 free weights, and an objective nobody can read is one
# nobody can argue with.
CANDIDATE_METRICS = (
    "yn_adapted_de00",
    "yn_adapted_dL",
    "yn_adapted_rmsL",
    "yn_dC",
    "yn_dhue",
    "detail_l",
    "detail_c",
    "chroma_use",
    "chroma_vs_source",
    "skin_dhue",
    "skin_dC",
    "oog_dhue",
    "ink_neutral_leak",
    "ink_warm_blue",
    "ink_lips_blue",
    "ink_error_roughness",
    "ink_high_freq_energy_ratio",
    # Measured inside detected faces rather than a hue mask. Added after the
    # rating notes named "blue lips", "blue face, looks dead" and "faces WAY too
    # red" among the commonest complaints, while the hue-masked blue metrics
    # tested unstable — a hue mask cannot tell a face from a brick wall.
    "face_de00",
    "face_dL",
    "face_dC",
    "face_dhue",
    "face_detail_l",
    "face_contrast",
    "face_ink_blue",
    "face_ink_red",
    "face_ink_black",
    # "Washed out" was a third of the complaints and nothing measured it.
    "contrast_ratio",
    "chroma_contrast_ratio",
)


def usable_metrics(key: list[dict], votes: dict, metrics: tuple[str, ...]) -> tuple[str, ...]:
    """The candidate metrics present on both sides of every judged trial.

    Region metrics disappear when their mask is too small to measure — a picture
    with no skin has no `skin_dC`. Requiring the full list would silently discard
    every trial on such an image, which on a deliberately varied set could be
    most of the session. Dropping the metric instead costs one column and keeps
    every verdict.
    """
    judged = [e for e in key if e["id"] in votes]
    if not judged:
        return ()
    return tuple(
        m
        for m in metrics
        if all(m in e["left_metrics"] and m in e["right_metrics"] for e in judged)
    )


def build_dataset(key: list[dict], votes: dict, metrics: tuple[str, ...]):
    """(X, y, groups, kinds) with X = right-minus-left metric deltas."""
    rows, labels, groups, kinds, ids = [], [], [], [], []
    for entry in key:
        vote = votes.get(entry["id"])
        if not vote:
            continue
        left, right = entry["left_metrics"], entry["right_metrics"]
        if not all(m in left and m in right for m in metrics):
            continue
        rows.append([right[m] - left[m] for m in metrics])
        labels.append(vote["choice"])
        groups.append(entry["image"])
        kinds.append(entry["kind"])
        ids.append(entry["id"])
    return (
        np.array(rows, dtype=float),
        np.array(labels),
        np.array(groups),
        np.array(kinds),
        np.array(ids),
    )


def self_agreement(key: list[dict], votes: dict) -> tuple[float, int]:
    """How often the judge repeated their own verdict on a swapped repeat.

    The sides are swapped in a repeat, so agreeing means giving the mirrored
    answer. This is the ceiling: no objective can predict a judge better than
    the judge predicts themselves.
    """
    agree = total = 0
    for entry in key:
        if entry["kind"] != "repeat" or "repeat_of" not in entry:
            continue
        now, before = votes.get(entry["id"]), votes.get(entry["repeat_of"])
        if not now or not before:
            continue
        mirrored = {
            "left": "right",
            "right": "left",
            "tie": "tie",
            "both_bad": "both_bad",
        }[now["choice"]]
        agree += mirrored == before["choice"]
        total += 1
    return (agree / total if total else float("nan")), total


def fit(x: np.ndarray, y: np.ndarray, c: float = 1.0) -> LogisticRegression:
    """L1 logistic on mirrored data, no intercept — see the module docstring."""
    xx = np.vstack([x, -x])
    yy = np.concatenate([y, 1 - y])
    # l1_ratio=1 is L1; scikit-learn 1.8 deprecated the `penalty` argument.
    model = LogisticRegression(
        l1_ratio=1, C=c, solver="liblinear", fit_intercept=False, max_iter=5000
    )
    model.fit(xx, yy)
    return model


def leave_one_image_out(x, y, groups, c: float) -> tuple[float, int]:
    """Accuracy on decided trials, training on every other photograph."""
    correct = total = 0
    for held in np.unique(groups):
        train, test = groups != held, groups == held
        if train.sum() < 8 or test.sum() == 0 or len(np.unique(y[train])) < 2:
            continue
        model = fit(x[train], y[train], c)
        correct += int((model.predict(x[test]) == y[test]).sum())
        total += int(test.sum())
    return (correct / total if total else float("nan")), total


def report(x, y, groups, metrics, scale, ceiling, ceiling_n) -> dict:
    print(f"\n  {len(y)} decided trials over {len(np.unique(groups))} images")

    best = (None, -1.0, 0)
    for c in (0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 4.0):
        acc, n = leave_one_image_out(x, y, groups, c)
        if np.isfinite(acc):
            marker = ""
            if acc > best[1]:
                best, marker = (c, acc, n), "  <-"
            print(f"    C={c:<5g} leave-one-image-out accuracy {acc:.3f} on {n} trials{marker}")
    c_best, acc_best, n_best = best
    if c_best is None:
        print("  not enough data to validate")
        return {}

    model = fit(x, y, c_best)
    weights = model.coef_[0] / np.maximum(scale, 1e-12)
    print()
    print(f"  chance is 0.500; you agreed with yourself on {ceiling:.3f} of {ceiling_n} repeats")
    print(f"  fitted objective reaches {acc_best:.3f} on {n_best} held-out trials")

    # Self-agreement is NOT an upper bound on model accuracy, and treating it
    # as one printed a nonsensical "136 % of the way to the ceiling". A judge
    # who follows a stable preference with probability q and otherwise flips
    # agrees with themselves at q^2 + (1-q)^2, while a noiseless model of that
    # same preference scores q — which is higher. Inverting gives what a
    # perfect model could actually reach.
    if np.isfinite(ceiling) and ceiling > 0.5:
        q = 0.5 * (1.0 + np.sqrt(max(2.0 * ceiling - 1.0, 0.0)))
        print(f"  that implies a stable preference on {q:.0%} of trials, so a")
        print(f"  perfect model of it would score about {q:.3f}")
        if q > 0.5:
            share = (acc_best - 0.5) / max(q - 0.5, 1e-9)
            print(f"  = {share:.0%} of the achievable signal")

    print("\n  weights (positive means MORE of this metric was preferred):")
    order = np.argsort(-np.abs(model.coef_[0]))
    for i in order:
        if abs(model.coef_[0][i]) < 1e-8:
            continue
        print(f"    {metrics[i]:28s} {weights[i]:+10.4f}   (standardised {model.coef_[0][i]:+.3f})")
    dropped = [metrics[i] for i in range(len(metrics)) if abs(model.coef_[0][i]) < 1e-8]
    if dropped:
        print(f"    dropped to zero: {', '.join(dropped)}")
    return {
        "metrics": list(metrics),
        "weights": weights.tolist(),
        "standardised_weights": model.coef_[0].tolist(),
        "scale": scale.tolist(),
        "C": c_best,
        "loio_accuracy": acc_best,
        "n_heldout": n_best,
        "self_agreement": ceiling,
        "n_repeats": ceiling_n,
    }


def synthetic() -> int:
    """Prove the machinery recovers a known objective before trusting it on real data."""
    rng = np.random.default_rng(0)
    metrics = CANDIDATE_METRICS[:8]
    truth = np.zeros(len(metrics))
    truth[0], truth[5] = -1.0, 2.0  # dislikes error, likes detail
    x = rng.normal(size=(220, len(metrics)))
    groups = rng.integers(0, 8, size=len(x)).astype(str)
    z = x @ truth + rng.normal(scale=0.4, size=len(x))
    keep = np.abs(z) > 0.35  # the rest would have been called ties
    x, z, groups = x[keep], z[keep], groups[keep]
    y = (z > 0).astype(int)

    scale = np.maximum(x.std(0), 1e-9)
    acc, n = leave_one_image_out(x / scale, y, groups, 1.0)
    model = fit(x / scale, y, 1.0)
    recovered = model.coef_[0] / scale
    corr = float(np.corrcoef(recovered, truth)[0, 1])
    print(f"  synthetic: {len(y)} trials, held-out accuracy {acc:.3f} on {n}")
    print(f"  weight correlation with ground truth {corr:+.3f}")
    top = [metrics[i] for i in np.argsort(-np.abs(recovered))[:2]]
    print(f"  strongest recovered: {top}  (planted: {[metrics[0], metrics[5]]})")
    ok = corr > 0.9 and acc > 0.8
    print("  PASS" if ok else "  FAIL: the fitting machinery does not recover a known objective")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", type=Path, default=Path("build/camcal/session1"))
    ap.add_argument("--verdicts", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--synthetic", action="store_true", help="self-test on generated data")
    ap.add_argument(
        "--allow-stale", action="store_true", help="fit despite a session stamp mismatch"
    )
    args = ap.parse_args(argv)

    if args.synthetic:
        return synthetic()
    if not args.verdicts:
        ap.error("--verdicts is required unless --synthetic")

    key = json.loads((args.session / "trial_key.json").read_text(encoding="utf-8"))
    payload = json.loads(args.verdicts.read_text(encoding="utf-8"))
    votes = payload.get("votes", payload)

    # Refuse verdicts recorded against a different build of the trial set. Trial
    # ids are positional, so a stale file looks valid and silently attaches every
    # verdict to the wrong pair of images.
    expected = next((e.get("stamp") for e in key if e.get("stamp")), None)
    found = payload.get("stamp") if isinstance(payload, dict) else None
    if expected and found != expected:
        print(f"  session stamp mismatch: verdicts say {found!r}, trials say {expected!r}")
        print("  These verdicts were recorded against a different build of the session.")
        print("  Trial ids are positional, so fitting them would attach every verdict")
        print("  to the wrong pair. Re-judge, or pass --allow-stale if you are certain.")
        if not args.allow_stale:
            return 1
    elif expected and found == expected:
        print(f"  session stamp {found} matches")

    ceiling, ceiling_n = self_agreement(key, votes)
    catch = [e for e in key if e["kind"] == "catch" and e["id"] in votes]
    catch_tie = sum(1 for e in catch if votes[e["id"]]["choice"] == "tie")
    print(f"  {len(votes)} verdicts recorded")
    print(f"  catch trials: {len(catch)} judged, {catch_tie} called a tie")
    if catch and catch_tie > len(catch) / 2:
        print("  WARNING: most catch trials were called ties — treat this session as suspect")

    metrics = usable_metrics(key, votes, CANDIDATE_METRICS)
    missing = [m for m in CANDIDATE_METRICS if m not in metrics]
    if missing:
        print(f"  dropping {len(missing)} metric(s) absent from some trials: {', '.join(missing)}")
    if not metrics:
        print("  no metric is present across all trials")
        return 1
    x_all, labels, groups, _kinds, _ids = build_dataset(key, votes, metrics)
    if len(x_all) == 0:
        print("  no usable trials")
        return 1
    ties = (labels == "tie") | (labels == "both_bad")
    n_tie = int((labels == "tie").sum())
    n_bad = int((labels == "both_bad").sum())
    print(f"  {n_tie} ties, {n_bad} both-unusable, {int((~ties).sum())} decided")

    # "Both unusable" is not a tie. A tie says the two are equally good; this
    # says neither is shippable, which is a statement about absolute quality
    # rather than about ranking. Neither carries a direction, so neither can
    # train the sign — but this one identifies settings to remove from the
    # search space outright, which a ranking never would.
    if n_bad:
        rejected: dict[str, int] = {}
        seen: dict[str, int] = {}
        for entry in key:
            vote = votes.get(entry["id"])
            if not vote:
                continue
            for side in ("left_tag", "right_tag"):
                seen[entry[side]] = seen.get(entry[side], 0) + 1
                if vote["choice"] == "both_bad":
                    rejected[entry[side]] = rejected.get(entry[side], 0) + 1
        ranked = sorted(rejected.items(), key=lambda kv: -kv[1] / max(seen[kv[0]], 1))
        print()
        print("  settings you called unusable (share of the trials they appeared in):")
        for tag, n in ranked[:10]:
            print(f"    {tag:26s} {n}/{seen[tag]}  ({100 * n / seen[tag]:.0f} %)")

    scale = np.maximum(x_all.std(0), 1e-9)
    x = x_all[~ties] / scale
    y = (labels[~ties] == "right").astype(int)
    result = report(x, y, groups[~ties], metrics, scale, ceiling, ceiling_n)

    # Does the objective know a tie when it sees one? If ties sit as far from
    # zero as decided pairs, it has fitted something other than what was seen.
    if result and ties.sum() >= 5:
        model = fit(x, y, result["C"])
        z_tie = np.abs(x_all[ties] / scale @ model.coef_[0])
        z_dec = np.abs(x @ model.coef_[0])
        print(
            f"\n  |score| on ties {z_tie.mean():.2f} vs decided {z_dec.mean():.2f}"
            f"  (ties should be smaller)"
        )

    if result:
        out = args.out or args.session / "objective.json"
        out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(f"\n  objective -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
