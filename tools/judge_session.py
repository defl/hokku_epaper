#!/usr/bin/env python3
"""Blind pairwise judging of photographed candidates, recorded as fittable data.

The point of this session is not to pick a winner. It is to produce verdicts a
*metric* can be scored against, so the objective the search optimises is one
measured against a human rather than asserted by whoever wrote the weights. Three
metrics in this project have already been overturned by eye, and the existing
auto-tuner's weights (`dither_search.PROFILE_WEIGHTS`) were never fitted to
anything — its own comment calls them "the opinion in this tool".

The methodology follows `ab_session.py`, which got it right:

  **Blind.** Sides are assigned by seeded coin flip and never shown. The prompt
  says nothing about what differs. A difference visible only once pointed at is
  not a difference worth shipping.

  **"Can't tell" is a real answer.** Ties are the most informative trials there
  are — a candidate objective must predict a *small* difference where none was
  seen. Forcing a binary choice injects coin flips exactly where candidate
  metrics are separated.

  **Repeats.** A share of trials return later with the sides swapped. Agreement
  with yourself is the ceiling any metric can possibly score, and without it
  "the metric is bad" cannot be distinguished from "these look the same".

  **Catch trials.** A few pairs differ blatantly. Missing them means the session
  was fatigued and the data should be discarded — better known before analysis
  than after.

Two departures, both deliberate. `ab_session` bypasses the tonal chain to avoid
confounding the dither arms it compares; here the whole config *is* the thing
being judged, so the full pipeline runs. And trials are composed from
already-photographed candidates rather than driving the panel, so panel time
scales with candidates while trials come free.

Pairs are chosen to spread over the *difference* between candidates, since a
preference model is fitted on those differences: sampling pairs that all differ
the same way would leave most of the fit unconstrained however many were judged.

    python tools/judge_session.py plan  --session build/camcal/session1
    # ... open build/camcal/session1/judge.html, judge, click Export ...
    python tools/fit_preference.py --session build/camcal/session1 \
        --verdicts ~/Downloads/verdicts.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from itertools import combinations
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from session_plan import SPREAD_METRICS

REPEAT_FRACTION = 0.15
CATCH_TRIALS = 5


def load_candidates(session: Path, plan_path: Path) -> list[dict]:
    """Plan entries whose capture actually landed on disk."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    out = []
    for entry in plan["candidates"]:
        if (session / f"{entry['tag']}__measured.png").exists():
            out.append(entry)
    return out


