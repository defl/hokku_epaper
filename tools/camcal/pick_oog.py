#!/usr/bin/env python3
"""Pick photographs for a gamut-correction test, weighted to out-of-gamut content.

A correction LUT only does anything where the requested colour is outside what
six inks on this glass can reach. Judging it on a set chosen for general variety
wastes most of the panel time on photographs where every arm renders almost
identically — which is what happened to several arms in the sixteen-way run.

So: measure how much of each photograph the panel physically cannot reach, using
the campaign's own fitted chroma ceiling rather than a guess, and take a set
weighted towards the hard end while keeping a tail of easy ones. The easy ones
are not padding — if the correction damages photographs that never needed it,
that is exactly the failure worth catching.

    python tools/camcal/pick_oog.py --count 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, "tools")
sys.path.insert(0, "python")

from color_validate_photos import lab_img, srgb_img_to_xyz
from gamut_clamp import lookup_ceiling
from render_bank import load_model

CAM = Path("build/camcal")
THUMB = 320


def oog_fraction(path: Path, ceiling) -> float | None:
    """Share of pixels whose chroma exceeds what the panel can reach there."""
    try:
        with Image.open(path) as img:
            img.draft("RGB", (THUMB, THUMB))
            small = img.convert("RGB")
            small.thumbnail((THUMB, THUMB))
            arr = np.asarray(small, dtype=np.uint8)
    except Exception:
        return None
    lab = lab_img(srgb_img_to_xyz(arr))
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    return float((chroma > lookup_ceiling(ceiling, lab)).mean())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--images", type=Path, default=CAM / "selected_images.txt")
    ap.add_argument("--imagedir", type=Path, default=CAM / "server_images")
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--easy", type=int, default=6, help="how many easy ones to keep")
    ap.add_argument("--out", type=Path, default=CAM / "oog_images.txt")
    args = ap.parse_args(argv)

    ceiling = load_model()["ceiling"]
    names = [n for n in args.images.read_text(encoding="utf-8").splitlines() if n.strip()]
    scored = []
    for i, name in enumerate(names, 1):
        frac = oog_fraction(args.imagedir / name, ceiling)
        if frac is not None:
            scored.append((name, frac))
        if i % 25 == 0:
            print(f"  measured {i}/{len(names)}", flush=True)

    scored.sort(key=lambda r: -r[1])
    hard = scored[: args.count - args.easy]
    # Evenly spaced through the remainder, so the easy tail spans easy-ish to
    # trivial rather than clustering at zero.
    rest = scored[args.count - args.easy :]
    step = max(1, len(rest) // args.easy)
    easy = rest[::step][: args.easy]
    picked = hard + easy

    args.out.write_text("\n".join(n for n, _ in picked) + "\n", encoding="utf-8")
    print(f"\n{len(picked)} photographs -> {args.out}")
    print(f"  hardest {hard[0][1]:.1%} out of gamut, easiest kept {easy[-1][1]:.1%}")
    print(f"  whole set of {len(scored)}: median {np.median([f for _, f in scored]):.1%}")
    print("\n  top of the set:")
    for name, frac in picked[:8]:
        print(f"    {frac:6.1%}  {name[:46]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
