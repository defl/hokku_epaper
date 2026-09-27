#!/usr/bin/env python3
"""Measure the room light's spectrum, so panel appearance can be computed under it.

E-paper is reflective: what the panel looks like is entirely a function of the
light falling on it. The campaign recorded a full 380-730 nm reflectance curve for
every patch, which makes the panel characterisation illuminant-INDEPENDENT — but
turning reflectance into an appearance still needs an illuminant, and D65 is a
standard, not this room.

This closes that gap. With the room's own SPD measured, `color_*` analysis can
predict what the glass looks like *here*, which matters twice over:

  - Any human A/B judgement happens under the room light. Scoring the same
    comparison under D65 compares two different things, and the metric would be
    blamed for the mismatch.
  - It is the first real data point on whether illuminant-adaptive rendering is
    worth building at all. One reading now and one in daylight bounds the effect
    size for the price of two dial rotations.

**Dim-to-warm strips are a moving target.** A DTW LED changes correlated colour
temperature with its dim level by design, so a reading is only valid for the
brightness it was taken at. Record the dimmer setting, and do not touch it between
this measurement and whatever the measurement is used to interpret.

The dial must be in the AMBIENT position (the diffuser swung over the aperture),
pointed the way the panel faces — this measures the light ARRIVING at the panel,
not light leaving it. Rotating the dial invalidates any reflective calibration,
which is why this is best done once the reflective work is finished.

Usage:
  python tools/color_ambient.py --note "evening, DTW strip ~30%"
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from colorimeter import make_instrument

# ``-a`` ambient, replacing the reflective default. ``-s`` keeps the spectrum,
# which is the entire point — an ambient reading without its SPD is just a lux
# number and cannot be used to re-illuminate anything.
#
# ``-N`` skips auto-calibration, which is only correct once a valid ambient
# calibration exists. It does not carry over from the reflective work: that
# calibrates a different measurement mode entirely.
AMBIENT_ARGS = ("-O", "-N", "-a", "-s")

# ``-O`` does one calibration OR one measurement and exits — so an invocation
# that finds itself needing a calibration spends itself on it and never
# measures. Ambient starts with no calibration, so the first invocation must be
# allowed to be that calibration pass (no -N) and to return nothing.
AMBIENT_CAL_ARGS = ("-O", "-a", "-s")


def cct_mccamy(xyz: np.ndarray) -> float | None:
    """Correlated colour temperature via McCamy's cubic approximation.

    Good to a few kelvin near the Planckian locus, which is where a DTW strip
    lives. Reported as a human-readable summary only — every downstream
    calculation uses the SPD itself, never this number.
    """
    s = float(np.sum(xyz))
    if s <= 0:
        return None
    x, y = float(xyz[0]) / s, float(xyz[1]) / s
    # n = (x - 0.3320) / (0.1858 - y). The denominator's sign is not cosmetic:
    # flipping it turns a 2700 K tungsten reading into 10600 K, which is not a
    # small error but a warm light reported as an overcast sky.
    denom = 0.1858 - y
    if abs(denom) < 1e-9:
        return None
    n = (x - 0.3320) / denom
    return 449.0 * n**3 + 3525.0 * n**2 + 6823.3 * n + 5520.33


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("build/colorcal/ambient.jsonl"))
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--note", default="", help="lighting description — dimmer level, time of day")
    ap.add_argument("--timeout-s", type=float, default=60.0)
    ap.add_argument(
        "--skip-calibration",
        action="store_true",
        help="ambient calibration is known fresh — go straight to measuring",
    )
    ap.add_argument(
        "--calibrate-only",
        action="store_true",
        help="do the ambient dark calibration and stop (dial in the CALIBRATION position)",
    )
    args = ap.parse_args(argv)

    # The dial has to be in two different places for the two halves of this, which
    # is why they are separate invocations rather than one prompt-driven run: the
    # calibration is a DARK calibration and needs the aperture against the
    # instrument's own closed calibration position, while the measurement needs
    # the diffuser open to the room. Running the calibration pass with the dial
    # already in the ambient position fails as 'Communications failure', which
    # reads like a USB fault and is not one.
    if args.calibrate_only:
        print("Ambient dark calibration — the dial must be in the CALIBRATION position.\n")
        # Deliberately NOT routed through colorimeter.read(). Under -O a SUCCESSFUL
        # calibration produces no XYZ at all, and read() cannot tell that apart from
        # a failure — worse, it sees spotread's own banner and classifies the
        # success as a calibration error. Matching the success string is what
        # f7_calibrate.py does, and it is the only reliable signal here.
        exe = shutil.which("spotread") or r"C:\Program Files\Argyll\Argyll_V3.5.0\bin\spotread.exe"
        proc = subprocess.run(
            [exe, *AMBIENT_CAL_ARGS],
            capture_output=True,
            text=True,
            timeout=args.timeout_s * 3,
            check=False,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if "calibration complete" not in out.lower():
            print("FAILED — is the dial in the CALIBRATION position?")
            for line in out.strip().splitlines()[-4:]:
                print(f"  | {line}")
            return 1
        print(f"Ambient dark calibration complete at {time.strftime('%H:%M:%S')}.")
        print("It expires in ~1 h. Rotate the dial to AMBIENT and measure NOW:")
        print("  python tools/color_ambient.py --skip-calibration --note '...'")
        return 0

    print("Dial to the AMBIENT position (diffuser over the aperture).")
    print("Hold the meter where the PANEL sits, facing the way the panel faces.\n")

    if not args.skip_calibration:
        # Deliberately tolerant of "no reading": with -O this pass is EXPECTED to
        # consume itself calibrating. Treating that as a failure would abort the
        # run at exactly the moment it succeeded.
        print("calibration pass (expected to produce no reading)...", flush=True)
        cal = make_instrument(
            "spotread", extra_args=list(AMBIENT_CAL_ARGS), timeout_s=args.timeout_s
        )
        cal.prepare()
        if cal.read("ambient calibration") is not None:
            print("  (it measured instead — already calibrated)")
        else:
            print(f"  calibration pass done ({getattr(cal, 'last_error', None) or 'ok'})")

    instrument = make_instrument(
        "spotread", extra_args=list(AMBIENT_ARGS), timeout_s=args.timeout_s
    )
    instrument.prepare()

    raws, specs, waves = [], [], None
    for i in range(args.repeats):
        reading = instrument.read(f"ambient #{i + 1}")
        if reading is None:
            print(f"  ! reading {i + 1} failed ({getattr(instrument, 'last_error', '?')})")
            continue
        raws.append(np.asarray(reading.raw, dtype=float))
        print(f"  raw = {reading.raw}")
        if reading.spectrum is not None and reading.wavelengths is not None:
            specs.append(np.asarray(reading.spectrum, dtype=float))
            waves = [float(v) for v in reading.wavelengths]

    if not raws:
        print("\nNo readings. Is the dial in the ambient position?")
        return 1

    # Median, not mean: a single glitched read should not move the result, and
    # this instrument produces a self-clearing communications failure now and then.
    med = np.median(np.vstack(raws), axis=0)
    rec: dict = {
        "t_unix": time.time(),
        "t_local": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": args.note,
        "repeats": len(raws),
        "raw": [[float(v) for v in r] for r in raws],
        "median_raw": [float(v) for v in med],
    }
    if specs:
        rec["wavelengths_nm"] = waves
        rec["spectrum"] = [[float(v) for v in s] for s in specs]
        rec["median_spectrum"] = [float(v) for v in np.median(np.vstack(specs), axis=0)]

    cct = cct_mccamy(med)
    if cct:
        rec["cct_k_mccamy"] = float(cct)
        print(f"\nCCT ~= {cct:.0f} K")
    if not specs:
        print("\n*** No spectrum returned — this reading CANNOT re-illuminate the panel. ***")
        print("    Check that -s survived into the spotread args.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(f"appended to {args.out}")
    if not args.note:
        print("NOTE: no --note given. A DTW reading without its dimmer setting is not reusable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
