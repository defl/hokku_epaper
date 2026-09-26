#!/usr/bin/env python3
"""Decode a rating export against its page key, and say what it supports.

Replaces `grouprate_report.py`, which was lost when the gamut-lut worktree was
removed. Two things it does that the lost one had to be taught the hard way:

**Positions in notes are translated into arm names**, both the numeric kind
("2 is too light") and the spatial kind ("left is washed out"). The spatial words
were missing at first, and their absence hid a whole round's story — every note
said left/right, the report printed them untranslated, and nine complaints that
all landed on one arm read as scattered remarks.

**The export is archived beside its session.** Every browser download is called
the same thing, so one round's raw export silently overwrote another's and those
notes were lost for good.

Comparisons are always *paired within a photograph*. The absolute level of a page
drifts — the same renders scored 0.57 points apart on two evenings — so only
"this version against that one, on this picture" survives.

Reductions run in numpy over plain dicts rather than by chaining pandas. That is
not style: a chained groupby/idxmax result is opaque to the type checker, and the
arithmetic here is a few dozen numbers over a handful of arms.

    python tools/camcal/rate_report.py <key.json> <export.json> [--ref live]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import binomtest

LABELS = ["terrible", "bad", "meh", "ok", "good"]


def decode_note(note: str, slots: list[dict]) -> str:
    """Rewrite positions — "2", "left" — as the arm that actually sat there."""
    order = [s["arm"] for s in sorted(slots, key=lambda s: s["slot"])]
    by_number = {i + 1: arm for i, arm in enumerate(order)}
    # A digit is a position unless it follows "all"/"these", where it is a count.
    out = re.sub(
        r"(?<!all )(?<!these )(?<![\w.])([1-9])(?![\w.%])",
        lambda m: f"[{by_number.get(int(m.group(1)), m.group(1))}]",
        note,
    )
    spatial = {"left": order[0], "right": order[-1]}
    if len(order) == 3:
        spatial["middle"] = order[1]
    for word, arm in spatial.items():
        out = re.sub(r"\b" + word + r"\b", f"[{arm}]", out, flags=re.IGNORECASE)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("key", type=Path)
    ap.add_argument("export", type=Path)
    ap.add_argument("--ref", default="live", help="the arm everything is compared against")
    args = ap.parse_args(argv)

    key = json.loads(args.key.read_text(encoding="utf-8"))
    got = json.loads(args.export.read_text(encoding="utf-8"))
    if got.get("stamp") != key.get("stamp"):
        print(f"MISMATCH: export stamp {got.get('stamp')} is not this page ({key.get('stamp')})")
        return 1
    print(f"stamp {key['stamp']} MATCHES this page")

    rows, notes = [], {}
    for img in key["images"]:
        rec = got.get("ratings", {}).get(img["image"], {})
        note = (rec.get("note") or "").strip()
        if note:
            notes[img["image"]] = decode_note(note, img["slots"])
        for slot in img["slots"]:
            rows.append(
                {
                    "image": img["image"],
                    "arm": slot["arm"],
                    "rating": rec.get("r", {}).get(str(slot["slot"])),
                }
            )
    df = pd.DataFrame(rows)
    ratings = df["rating"].to_numpy(dtype=float)
    seen_mask = np.isfinite(ratings)
    rated = int(seen_mask.sum())
    print(f"rated {rated} of {len(df)} cells, {df.loc[seen_mask, 'image'].nunique()} photographs")
    if not rated:
        return 0
    print("distribution:", dict(Counter(LABELS[int(v)] for v in ratings[seen_mask])))

    wide = df.pivot(index="image", columns="arm", values="rating")
    columns = [str(c) for c in wide.columns]
    shown = {a: wide[a].to_numpy(dtype=float) for a in columns}
    ref = shown[args.ref]

    print("\nmean rating per version (only where it was actually shown):")
    levels = {a: float(np.nanmean(v)) for a, v in shown.items() if np.isfinite(v).any()}
    for arm in sorted(levels, key=lambda a: -levels[a]):
        n = int(np.isfinite(shown[arm]).sum())
        tail = "   <- baseline" if arm == args.ref else ""
        print(f"  {arm:14s} {levels[arm]:5.2f}   n={n:2d}{tail}")

    print(f"\neach version against {args.ref}, paired on the same photograph:")
    print(
        f"  {'arm':14s} {'n':>3s} {'better':>7s} {'worse':>6s} "
        f"{'same':>5s} {'mean':>7s} {'sign p':>7s}"
    )
    summary = []
    for arm in columns:
        if arm == args.ref:
            continue
        delta = shown[arm] - ref
        delta = delta[np.isfinite(delta)]
        if not delta.size:
            continue
        better, worse = int((delta > 0).sum()), int((delta < 0).sum())
        p = binomtest(better, better + worse).pvalue if better + worse else 1.0
        summary.append((arm, int(delta.size), better, worse, float(delta.mean()), float(p)))
    for arm, n, better, worse, mean, p in sorted(summary, key=lambda r: -r[4]):
        flag = "  *" if p < 0.05 else ""
        same = n - better - worse
        print(f"  {arm:14s} {n:3d} {better:7d} {worse:6d} {same:5d} {mean:+7.2f} {p:7.2f}{flag}")

    winners: Counter[str] = Counter()
    tied = 0
    for row in wide.to_numpy(dtype=float):
        finite = np.isfinite(row)
        if finite.sum() < 2:
            continue
        top = float(np.nanmax(row))
        at_top = [columns[i] for i, v in enumerate(row) if finite[i] and v == top]
        if len(at_top) > 1:
            tied += 1
        else:
            winners[at_top[0]] += 1
    print("\nbest version per photograph:", dict(winners))
    print(f"  ({tied} photographs had a tie at the top)")

    print("\nrated ok or better, as a share of where each was shown:")
    share = {}
    for arm, values in shown.items():
        finite = values[np.isfinite(values)]
        if finite.size:
            share[arm] = (int((finite >= 3).sum()), int(finite.size))
    for arm in sorted(share, key=lambda a: -share[a][0] / share[a][1]):
        good, total = share[arm]
        print(f"  {arm:14s} {good:2d}/{total:2d}  {good / total:4.0%}")

    if notes:
        print("\nnotes, with positions translated:")
        for name, note in notes.items():
            print(f"  {name[:30]:30s} {note}")

    wide.to_csv(args.key.with_name(args.key.stem.replace("_key", "") + "_wide.csv"))
    archive = args.key.parent / f"{args.key.stem.replace('_key', '')}_export.json"
    archive.write_text(json.dumps(got, indent=1), encoding="utf-8")
    print(f"\narchived export -> {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
