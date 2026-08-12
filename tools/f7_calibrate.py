#!/usr/bin/env python3
"""Calibrate the spectrophotometer and record WHEN, so cycles start informed.

ArgyllCMS persists a calibration and `-N` reuses it, but the calibration times
out — measured at roughly 60 minutes on the ColorMunki Photo. Argyll does not
publish the age, and a successful reading proves only that the calibration is
valid *right now*, not that it has any life left. Starting a measurement cycle
on a calibration with three minutes remaining wastes the whole cycle: that is
exactly how one was lost.

So the calibration timestamp is written here, and color_campaign_run reads it and
refuses to start a long cycle on a stale one.

Usage:
  python tools/f7_calibrate.py          # dial must be in the CALIBRATION position
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

STAMP = Path.home() / ".cache" / "ArgyllCMS" / "hokku_last_calibration.json"
# NOT a guess and not a physical property of the instrument: ArgyllCMS's ColorMunki
# driver hardcodes it. From spectro/munki_imp.c:
#
#   #define DCALTOUT (1 * 60 * 60)   /* [1 Hrs] Dark Calibration timeout in seconds */
#   #define WCALTOUT (24 * 60 * 60)  /* [24 Hrs] White Calibration timeout in seconds */
#
#   if ((curtime - cs->ddate) > DCALTOUT) { ... dark_valid = 0; }
#
# So it is the DARK calibration that expires, on elapsed time alone — no drift or
# temperature is measured — and the white tile calibration is good for 24 h. The
# source carries no comment justifying the hour; it is a conservative policy
# constant. Matches observation exactly: valid at 57 min, expired by 62 min.
NOMINAL_LIFETIME_S = 60 * 60
# Stop this far before the deadline so a cycle ends cleanly instead of crashing
# into the expiry and burning patches on reads that cannot succeed.
SAFETY_MARGIN_S = 5 * 60


def stamp_path() -> Path:
    return STAMP


def calibration_age_s() -> float | None:
    """Seconds since the last recorded calibration, or None if never recorded."""
    if not STAMP.exists():
        return None
    try:
        return time.time() - float(json.loads(STAMP.read_text(encoding="utf-8"))["t_unix"])
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def main() -> int:
    exe = shutil.which("spotread") or r"C:\Program Files\Argyll\Argyll_V3.5.0\bin\spotread.exe"
    print("Calibrating — the dial must be in the CALIBRATION position.", flush=True)
    # No -N: this call is the calibration. -O does one cal-or-measure and exits.
    proc = subprocess.run(
        [exe, "-O", "-i", "D65"], capture_output=True, text=True, timeout=180, check=False
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if "calibration complete" not in out.lower():
        print("FAILED — is the dial in the CALIBRATION position?")
        for line in out.strip().splitlines()[-4:]:
            print(f"  | {line}")
        return 1
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(json.dumps({"t_unix": time.time()}), encoding="utf-8")
    print(f"Calibration complete. Recorded at {time.strftime('%H:%M:%S')}.")
    print(f"Expect ~{NOMINAL_LIFETIME_S // 60} minutes of usable measuring — start the run NOW.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