def common_valid_box(session: Path, tags: list[str]) -> tuple[int, int, int, int]:
    """The rectangle covered by every capture, so the warp border is cropped off.

    Rectifying a photograph into panel coordinates leaves a border wherever the
    camera did not quite cover the panel. It is consistent across captures so it
    would not bias a comparison, but it wastes screen area and reads as part of
    the picture. Measured from the captures rather than hard-coded, so it stays
    correct if the rig is moved.

    The border is not black — the colour fit maps no-signal to about RGB
    (0, 0, 17) — so the test is against a real threshold rather than zero. And
    the box is taken from per-row extents rather than "rows that are entirely
    valid": the side borders appear in every single row, so the latter test
    excludes the whole image.
    """
    import cv2  # noqa: PLC0415 — only the plan path needs it

    valid = None
    for tag in tags:
        img = cv2.imread(str(session / f"{tag}__measured.png"))
        if img is None:
            continue
        ok = img.max(axis=2) > 25
        valid = ok if valid is None else (valid & ok)
    if valid is None:
        return (0, 0, 0, 0)

    height, width = valid.shape

    def extent(mask: np.ndarray) -> tuple[int, int]:
        """Inner edges that hold for ~90 % of lines, ignoring blank ones."""
        firsts, lasts = [], []
        for line in mask:
            hits = np.flatnonzero(line)
            if len(hits) > 0.5 * len(line):
                firsts.append(hits[0])
                lasts.append(hits[-1])
        if not firsts:
            return 0, mask.shape[1]
        return int(np.percentile(firsts, 90)), int(np.percentile(lasts, 10))

    x0, x1 = extent(valid)
    y0, y1 = extent(valid.T)
    pad = 6
    x0, y0 = min(x0 + pad, width // 4), min(y0 + pad, height // 4)
    x1, y1 = max(x1 - pad, 3 * width // 4), max(y1 - pad, 3 * height // 4)
    return x0, y0, x1, y1


def library_weights(images: list[str], features_path: Path) -> dict[str, float]:
    """How much of the library each session image stands for.

    The session's images were chosen to span the library, which deliberately
    over-samples unusual pictures — and then allocating trials evenly across them
    over-samples those unusual cases again. Measured on the first session:
    near-greyscale sources took 16 % of trials while being 3 % of the library, so
    a sixth of the judging was spent on photographs whose colour decisions barely
    exist.

    Each library image is assigned to its nearest session image in standardised
    feature space; the share of the library landing on a session image is that
    image's weight. Trials are then allocated in proportion, so the session's
    composition mirrors the library the fitted model has to generalise to.
    """
    import pandas as pd  # noqa: PLC0415 — only this path needs it

    frame = pd.read_csv(features_path)
    cols = ["mean_l", "mean_c", "oog_frac", "detail_energy"]
    frame = frame.dropna(subset=cols)
    names = [str(n) for n in frame["name"]]
    raw = frame[cols].to_numpy(dtype=float)
    norm = (raw - raw.mean(0)) / np.maximum(raw.std(0), 1e-9)
    index = {n: i for i, n in enumerate(names)}
    picked = [n for n in images if n in index]
    if not picked:
        return dict.fromkeys(images, 1.0 / max(len(images), 1))
    anchors = norm[[index[n] for n in picked]]
    nearest = np.argmin(np.linalg.norm(norm[:, None, :] - anchors[None, :, :], axis=2), axis=1)
    counts = np.bincount(nearest, minlength=len(picked)).astype(float)
    counts /= counts.sum()
    weights = dict(zip(picked, counts, strict=True))
    return {n: weights.get(n, 0.0) for n in images}


def plausible(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split candidates into ones worth judging and ones that are simply broken.

    Selecting trials purely for spread in metric space picks the most *different*
    render, which is reliably the most *broken* one. Measured on the first
    session: `default_bw` applied to colour photographs was the single most-used
    candidate — 41 of 170 trial sides — at 156 degrees of skin hue error, a
    complete inversion. Nearly half the session was therefore "a normal picture
    against an obviously destroyed one", which teaches a preference model almost
    nothing: the answer is never in doubt, and the huge metric delta drags the
    fit toward an easy case nobody would ever ship.

    So a candidate has to be a plausible thing to ship before it can appear in a
    normal trial. The gate is relative to that image's own baseline rather than
    absolute, because how much hue error is normal depends on the picture — the
    shipped config itself ranges from 8 to 28 degrees across this library.

    Broken candidates are still returned separately: they are exactly what catch
    trials need.
    """
    base = {
        c["image_name"]: c.get("metrics", {}) for c in candidates if c["config_tag"] == "baseline"
    }
    ok, broken = [], []
    for entry in candidates:
        metrics = entry.get("metrics") or {}
        reference = base.get(entry["image_name"], {})
        bad = False
        for key, margin in (("skin_dhue", 15.0), ("yn_dhue", 25.0)):
            got, ref = metrics.get(key), reference.get(key)
            if got is not None and ref is not None and got > ref + margin:
                bad = True
        (broken if bad else ok).append(entry)
    return ok, broken


def build_trials(
    candidates: list[dict],
    count: int,
    seed: int,
    weights: dict[str, float] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Pairs within each image, spread over how the two candidates differ.

    Returns (trials, key). The key holds which side is which and never reaches
    the judging page.
    """
    # Seeded and reproducible on purpose: a session must be reconstructible
    # from its seed. Not a security context.
    rng = random.Random(seed)  # noqa: S311
    candidates, broken = plausible(candidates)
    if broken:
        from collections import Counter  # noqa: PLC0415 — reporting only

        tally = Counter(e["config_tag"] for e in broken)
        print(f"  excluding {len(broken)} broken candidate(s) from normal trials:")
        for tag, n in tally.most_common(6):
            print(f"    {tag:26s} x{n}")
    by_image: dict[str, list[dict]] = {}
    for entry in candidates:
        by_image.setdefault(entry["image_name"], []).append(entry)

    # Metric availability varies by image — a region mask too small to measure
    # drops its metrics — so the usable set is intersected across every
    # candidate first. Per-image sets would give delta vectors of different
    # lengths, which cannot be compared to each other at all.
    everything = [e for e in candidates if e.get("metrics")]
    available = [k for k in SPREAD_METRICS if all(k in e["metrics"] for e in everything)]
    if not available:
        return [], []

    pairs: list[tuple[dict, dict, np.ndarray]] = []
    for entries in by_image.values():
        usable = [e for e in entries if e.get("metrics")]
        if len(usable) < 2:
            continue
        raw = np.array([[e["metrics"][k] for k in available] for e in usable], dtype=float)
        # Scaled per image: what counts as a big difference in lightness depends
        # on the picture, and pooling the scale would let one high-contrast image
        # dominate the selection for every other.
        scale = np.maximum(raw.std(0), 1e-9)
        for i, j in combinations(range(len(usable)), 2):
            pairs.append((usable[i], usable[j], (raw[i] - raw[j]) / scale))

    if not pairs:
        return [], []

    # Spread over the delta vectors, so the fit is constrained in many directions
    # rather than many times in one. Sign is arbitrary per pair, so compare on
    # magnitude — otherwise a pair and its mirror look maximally different.
    deltas = np.array([np.abs(d) for _a, _b, d in pairs])
    magnitude = np.linalg.norm(deltas, axis=1)
    owner = [a["image_name"] for a, _b, _d in pairs]

    # Every photographed image contributes at least two trials before diversity
    # takes over. Pure farthest-point selection left one image out entirely and
    # gave another eleven trials against two elsewhere — which wastes captures
    # that cost panel time, and costs a fold, since validation is
    # leave-one-*image*-out and the number of images is the number of folds.
    # Trials per image in proportion to how much of the library that image
    # stands for, with a floor of two so every photographed image contributes a
    # fold. Without the proportionality the rare, extreme pictures that diversity
    # selection favours end up dominating what gets judged.
    # Give every image a floor of two first, then share out what is left in
    # proportion to library weight. Scaling quotas and truncating instead drops
    # whichever images sort last, which silently cost three folds.
    names = sorted(set(owner))
    floor = 2
    quota = dict.fromkeys(names, floor)
    spare = count - floor * len(names)
    if spare > 0:
        total = sum(weights.get(n, 0.0) for n in names) if weights else 0.0
        for name in names:
            share = (weights[name] / total) if (weights and total > 0) else 1.0 / len(names)
            quota[name] += int(share * spare)

    chosen: list[int] = []
    for name in names:
        mine = [i for i, o in enumerate(owner) if o == name]
        mine.sort(key=lambda i: -magnitude[i])
        chosen.extend(mine[: quota[name]])

    # Fill the rest by maximising distance from everything already chosen.
    if len(chosen) < count:
        dist = np.min(
            np.linalg.norm(deltas[None, :, :] - deltas[chosen][:, None, :], axis=2), axis=0
        )
        while len(chosen) < min(count, len(pairs)):
            k = int(np.argmax(dist))
            if k in chosen:
                break
            chosen.append(k)
            dist = np.minimum(dist, np.linalg.norm(deltas - deltas[k], axis=1))

    trials, key = [], []
    for n, index in enumerate(chosen):
        left, right = pairs[index][0], pairs[index][1]
        if rng.random() < 0.5:
            left, right = right, left
        tid = f"t{n:03d}"
        trials.append(
            {
                "id": tid,
                "image": left["image_name"],
                "left": f"{left['tag']}__measured.png",
                "right": f"{right['tag']}__measured.png",
                "kind": "normal",
            }
        )
        key.append(
            {
                "id": tid,
                "image": left["image_name"],
                "left_tag": left["config_tag"],
                "right_tag": right["config_tag"],
                "left_metrics": left["metrics"],
                "right_metrics": right["metrics"],
                "kind": "normal",
            }
        )

    # Catch trials: the most extreme pairs available. A judge who cannot call
    # these consistently was not looking, and the session should be discarded.
    order = np.argsort(-magnitude)
    for n, index in enumerate(order[:CATCH_TRIALS]):
        left, right = pairs[index][0], pairs[index][1]
        if rng.random() < 0.5:
            left, right = right, left
        tid = f"c{n:03d}"
        trials.append(
            {
                "id": tid,
                "image": left["image_name"],
                "left": f"{left['tag']}__measured.png",
                "right": f"{right['tag']}__measured.png",
                "kind": "catch",
            }
        )
        key.append(
            {
                "id": tid,
                "image": left["image_name"],
                "left_tag": left["config_tag"],
                "right_tag": right["config_tag"],
                "left_metrics": left["metrics"],
                "right_metrics": right["metrics"],
                "kind": "catch",
            }
        )

    # Repeats, sides swapped, ids pointing back at the original.
    repeats = rng.sample(range(len(chosen)), max(1, int(REPEAT_FRACTION * len(chosen))))
    for n, which in enumerate(repeats):
        original = trials[which]
        tid = f"r{n:03d}"
        trials.append(
            {
                "id": tid,
                "image": original["image"],
                "left": original["right"],
                "right": original["left"],
                "kind": "repeat",
            }
        )
        key.append(
            {
                "id": tid,
                "image": original["image"],
                "repeat_of": original["id"],
                "left_tag": key[which]["right_tag"],
                "right_tag": key[which]["left_tag"],
                "left_metrics": key[which]["right_metrics"],
                "right_metrics": key[which]["left_metrics"],
                "kind": "repeat",
            }
        )

    paired = list(zip(trials, key, strict=True))
    rng.shuffle(paired)
    return [t for t, _ in paired], [k for _, k in paired]


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Hokku judging session</title>
<style>
 :root { color-scheme: dark; }
 body { margin:0; background:#111; color:#ddd; font:14px system-ui,sans-serif; }
 header { padding:10px 16px; display:flex; gap:16px; align-items:center;
          border-bottom:1px solid #333; position:sticky; top:0; background:#111; z-index:2 }
 .grow { flex:1 }
 .pair { display:flex; gap:10px; padding:10px; }
 .pair figure { flex:1; margin:0; text-align:center }
 .pair img { width:100%; height:auto; border:1px solid #333; border-radius:4px; cursor:zoom-in }
 button { background:#222; color:#ddd; border:1px solid #444; border-radius:5px;
          padding:9px 16px; font-size:14px; cursor:pointer }
 button:hover { background:#2c2c2c }
 button.pick { min-width:132px }
 button.bad { border-color:#7a3a3a; color:#e0b0b0 }
 button.bad:hover { background:#3a2424 }
 kbd { background:#000; border:1px solid #444; border-radius:3px; padding:1px 5px }
 #src { display:none; padding:0 10px 10px }
 #src img { max-width:100%; border:1px solid #333; border-radius:4px }
 .done { padding:40px; text-align:center; font-size:16px; line-height:1.7 }
 .zoom { position:fixed; inset:0; background:#000e; display:none; z-index:9;
         align-items:center; justify-content:center }
 .zoom img { max-width:98vw; max-height:98vh }
</style>
<header>
  <b>Which looks better?</b>
  <button class="pick" onclick="vote('left')">Left <kbd>&larr;</kbd></button>
  <button class="pick" onclick="vote('tie')">Can't tell <kbd>space</kbd></button>
  <button class="pick" onclick="vote('right')">Right <kbd>&rarr;</kbd></button>
  <button class="pick bad" onclick="vote('both_bad')">Both unusable <kbd>x</kbd></button>
  <span class="grow"></span>
  <button onclick="toggleSrc()">Show original <kbd>s</kbd></button>
  <button onclick="back()">Undo <kbd>u</kbd></button>
  <span id="prog"></span>
  <button onclick="save()"><b>Export</b></button>
</header>
<div class="pair">
  <figure><img id="L" onclick="zoom(this.src)"></figure>
  <figure><img id="R" onclick="zoom(this.src)"></figure>
</div>
<div id="src"><img id="S"></div>
<div class="zoom" id="Z" onclick="this.style.display='none'"><img id="ZI"></div>
<script>
const TRIALS = __TRIALS__;
const STAMP = "__STAMP__";
// Keyed on the trial content, not the session name: rebuilding the trial set
// must not silently resume on top of votes cast against the previous one.
const KEY = "hokku_judge___SESSION___" + STAMP;
let votes = {}, i = 0;
try { votes = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
function persist() { try { localStorage.setItem(KEY, JSON.stringify(votes)); } catch (e) {} }
function firstUnanswered() {
  let n = 0; while (n < TRIALS.length && votes[TRIALS[n].id]) n++; return n;
}
function show() {
  i = Math.min(i, TRIALS.length);
  if (i >= TRIALS.length) {
    document.querySelector(".pair").innerHTML =
      '<div class="done">All ' + TRIALS.length + ' trials judged.<br>' +
      'Click <b>Export</b> and give the downloaded file to Claude.</div>';
    document.getElementById("src").style.display = "none";
    document.getElementById("prog").textContent = TRIALS.length + "/" + TRIALS.length;
    return;
  }
  const t = TRIALS[i];
  document.getElementById("L").src = t.left;
  document.getElementById("R").src = t.right;
  document.getElementById("S").src = "source__" + t.image + ".png";
  document.getElementById("prog").textContent = (i + 1) + "/" + TRIALS.length;
}
function vote(choice) {
  if (i >= TRIALS.length) return;
  votes[TRIALS[i].id] = { choice: choice, t: Date.now() };
  persist(); i++; show();
}
function back() { if (i > 0) { i--; delete votes[TRIALS[i].id]; persist(); show(); } }
function toggleSrc() {
  const s = document.getElementById("src");
  s.style.display = s.style.display === "block" ? "none" : "block";
}
function zoom(src) {
  document.getElementById("ZI").src = src;
  document.getElementById("Z").style.display = "flex";
}
function save() {
  const blob = new Blob([JSON.stringify({ stamp: STAMP, votes: votes }, null, 1)],
                        { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "verdicts.json";
  a.click();
}
addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") vote("left");
  else if (e.key === "ArrowRight") vote("right");
  else if (e.key === " ") { e.preventDefault(); vote("tie"); }
  else if (e.key === "x") vote("both_bad");
  else if (e.key === "u") back();
  else if (e.key === "s") toggleSrc();
});
i = firstUnanswered();
show();
</script>
"""


def cmd_plan(args) -> int:
    session: Path = args.session
    candidates = load_candidates(session, args.plan)
    if len(candidates) < 2:
        print(f"  only {len(candidates)} captured candidates under {session} — nothing to judge")
        return 1
    weights = library_weights(sorted({c["image_name"] for c in candidates}), args.features_csv)
    trials, key = build_trials(candidates, args.trials, args.seed, weights)
    if not trials:
        print("  could not form any pairs")
        return 1

    import cv2  # noqa: PLC0415 — only the plan path needs it

    tags = sorted({c["tag"] for c in candidates})
    x0, y0, x1, y1 = common_valid_box(session, tags)
    print(f"  cropping captures to the region every one covers: ({x0},{y0})-({x1},{y1})")
    for tag in tags:
        out = session / f"{tag}__view.png"
        if out.exists():
            continue
        img = cv2.imread(str(session / f"{tag}__measured.png"))
        if img is not None:
            cv2.imwrite(str(out), img[y0:y1, x0:x1])
    for trial in trials:
        trial["left"] = trial["left"].replace("__measured.png", "__view.png")
        trial["right"] = trial["right"].replace("__measured.png", "__view.png")

    # A fingerprint of what is actually being judged. Trial ids are positional
    # (t000, t001, ...), so rebuilding the set reuses them for different pairs —
    # and a verdict file from the previous build then looks perfectly valid while
    # pointing at the wrong images. That happened once, mid-session, and cost 17
    # verdicts before anything noticed. The stamp travels into the page, into
    # localStorage's key, and out through the export, so the mismatch is caught.
    stamp = hashlib.sha256(
        json.dumps([(t["id"], t["left"], t["right"]) for t in trials]).encode()
    ).hexdigest()[:12]
    for entry in key:
        entry["stamp"] = stamp
    (session / "trial_key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")

    page = (
        PAGE.replace("__TRIALS__", json.dumps(trials))
        .replace("__SESSION__", session.name)
        .replace("__STAMP__", stamp)
    )
    (session / "judge.html").write_text(page, encoding="utf-8")
    print(f"  session stamp {stamp}")

    # Source references, for the optional "show original" toggle. Off by default:
    # the target is the best-looking picture, not the most faithful one, and
    # showing the original first would quietly turn every verdict into a
    # fidelity judgement.
    from PIL import Image  # noqa: PLC0415 — only the plan path needs it

    from cam_compare import source_canvas  # noqa: PLC0415
    from hokku.screens.registry import DISPLAY_REGISTRY  # noqa: PLC0415

    display = DISPLAY_REGISTRY[args.model]
    for name in sorted({t["image"] for t in trials}):
        out = session / f"source__{name}.png"
        if out.exists():
            continue
        entry = next(c for c in candidates if c["image_name"] == name)
        canvas = source_canvas(Path(entry["image"]), display, "x")
        # Same crop as the captures: the source canvas is already in panel
        # visual coordinates, so the identical box lines the two up.
        Image.fromarray(canvas[y0:y1, x0:x1]).save(out)

    counts = {k: sum(1 for t in trials if t["kind"] == k) for k in ("normal", "catch", "repeat")}
    print(f"  {len(candidates)} captured candidates")
    print(
        f"  {len(trials)} trials: {counts['normal']} normal, "
        f"{counts['catch']} catch, {counts['repeat']} repeat"
    )
    print(f"  open {session / 'judge.html'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="compose trials from captured candidates")
    p.add_argument("--session", type=Path, default=Path("build/camcal/session1"))
    p.add_argument("--plan", type=Path, default=Path("build/camcal/session1_plan.json"))
    p.add_argument("--model", default="huessen_epf1301")
    p.add_argument("--trials", type=int, default=65)
    p.add_argument("--features-csv", type=Path, default=Path("build/camcal/library_features.csv"))
    p.add_argument("--seed", type=int, default=11)
    p.set_defaults(func=cmd_plan)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
