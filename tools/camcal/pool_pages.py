#!/usr/bin/env python3
"""Pool several rating pages into one verdict per rendering.

Pages are judged on different evenings and drift: on page 1 the baseline was
rated "ok or better" 45 % of the time, on page 2 78 %, with no change to the
renders. Absolute scores therefore cannot be pooled. What pools is the *paired
difference within one photograph* — that holds the picture and the sitting fixed,
so page mood cancels out of it.

Reports each rendering against the reference across every page it appeared on,
with a sign test over the comparisons that were decided. Ties are counted and
reported but carry no direction, so they are excluded from the test exactly as
they are from the fit.

    python tools/camcal/pool_pages.py build/camcal/session6/page{1,2}_key.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import binomtest


def page_frame(key_path: Path) -> pd.DataFrame | None:
    """One page as image x arm ratings, from its archived export."""
    export = key_path.parent / f"{key_path.stem.replace('_key', '')}_export.json"
    if not export.exists():
        print(f"  {key_path.name}: no archived export, skipped")
        return None
    key = json.loads(key_path.read_text(encoding="utf-8"))
    got = json.loads(export.read_text(encoding="utf-8"))
    rows = []
    for img in key["images"]:
        rec = got.get("ratings", {}).get(img["image"], {})
        for slot in img["slots"]:
            value = rec.get("r", {}).get(str(slot["slot"]))
            if value is not None:
                rows.append(
                    {
                        "page": key_path.stem,
                        "image": img["image"],
                        "arm": slot["arm"],
                        "rating": int(value),
                    }
                )
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    print(f"  {key_path.stem}: {len(frame)} ratings over {frame.image.nunique()} photographs")
    return frame


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("keys", nargs="+", type=Path)
    ap.add_argument("--ref", default="live")
    args = ap.parse_args(argv)

    frames = [f for f in (page_frame(k) for k in args.keys) if f is not None]
    if not frames:
        print("nothing to pool")
        return 1
    df = pd.concat(frames, ignore_index=True)
    print(
        f"\npooled: {len(df)} ratings, {df.image.nunique()} photographs, {df.page.nunique()} pages"
    )

    # Per-page level, to show why only paired differences are pooled.
    print("\nbaseline level per page (this is why absolute scores do not pool):")
    for page, group in df[df.arm == args.ref].groupby("page"):
        share = (group.rating >= 3).mean()
        print(f"  {page:14s} mean {group.rating.mean():.2f}   ok-or-better {share:.0%}")

    wide = df.pivot_table(index=["page", "image"], columns="arm", values="rating")
    arms = [a for a in wide.columns if a != args.ref]

    print(f"\neach rendering against {args.ref}, paired within a photograph:")
    print(
        f"  {'arm':14s} {'n':>3s} {'better':>7s} {'worse':>6s} {'same':>5s} {'mean':>7s} {'p':>6s}"
    )
    rows = []
    for arm in arms:
        d = (wide[arm] - wide[args.ref]).dropna()
        if d.empty:
            continue
        b, w = int((d > 0).sum()), int((d < 0).sum())
        p = binomtest(b, b + w).pvalue if b + w else 1.0
        rows.append((arm, len(d), b, w, float(d.mean()), p))
    for arm, n, b, w, mean, p in sorted(rows, key=lambda r: -r[4]):
        mark = "  **" if p < 0.01 else ("  *" if p < 0.05 else "")
        print(f"  {arm:14s} {n:3d} {b:7d} {w:6d} {n - b - w:5d} {mean:+7.2f} {p:6.3f}{mark}")

    beat = [r for r in rows if r[4] > 0 and r[5] < 0.05]
    lost = [r for r in rows if r[4] < 0 and r[5] < 0.05]
    print(f"\nrenderings significantly BETTER than {args.ref}: {[r[0] for r in beat] or 'none'}")
    print(f"renderings significantly WORSE:            {[r[0] for r in lost] or 'none'}")

    # How often the baseline is simply the best thing on its own page. Row-wise
    # in numpy so "tied for the top" is explicit rather than an idxmax artefact.
    columns = [str(c) for c in wide.columns]
    ref_index = columns.index(args.ref)
    ref_top, judged_rows = 0, 0
    solo: Counter[str] = Counter()
    for row in wide.to_numpy(dtype=float):
        finite = np.isfinite(row)
        if finite.sum() < 2:
            continue
        judged_rows += 1
        top = float(np.nanmax(row))
        at_top = [i for i, v in enumerate(row) if finite[i] and v == top]
        if ref_index in at_top:
            ref_top += 1
        if len(at_top) == 1:
            solo[columns[at_top[0]]] += 1
    print(f"\n{args.ref} is at or tied for the top on {ref_top} of {judged_rows} photographs")
    print("outright winner, where there was one:", dict(solo))

    total = (
        df.groupby("arm")
        .rating.agg(["count", "mean", lambda s: (s >= 3).mean()])
        .rename(columns={"<lambda_0>": "ok_share"})
    )
    print("\nfor reference only (levels drift between pages, so do not read these as a ranking):")
    print(total.sort_values("mean", ascending=False).round(2).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
