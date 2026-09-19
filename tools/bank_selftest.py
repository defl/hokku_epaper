#!/usr/bin/env python3
"""Check the metric bank is reproducible, and find the floor it can resolve.

Three questions, in the order they matter.

**Is a measurement repeatable?** The shipped renderer draws its ``dither_noise``
from numpy's global unseeded RNG (`image_renderer.py:800`), so two renders of one
image and config are never identical. The bank seeds that RNG per (image, config)
so a score is a function of its inputs. If this check fails, every cached number
and every search comparison is built on sand.

**How much does the noise actually move a score?** With the seed released, the
same config is scored repeatedly. The spread is the floor: two configs closer
than this are indistinguishable to the pipeline itself, and a search that
prefers one over the other is chasing a coin flip.

**How fast?** Throughput decides how large a search is affordable, so it is
measured rather than assumed.

    python tools/bank_selftest.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import config_space
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from render_bank import evaluate, evaluate_many

IMAGES = Path("build/camcal/server_images")
REPORT = (
    "yn_de00",
    "yn_adapted_dL",
    "yn_dC",
    "yn_dhue",
    "detail_l",
    "detail_c",
    "chroma_use",
    "ink_neutral_leak",
    "ink_error_roughness",
)


def main() -> int:
    images = sorted(p for p in IMAGES.iterdir() if p.is_file())
    cfg = PRESET_IMAGE_CONFIGS["default_general"]
    probe = images[0]

    # 1. seeded reproducibility — must be exact, not merely close
    first = evaluate(probe, cfg)
    second = evaluate(probe, cfg)
    drift = max(
        abs(second[k] - first[k]) / max(abs(first[k]), 1e-6)
        for k in first
        if np.isfinite(first[k]) and np.isfinite(second.get(k, np.nan))
    )
    print(f"  seeded repeatability: worst relative drift {drift:.2e}")
    if drift > 1e-12:
        print("  FAIL: seeding did not make the bank reproducible")
        return 1

    # 2. the noise floor, with the seed released
    repeats = [evaluate(probe, cfg, seed=None) for _ in range(6)]
    print(f"\n  noise floor from {len(repeats)} unseeded renders of one config:")
    print(f"    {'metric':24s} {'mean':>10s} {'sd':>9s} {'sd/|mean|':>10s}")
    floor = {}
    for key in REPORT:
        values = np.array([r[key] for r in repeats if key in r])
        if len(values) < 2:
            continue
        floor[key] = float(values.std())
        rel = values.std() / max(abs(values.mean()), 1e-9)
        print(f"    {key:24s} {values.mean():10.3f} {values.std():9.4f} {rel:10.2%}")

    # 3. how that floor compares with the spread across real candidates
    rng = np.random.default_rng(3)
    candidates = [config_space.sample(cfg, rng, knobs=3) for _ in range(12)]
    scored = [evaluate(probe, c) for c in candidates]
    print("\n  config spread vs that floor (higher is better — below ~2 is unresolvable):")
    for key in REPORT:
        values = np.array([s[key] for s in scored if key in s])
        if len(values) < 2 or key not in floor or floor[key] <= 0:
            continue
        ratio = values.std() / floor[key]
        print(f"    {key:24s} config sd {values.std():8.3f}  = {ratio:6.1f}x floor")

    # 4. throughput
    jobs = []
    for image in images[:8]:
        for _ in range(6):
            jobs.append((image, config_space.sample(cfg, rng, knobs=3)))
    started = time.time()
    results = evaluate_many(jobs, cache_path=None, verbose=False)
    elapsed = time.time() - started
    failed = sum(1 for r in results if "error" in r)
    rate = len(jobs) / elapsed
    print(
        f"\n  throughput: {len(jobs)} uncached evaluations in {elapsed:.1f}s "
        f"= {rate:.1f}/s ({failed} failed)"
    )
    print(f"  a 245-image x 200-candidate search would take ~{245 * 200 / rate / 3600:.1f} h")
    return 0


if __name__ == "__main__":
    sys.exit(main())
