#!/usr/bin/env python3
"""Recompute stored metrics for an existing session, keeping verdicts valid.

The metric bank grows as the judging notes reveal what was missing — face-region
ink fractions were added because "blue lips" and "faces WAY too red" were among
the commonest complaints while the only blue metrics used a hue mask that fires
on brick as readily as on skin. Those new metrics have to reach the trials that
were already judged, or the verdicts cannot be re-fitted against them.

What must NOT change is the pairing. A verdict is a statement about two specific
photographs; rebuilding the trial list would silently re-point every one of them,
which has already happened once here and cost 17 verdicts. So this rewrites only
the metric dictionaries, in place, keyed by candidate tag — trial ids, sides,
kinds, repeat links and the session stamp are all preserved untouched, and the
stamp is verified afterwards to prove it.

    python tools/refresh_metrics.py --plan build/camcal/session40_plan.json \\
        --session build/camcal/session1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_config import image_config_from_dict_strict
from render_bank import close_pool, evaluate_many


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--plan", type=Path, default=Path("build/camcal/session40_plan.json"))
    ap.add_argument("--session", type=Path, default=Path("build/camcal/session1"))
    ap.add_argument("--imagedir", type=Path, default=Path("build/camcal/server_images"))
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--cache", type=Path, default=None, help="omit to force a fresh compute")
    args = ap.parse_args(argv)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    candidates = plan["candidates"]
    jobs = [
        (args.imagedir / c["image_name"], image_config_from_dict_strict(c["config"]))
        for c in candidates
    ]
    print(f"  recomputing metrics for {len(jobs)} candidates")
    results = evaluate_many(jobs, args.model, args.workers or None, args.cache, verbose=True)

    fresh: dict[tuple[str, str], dict] = {}
    failed = 0
    for entry, metrics in zip(candidates, results, strict=True):
        if not metrics or "error" in metrics:
            failed += 1
            continue
        entry["metrics"] = metrics
        # Keyed by (image, config tag), because that is what the trial key
        # records. Keying by the plan's unique candidate tag instead matched
        # nothing at all, and the rewrite then silently left every trial on its
        # old metrics while reporting success.
        fresh[(entry["image_name"], entry["config_tag"])] = metrics
    print(f"  {len(fresh)} refreshed, {failed} failed")
    if failed:
        print("  refusing to rewrite with gaps — investigate the failures first")
        return 1

    args.plan.write_text(json.dumps(plan, indent=1), encoding="utf-8")

    key_path = args.session / "trial_key.json"
    if key_path.exists():
        key = json.loads(key_path.read_text(encoding="utf-8"))
        stamp_before = {e.get("stamp") for e in key}
        ids_before = [e["id"] for e in key]
        sides_before = [(e["left_tag"], e["right_tag"]) for e in key]
        missing = 0
        for entry in key:
            for side in ("left", "right"):
                got = fresh.get((entry["image"], entry[f"{side}_tag"]))
                if got is None:
                    missing += 1
                else:
                    entry[f"{side}_metrics"] = got
        if missing:
            print(f"  {missing} of {2 * len(key)} trial sides found no refreshed metrics")
            print("  Refusing to write: stale metrics that look fresh are worse than none.")
            return 1
        # Prove the pairing survived: anything else would invalidate the verdicts.
        assert [e["id"] for e in key] == ids_before, "trial ids changed"
        assert [(e["left_tag"], e["right_tag"]) for e in key] == sides_before, "pairing changed"
        assert {e.get("stamp") for e in key} == stamp_before, "session stamp changed"
        key_path.write_text(json.dumps(key, indent=1), encoding="utf-8")
        print(f"  trial key updated: {len(key)} trials, pairing and stamp unchanged")

    sample = next(iter(fresh.values()))
    print(f"  {len(sample)} metrics per candidate")
    new = sorted(k for k in sample if k.startswith("face") or "contrast_ratio" in k)
    print(f"  including {len(new)} new: {', '.join(new)}")
    close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
