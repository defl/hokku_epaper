#!/usr/bin/env python3
"""Measure a Bigme F7 panel with the meter CLAMPED IN ONE PLACE.

The grid target in ``color_target.py`` assumes the operator re-aims the
instrument at each of 15 patches. A spectrophotometer resting on the glass
cannot be moved without disturbing the geometry it is measuring — and moving it
between readings is exactly what makes a session unrepeatable. So this flips the
problem: the meter stays put and the PANEL changes, one full-screen field at a
time.

Each field is uploaded over USB with the `frame` protocol (never HTTP — see
AGENTS.md), the panel refreshes, and one reading is taken. 13 fields: six flat
inks, then seven Bayer levels at k/8 black coverage. Roughly a minute each,
fully unattended once it starts.

Output is a JSON manifest+readings pair that ``color_read.assemble()`` consumes,
so the analysis path is shared with the hand-aimed grid route.

Usage:
  python tools/color_measure_f7.py --port COM10 --out build/colorcal/f7_run.json
  python tools/color_measure_f7.py --port COM10 --dry-run   # no instrument
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import serial

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from color_target import fullscreen_sequence
from colorimeter import finalise, make_instrument
from f7_send_frame import send_frame
from hokku.screens.bigme_f7.bootstrap import _open_console
from hokku.screens.registry import DISPLAY_REGISTRY


def catch_console(port: str, deadline_s: float):
    """Poll until the firmware console answers, or give up.

    The console only lives a few seconds per wake unless the unit is pinned with
    `cfg power awake`, which is why this polls rather than opening once.
    """
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        s = _open_console(port, serial)
        if s is not None:
            return s
        time.sleep(0.7)
    return None


def upload_with_retry(s, data: bytes, label: str, attempts: int, gap_s: float) -> bool:
    """Send one frame, retrying while the device's refresh lock is held.

    `frame` takes a try-lock and REFUSES rather than queueing, so a single
    attempt loses to any refresh already in flight.
    """
    for i in range(1, attempts + 1):
        s.reset_input_buffer()
        if send_frame(s, data, label):
            return True
        if i < attempts:
            print(f"    retrying ({i}/{attempts - 1})...", flush=True)
            time.sleep(gap_s)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM10", help="serial port of the F7 console")
    ap.add_argument("--model", default="bigme_f7", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--out", type=Path, default=Path("build/colorcal/f7_measure.json"))
    ap.add_argument("--instrument", default="spotread", choices=("spotread", "manual"))
    ap.add_argument("--repeats", type=int, default=2, help="readings averaged per field")
    ap.add_argument("--settle-s", type=float, default=3.0, help="pause after REFRESHED")
    ap.add_argument("--console-timeout", type=float, default=900.0)
    # A calibrated ColorMunki answers in ~1.5 s, so anything past a few seconds is
    # a hang, not slowness. The 120 s library default would burn ~26 min of dead
    # wait across 13 fields x 2 reads if it recurred.
    ap.add_argument("--instrument-timeout", type=float, default=45.0)
    ap.add_argument("--dry-run", action="store_true", help="display the fields, take no readings")
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    fields = fullscreen_sequence(display)
    print(f"{len(fields)} full-screen fields on {args.model} ({display.panel_w}x{display.panel_h})")

    instrument = None
    if not args.dry_run:
        instrument = make_instrument(args.instrument)
        instrument.prepare()
        print(f"instrument: {instrument.name}")
        # Take one throwaway reading before touching the panel. A ColorMunki that
        # has been power-cycled has no stored calibration, so spotread forces a
        # calibration that fails unless the dial is in the white-tile position —
        # and it fails the SAME way for all 13 fields. Without this check that is
        # 13 uploads and 13 full ~30 s panel refreshes to learn nothing.
        print("preflight: taking one reading to prove the instrument answers...", flush=True)
        if instrument.read("preflight") is None:
            print(
                "\nABORT: the instrument did not return a reading.\n"
                "  Most likely it needs calibrating: put the dial in the CALIBRATION\n"
                "  position, let it calibrate, then return it to MEASUREMENT position\n"
                "  on the glass and re-run. Nothing was displayed or changed."
            )
            return 1
        print("preflight OK", flush=True)

    print(f"catching console on {args.port}...", flush=True)
    s = catch_console(args.port, args.console_timeout)
    if s is None:
        print("ABORT: console never answered. Long-press the power button and re-run.")
        return 1

    results: list[dict] = []
    readings = []
    try:
        for n, (name, kind, coverage, idx) in enumerate(fields, 1):
            data = display.indices_to_panel_bytes(idx)
            print(f"\n[{n}/{len(fields)}] {name}", flush=True)
            if not upload_with_retry(s, data, name, attempts=6, gap_s=8.0):
                print(f"    ! could not display {name} — skipping (no reading taken)")
                results.append(
                    {"name": name, "kind": kind, "coverage": coverage, "error": "upload"}
                )
                continue

            if args.dry_run:
                results.append({"name": name, "kind": kind, "coverage": coverage})
                continue

            # Let the panel settle: e-paper keeps creeping briefly after the
            # driver reports done, and a reading taken too early is measurably
            # off on the dark inks.
            time.sleep(args.settle_s)

            assert instrument is not None
            got = []
            for r in range(args.repeats):
                reading = instrument.read(f"{name} #{r + 1}")
                if reading is None:
                    print(f"    ! no reading for {name} #{r + 1}")
                    continue
                got.append(reading)
                readings.append(reading)
                print(f"    raw XYZ = {reading.raw}")
            results.append(
                {
                    "name": name,
                    "kind": kind,
                    "coverage": coverage,
                    "raw": [list(map(float, g.raw)) for g in got],
                }
            )
    finally:
        s.close()
        print("\n--- serial closed ---", flush=True)

    if readings:
        # Percent-vs-unit scale can only be judged once the brightest field in
        # the set is known, so this is deliberately deferred to the end.
        finalise(readings, illuminant="d65")
        k = 0
        for entry in results:
            if "raw" not in entry:
                continue
            n_got = len(entry["raw"])
            entry["xyz_d65"] = [list(map(float, readings[k + i].xyz_d65)) for i in range(n_got)]
            k += n_got

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"model": args.model, "fields": results}, indent=2), encoding="utf-8"
    )
    print(f"wrote {args.out}")
    ok = sum(1 for r in results if "error" not in r)
    print(
        f"{ok}/{len(fields)} fields displayed"
        + ("" if args.dry_run else f", {len(readings)} readings taken")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
