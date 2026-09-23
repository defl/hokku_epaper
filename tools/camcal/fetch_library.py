"""Download the live server's image pool, and pick a spread to photograph.

The pool is the authoritative set: it is what the Foyer actually shows. A local
copy of "the library" drifts the moment a photograph is added or removed, and the
previous copy was lost with its worktree anyway.

Selection is by measured spread, not by taste. Farthest-point over a few cheap
statistics of each photograph — lightness, chroma, how much of it falls outside
the panel's gamut, and how much fine detail it carries — so the set spans easy to
hard instead of clustering on whatever happens to be recent. Seeded with the most
out-of-gamut photograph, because that is the one the pipeline handles worst and
no judging set should omit it by luck.

    python build/camcal/analysis/fetch_library.py --pick 100
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, "tools")
sys.path.insert(0, "python")

from hokku_server import DEFAULT_BASE, fetch_original, pool
from session_plan import farthest_point

CAM = Path("build/camcal")
THUMB = 256


def stats(path: Path) -> dict | None:
    """Cheap descriptors from a thumbnail: enough to spread a selection over."""
    try:
        with Image.open(path) as img:
            img.draft("RGB", (THUMB, THUMB))  # JPEG fast path
            small = img.convert("RGB")
            small.thumbnail((THUMB, THUMB))
            a = np.asarray(small, dtype=np.float32) / 255.0
    except Exception as exc:
        print(f"    unreadable, skipped: {path.name} ({type(exc).__name__}: {exc})")
        return None
    # Rough Lab-ish descriptors; exactness does not matter for a spread.
    lightness = a.mean(-1)
    chroma = a.max(-1) - a.min(-1)
    gy, gx = np.gradient(lightness)
    return {
        "name": path.name,
        "mean_l": float(lightness.mean()),
        "p05_l": float(np.percentile(lightness, 5)),
        "p95_l": float(np.percentile(lightness, 95)),
        "mean_c": float(chroma.mean()),
        "p90_c": float(np.percentile(chroma, 90)),
        # Saturated AND bright is what this panel cannot reach.
        "oog_frac": float(((chroma > 0.35) & (lightness > 0.45)).mean()),
        "detail": float(np.hypot(gy, gx).mean()),
    }


FEATURES = ("mean_l", "p05_l", "p95_l", "mean_c", "p90_c", "oog_frac", "detail")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--dest", type=Path, default=CAM / "server_images")
    ap.add_argument("--pick", type=int, default=100)
    ap.add_argument("--out", type=Path, default=CAM / "selected_images.txt")
    args = ap.parse_args(argv)

    names = pool(args.base)
    print(f"live pool: {len(names)} photographs")

    local = []
    for i, name in enumerate(names, 1):
        try:
            local.append(fetch_original(name, args.dest, args.base))
        except Exception as exc:
            print(f"    download failed, skipped: {name} ({type(exc).__name__})")
        if i % 25 == 0:
            print(f"  downloaded {i}/{len(names)}", flush=True)
    print(f"have {len(local)} files locally")

    rows = [s for s in (stats(p) for p in local) if s]
    print(f"measured {len(rows)}")

    raw = np.array([[r[f] for f in FEATURES] for r in rows], dtype=float)
    norm = (raw - raw.mean(0)) / np.maximum(raw.std(0), 1e-9)
    seed = int(np.argmax(raw[:, FEATURES.index("oog_frac")]))
    picked = farthest_point(norm, min(args.pick, len(rows)), seed)
    chosen = [rows[i]["name"] for i in picked]

    args.out.write_text("\n".join(chosen) + "\n", encoding="utf-8")
    print(f"\nselected {len(chosen)} photographs -> {args.out}")
    sel = raw[picked]
    for i, f in enumerate(FEATURES):
        print(
            f"  {f:10s} selected {sel[:, i].min():6.3f}..{sel[:, i].max():6.3f}"
            f"   whole pool {raw[:, i].min():6.3f}..{raw[:, i].max():6.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
