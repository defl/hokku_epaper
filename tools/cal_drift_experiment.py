#!/usr/bin/env python3
"""Does ArgyllCMS's 1-hour dark-calibration timeout actually protect anything?

ArgyllCMS invalidates the ColorMunki's dark calibration after exactly one hour
(``DCALTOUT`` in ``spectro/munki_imp.c``), on elapsed time alone — it measures no
drift and checks no temperature, and the source gives no justification for the
value. On this rig that costs a dial rotation every hour, so a ~19 h campaign
pays ~19 manual interruptions. Worth knowing whether the constant is earning it.

Method: keep the ORIGINAL dark-calibration data and only rewrite its recorded
date, so Argyll keeps measuring with a calibration that is genuinely hours old.
Then read one unchanging patch repeatedly and watch whether the numbers move.

  no drift  -> the hour is over-conservative for this instrument, and the
               campaign could run far longer per calibration
  drift     -> the constant is doing real work, and we learn its magnitude

Confound and how it is handled: e-paper is bistable but not perfectly static, so
a slowly fading panel would look like instrument drift. The same field is
therefore re-uploaded periodically; a fade would show up as a step at each
refresh, while instrument drift would continue smoothly through it.

Black ink is the stimulus because dark-current error is proportionally largest
where there is least signal — if a stale dark calibration hurts anywhere, it
hurts here first.

Everything is reversible: the calibration file is backed up before the first
patch and restored on exit.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import struct
import sys
import time
from pathlib import Path

import numpy as np
import serial

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from color_measure_f7 import catch_console, upload_with_retry
from colorimeter import make_instrument
from hokku.screens.registry import DISPLAY_REGISTRY

CAL_FILE = Path.home() / ".cache" / "ArgyllCMS" / ".mk_2017853.cal"
# Located empirically: two int32 fields both holding the calibration time_t,
# confirmed by matching a calibration we had just performed to the second.
DATE_OFFSETS = (85, 3221)


def read_cal_dates(path: Path) -> list[int]:
    data = path.read_bytes()
    return [struct.unpack_from("<i", data, off)[0] for off in DATE_OFFSETS]


def set_cal_dates(path: Path, when: int) -> None:
    """Rewrite ONLY the recorded date, leaving the calibration data untouched.

    That is the whole point: the instrument keeps using measurements taken hours
    ago, so any change in the readings is the staleness we are trying to detect.
    """
    data = bytearray(path.read_bytes())
    for off in DATE_OFFSETS:
        struct.pack_into("<i", data, off, when)
    path.write_bytes(bytes(data))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("build/colorcal/cal_drift.jsonl"))
    ap.add_argument("--port", default="COM10")
    ap.add_argument("--hours", type=float, default=7.0)
    ap.add_argument("--interval-s", type=float, default=300.0)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--ink", default="black", help="which solid ink to park on the glass")
    ap.add_argument(
        "--refresh-every", type=int, default=12, help="re-upload the field every N reads"
    )
    args = ap.parse_args()

    names = ("black", "white", "yellow", "red", "blue", "green")
    ink_index = names.index(args.ink)
    display = DISPLAY_REGISTRY["bigme_f7"]

    if not CAL_FILE.exists():
        print(f"no calibration file at {CAL_FILE}")
        return 1
    backup = CAL_FILE.with_suffix(".cal.drift_backup")
    if not backup.exists():
        shutil.copy2(CAL_FILE, backup)
    original_dates = read_cal_dates(CAL_FILE)
    true_cal_time = min(original_dates)
    print(f"original calibration date: {time.strftime('%H:%M:%S', time.localtime(true_cal_time))}")
    print(f"backup: {backup}")

    instrument = make_instrument("spotread", timeout_s=45.0)
    instrument.prepare()

    print(f"catching console on {args.port}...", flush=True)
    s = catch_console(args.port, 900.0)
    if s is None:
        print("console never answered — cannot park a field on the glass")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    idx = np.full((display.panel_h, display.panel_w), ink_index, dtype=np.uint8)
    data = display.indices_to_panel_bytes(idx)

    n = 0
    deadline = time.monotonic() + args.hours * 3600
    try:
        with args.out.open("a", encoding="utf-8") as fh:

            def emit(rec: dict) -> None:
                fh.write(json.dumps(rec) + "\n")
                fh.flush()

            print(f"parking {args.ink} on the glass...", flush=True)
            if not upload_with_retry(s, data, args.ink, attempts=6, gap_s=8.0):
                print("could not display the field")
                return 1
            emit({"event": "refresh", "t_unix": time.time(), "ink": args.ink})
            time.sleep(5.0)

            while time.monotonic() < deadline:
                # Re-date the calibration so Argyll will use it. The DATA is the
                # original; only its timestamp moves.
                set_cal_dates(CAL_FILE, int(time.time()))
                age_s = time.time() - true_cal_time

                raws, specs = [], []
                for r in range(args.repeats):
                    reading = instrument.read(f"drift #{n}.{r}")
                    if reading is None:
                        continue
                    raws.append(reading.raw)
                    if reading.spectrum is not None:
                        specs.append(reading.spectrum)
                rec: dict = {
                    "event": "read",
                    "n": n,
                    "t_unix": time.time(),
                    "true_cal_age_s": age_s,
                    "raw": [[float(v) for v in x] for x in raws],
                    "error": None if raws else getattr(instrument, "last_error", "none"),
                }
                if raws:
                    rec["median_raw"] = [float(v) for v in np.median(np.vstack(raws), axis=0)]
                if specs:
                    rec["median_spectrum_pct"] = [
                        float(v) for v in np.median(np.vstack(specs), axis=0)
                    ]
                emit(rec)
                med = rec.get("median_raw")
                print(
                    f"[{n:3d}] cal age {age_s / 3600:5.2f} h  "
                    + (f"XYZ {[round(v, 4) for v in med]}" if med else f"FAIL {rec['error']}"),
                    flush=True,
                )

                n += 1
                # Periodic re-upload: separates a fading panel (a step here) from
                # instrument drift (which would continue smoothly through it).
                if args.refresh_every and n % args.refresh_every == 0:
                    if upload_with_retry(s, data, args.ink, attempts=4, gap_s=8.0):
                        emit({"event": "refresh", "t_unix": time.time(), "ink": args.ink})
                        print("      (field re-uploaded)", flush=True)
                    time.sleep(5.0)

                time.sleep(max(0.0, args.interval_s))
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
    finally:
        with contextlib.suppress(serial.SerialException, OSError):
            s.close()
        # Always put the real calibration file back, whatever happened.
        shutil.copy2(backup, CAL_FILE)
        print(f"restored the original calibration file from {backup}", flush=True)
    print(f"{n} readings -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
