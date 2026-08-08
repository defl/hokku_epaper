#!/usr/bin/env python3
"""Run a panel-characterisation campaign: display each patch, measure, record.

Consumes a spec from ``color_campaign_spec.py`` and drives the hardware for as
long as it takes — eight hours for the default 521-patch plan. The meter is
clamped and never moves; the panel changes under it, one full-screen field per
patch, uploaded over USB with the `frame` protocol (never HTTP — see AGENTS.md).

Crash-safety is the point of the file format. Results append to a JSONL, one
line per patch, flushed and fsynced immediately. A run that dies at hour six
keeps every patch it measured, and re-running skips the completed uids. Nothing
is held in memory waiting to be written at the end.

Recorded per patch, so any conclusion can be re-derived without re-measuring:
the requested colour and dither config, the EXACT achieved ink histogram (we
generate the raster, so this is known, not inferred), all three raw readings, the
median, timestamps, and the device battery voltage.

Usage:
  python tools/color_campaign_spec.py --out build/colorcal/spec.json
  python tools/color_campaign_run.py --spec build/colorcal/spec.json \
      --out build/colorcal/campaign.jsonl --port COM10
"""

from __future__ import annotations

import argparse
import json
import os
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
from color_target import fullscreen_bayer_raster
from colorimeter import make_instrument
from hokku.screens.bigme_f7.bootstrap import _console_send
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.dither_streaming import dither

INK_NAMES = ("black", "white", "yellow", "red", "blue", "green")


def patch_raster(patch: dict, display) -> np.ndarray:
    """Palette-index raster for one patch.

    Three sources deliberately kept apart: `ink` is a solid primary (the anchors
    the whole area-coverage model is expressed in), `bayer` is the pipeline-free
    reference with exactly-known coverage, and `pipeline` is what the renderer
    really produces for a requested colour. Only dither() runs — no tonal chain,
    which on a flat field would be destroyed by autocontrast.
    """
    h, w = display.panel_h, display.panel_w
    if patch["source"] == "ink":
        return np.full((h, w), int(patch["ink_index"]), dtype=np.uint8)
    if patch["source"] == "bayer":
        return fullscreen_bayer_raster(w, h, int(patch["bayer_k"]))
    if patch["source"] == "pipeline":
        canvas = np.zeros((h, w, 3), dtype=np.uint8)
        canvas[:, :] = tuple(patch["rgb"])
        return dither(canvas, DitherConfig(**patch["config"]), display)
    raise ValueError(f"unknown patch source {patch['source']!r}")


def ink_histogram(idx: np.ndarray) -> dict[str, float]:
    """Exact area fraction of every ink. Known because we made the raster."""
    counts = np.bincount(idx.ravel(), minlength=len(INK_NAMES))
    return {INK_NAMES[i]: float(counts[i] / idx.size) for i in range(len(INK_NAMES))}


def read_battery_mv(s) -> int | None:
    """Ask the device for its battery voltage via `cfg show`.

    This is how the campaign proves it is actually running on external power:
    `usb_present` has read 0 on this unit even when plugged in, so the voltage
    trend over hours is the trustworthy signal, not the flag.
    """
    try:
        txt = _console_send(s, "cfg show", settle=1.0).decode("utf-8", "replace")
    except (serial.SerialException, OSError):
        return None
    for line in txt.splitlines():
        if "bat_mv=" in line:
            tail = line.split("bat_mv=")[1].split()[0]
            if tail.isdigit():
                return int(tail)
    return None


def median_xyz(raws: list[np.ndarray]) -> list[float] | None:
    """Per-channel median. Median, not mean: one bad reading should not move it.

    A spurious 'Communications failure' has already been seen mid-run, and a
    mean would quietly drag the result toward whatever garbage such a read
    produced.
    """
    if not raws:
        return None
    return [float(v) for v in np.median(np.vstack(raws), axis=0)]


def done_uids(path: Path) -> set[str]:
    """uids already recorded, so a resumed run does not repeat work."""
    if not path.exists():
        return set()
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            seen.add(json.loads(line)["uid"])
        except (json.JSONDecodeError, KeyError):
            continue  # a torn final line from a hard kill is not fatal
    return seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="JSONL, appended to")
    ap.add_argument("--port", default="COM10")
    ap.add_argument("--instrument", default="spotread", choices=("spotread", "manual"))
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--settle-s", type=float, default=3.0)
    ap.add_argument("--battery-every", type=int, default=20, help="patches between checks")
    ap.add_argument("--console-timeout", type=float, default=900.0)
    ap.add_argument("--instrument-timeout", type=float, default=45.0)
    ap.add_argument("--limit", type=int, default=0, help="stop after N patches (0 = all)")
    # A ColorMunki that loses calibration mid-run fails identically for every
    # subsequent patch. Without this the run would keep displaying and refreshing
    # for hours, recording nothing — the expensive failure mode, because the panel
    # time is spent either way. Stop early and keep what was measured.
    ap.add_argument("--max-consecutive-failures", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="display only, no readings")
    args = ap.parse_args(argv)

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    display = DISPLAY_REGISTRY[spec["model"]]
    patches = spec["patches"]

    already = done_uids(args.out)
    todo = [p for p in patches if p["uid"] not in already]
    if args.limit:
        todo = todo[: args.limit]
    print(f"spec {args.spec}: {len(patches)} patches, {len(already)} already done")
    print(f"this run: {len(todo)} patches, ~{len(todo) * 56 / 3600:.2f} h")
    if not todo:
        print("nothing to do")
        return 0

    instrument = None
    if not args.dry_run:
        instrument = make_instrument(args.instrument, timeout_s=args.instrument_timeout)
        instrument.prepare()
        # Preflight before any panel time: a ColorMunki that lost its calibration
        # fails identically for every patch, and finding that out on patch 400 is
        # hours wasted.
        print("preflight: proving the instrument answers...", flush=True)
        if instrument.read("preflight") is None:
            print(
                "\nABORT: no reading. Put the dial in the CALIBRATION position,\n"
                "let it calibrate, return it to MEASUREMENT on the glass, re-run.\n"
                "Nothing was displayed; no patch was consumed."
            )
            return 1
        print("preflight OK", flush=True)

    s = catch_console(args.port, args.console_timeout)
    if s is None:
        print("ABORT: console never answered. Long-press the power button and re-run.")
        return 1

    t_start = time.monotonic()
    n_ok = n_fail = 0
    consecutive_fail = 0
    stopped_early = False
    battery_first = battery_last = None
    try:
        with args.out.open("a", encoding="utf-8") as fh:
            for n, patch in enumerate(todo, 1):
                idx = patch_raster(patch, display)
                data = display.indices_to_panel_bytes(idx)
                inks = ink_histogram(idx)

                elapsed = time.monotonic() - t_start
                eta = (elapsed / max(n - 1, 1)) * (len(todo) - n + 1) if n > 1 else 0.0
                print(
                    f"\n[{n}/{len(todo)}] {patch['uid']} {patch['phase']}/{patch['label']}"
                    f"   ETA {eta / 3600:.2f} h",
                    flush=True,
                )

                rec: dict = {
                    "uid": patch["uid"],
                    "phase": patch["phase"],
                    "label": patch["label"],
                    "source": patch["source"],
                    "bayer_k": patch.get("bayer_k"),
                    "ink_index": patch.get("ink_index"),
                    "rgb": patch.get("rgb"),
                    "config_name": patch.get("config_name"),
                    "config": patch.get("config"),
                    "is_control": patch.get("is_control", False),
                    "ink_fraction": inks,
                    "t_unix": time.time(),
                }

                if not upload_with_retry(s, data, patch["label"], attempts=6, gap_s=8.0):
                    rec["error"] = "upload"
                    n_fail += 1
                else:
                    if args.dry_run:
                        n_ok += 1
                    else:
                        time.sleep(args.settle_s)
                        assert instrument is not None
                        raws = []
                        for r in range(args.repeats):
                            reading = instrument.read(f"{patch['label']} #{r + 1}")
                            if reading is None:
                                print(f"    ! no reading #{r + 1}")
                                continue
                            raws.append(reading.raw)
                            print(f"    raw XYZ = {reading.raw}")
                        rec["raw"] = [[float(v) for v in x] for x in raws]
                        med = median_xyz(raws)
                        rec["median_raw"] = med
                        if med is not None:
                            # spotread -i D65 already reports under D65; percent
                            # scale (Y=100 for a perfect diffuser) is preserved as
                            # measured and normalised at analysis time, when the
                            # brightest patch in the set is known.
                            rec["xyz_d65_pct"] = med
                            n_ok += 1
                        else:
                            rec["error"] = "no_readings"
                            n_fail += 1

                consecutive_fail = 0 if "error" not in rec else consecutive_fail + 1

                if n % args.battery_every == 1 or n == len(todo):
                    mv = read_battery_mv(s)
                    rec["battery_mv"] = mv
                    if mv:
                        battery_first = battery_first if battery_first is not None else mv
                        battery_last = mv
                        print(f"    battery {mv} mV", flush=True)

                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                os.fsync(fh.fileno())  # survive a hard kill, not just a clean exit

                if consecutive_fail >= args.max_consecutive_failures:
                    stopped_early = True
                    print(
                        f"\nSTOPPING: {consecutive_fail} patches in a row produced nothing.\n"
                        "  Most likely the instrument lost its calibration (it sleeps when\n"
                        "  idle). Recalibrate via the dial, then re-run the SAME command —\n"
                        "  every measured patch is on disk and will be skipped.",
                        flush=True,
                    )
                    break
    except KeyboardInterrupt:
        print("\ninterrupted — everything measured so far is already on disk", flush=True)
    finally:
        s.close()
        print("--- serial closed ---", flush=True)

    dt = time.monotonic() - t_start
    print(f"\n{n_ok} ok, {n_fail} failed, {dt / 3600:.2f} h elapsed")
    if battery_first is not None and battery_last is not None:
        delta = battery_last - battery_first
        trend = "charging/held" if delta >= -20 else "DRAINING"
        print(f"battery {battery_first} -> {battery_last} mV ({delta:+d}) — {trend}")
    if stopped_early:
        print("STOPPED EARLY — re-run the same command after recalibrating to continue.")
    print(f"results: {args.out}  (resume by re-running the same command)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
