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
import hashlib
import json
import os
import random
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
from f7_calibrate import NOMINAL_LIFETIME_S, SAFETY_MARGIN_S, calibration_age_s
from hokku.screens.bigme_f7.bootstrap import _console_send
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.dither_streaming import dither

INK_NAMES = ("black", "white", "yellow", "red", "blue", "green")

# Wall-clock cost of one six-ink anchor block (upload + refresh + reads each).
# Reserved at the end of a session so the closing bracket fits before the dark
# calibration expires.
ANCHOR_BLOCK_S = 6 * 55


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


def read_with_retry(instrument, label: str, retries: int):
    """One reading, retrying transient instrument failures.

    Intermittent 'Communications failure' / 'Instrument initialisation failed'
    from the ColorMunki is well documented and self-clearing — measured here at
    ~2 per 114 reads, with the next read succeeding. Retrying costs a couple of
    seconds; not retrying costs the patch, and previously killed the whole run.
    """
    for attempt in range(retries + 1):
        reading = instrument.read(label)
        if reading is not None:
            return reading
        if getattr(instrument, "last_error", None) != "transient":
            return None  # a real failure — let the caller classify it
        if attempt < retries:
            print(f"    (transient instrument error, retry {attempt + 1}/{retries})", flush=True)
            time.sleep(2.0)
    return None


def read_firmware(s) -> str | None:
    """Firmware version + config the device reports, for the session header.

    Worth recording because the `frame` upload path and the panel waveform both
    live in firmware: a session measured on a different build is not
    automatically comparable, and without this that would be invisible later.
    """
    try:
        txt = _console_send(s, "cfg show", settle=1.0).decode("utf-8", "replace")
    except (serial.SerialException, OSError):
        return None
    bits = [ln.strip() for ln in txt.splitlines() if ln.strip().startswith("cfg:")]
    return " | ".join(bits) if bits else None


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
    """uids that actually HAVE a measurement, so a resume does not repeat work.

    Deliberately keyed on having data rather than on having been attempted. Every
    failure mode seen here is transient — an upload glitch, an expired calibration
    — so a patch that failed should come back around on the next run. Counting the
    attempt would leave a permanent hole in the dataset with nothing to indicate
    why that colour is missing.
    """
    if not path.exists():
        return set()
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line from a hard kill is not fatal
        # A dry run displays patches but measures nothing. Its rows must NOT count
        # as done, or a rehearsal would permanently mask real patches from every
        # later run — the data would simply be missing, with nothing to show why.
        if rec.get("dry_run"):
            continue
        uid = rec.get("uid")
        if uid and "xyz_d65_pct" in rec:
            seen.add(uid)
    return seen


def measured_by_raster(path: Path) -> dict[str, dict]:
    """Map raster hash -> an already-measured record with that exact stimulus.

    The 9x9x9 gamut cube collapses hard on a device this gamut-limited: 729 input
    colours produce only ~313 distinct rasters, so 416 of them are byte-identical
    to one already scheduled. Identical panel bytes mean an identical stimulus, so
    re-measuring them buys nothing but 5.4 h of panel time.

    Dedup happens HERE rather than in the spec on purpose. Patch uids are assigned
    positionally and the interleaved controls are numbered after the phases, so
    shrinking a phase would silently renumber controls and remap already-measured
    uids onto different stimuli. Keying on the raster hash leaves every uid alone.
    """
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        h = rec.get("raster_sha1")
        if h and "xyz_d65_pct" in rec and not rec.get("duplicate_of"):
            out.setdefault(h, rec)
    return out


def next_session(path: Path) -> int:
    """Session number for this run: one past the highest already recorded.

    A "session" is one calibration's worth of measuring. The number is derived
    from the file rather than held anywhere, so the whole workflow stays
    idempotent — re-running after a crash continues the numbering correctly with
    no state to get out of sync.
    """
    if not path.exists():
        return 1
    hi = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            hi = max(hi, int(json.loads(line).get("session") or 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return hi + 1


def measure_session_anchors(
    s, display, instrument, session: int, repeats: int, settle_s: float, emit, tag: str = "open"
) -> int:
    """Re-measure the primaries at the start (and end) of every session.

    Running this at BOTH ends is what turns the campaign into its own drift
    experiment. ArgyllCMS invalidates the dark calibration after exactly one hour
    on elapsed time alone, measuring nothing — so whether that hour is protecting
    anything is an open question, and an expensive one at a dial rotation apiece.
    Anchors at open and close bracket a single calibration: if a stale dark cal
    biases readings, the same six inks measured ~50 minutes apart on that one
    calibration must differ. If they agree to the noise floor, the timeout is
    over-conservative and the evidence is ours rather than assumed.

    The original plan was to fake the calibration date in Argyll's cache file and
    measure for hours on a deliberately stale one. That file is checksummed over
    its serialised fields, and forging it convincingly was not worth the risk of a
    subtly corrupt calibration. This gets the same answer from data we collect
    anyway.

    The instrument is recalibrated between sessions, and a calibration is not a
    perfect restoration of the previous one. Without a per-session anchor there
    is no way to tell a real difference between two patches measured in
    different sessions from a shift in the rig — and on a campaign that spans
    days across a dozen calibrations, that ambiguity would contaminate
    everything.

    These carry their own uids (``s003_anchor_red``) so resume never skips them:
    a new session must always produce a fresh set.

    Each anchor is emitted the moment it completes rather than batched at the
    end: six anchors is ~6 minutes, and a calibration expiry or crash inside that
    window must not discard the ones already measured.
    """
    n = 0
    h, w = display.panel_h, display.panel_w
    for i, name in enumerate(INK_NAMES):
        idx = np.full((h, w), i, dtype=np.uint8)
        data = display.indices_to_panel_bytes(idx)
        print(f"  anchor[{tag}] {i + 1}/{len(INK_NAMES)}: {name}", flush=True)
        rec: dict = {
            "uid": f"s{session:03d}_anchor_{tag}_{name}",
            "session": session,
            "phase": "session_anchor",
            "anchor_tag": tag,
            "label": f"anchor {tag} {name}",
            "source": "ink",
            "ink_index": i,
            "ink_fraction": {n: (1.0 if n == name else 0.0) for n in INK_NAMES},
            "raster_sha1": hashlib.sha1(data, usedforsecurity=False).hexdigest()[:16],
            "t_unix": time.time(),
        }
        if not upload_with_retry(s, data, f"anchor {name}", attempts=6, gap_s=8.0):
            rec["error"] = "upload"
            emit(rec)
            continue
        time.sleep(settle_s)
        raws, specs = [], []
        for r in range(repeats):
            reading = read_with_retry(instrument, f"anchor {name} #{r + 1}", 2)
            if reading is None:
                continue
            raws.append(reading.raw)
            if reading.spectrum is not None and reading.wavelengths is not None:
                specs.append(reading.spectrum)
                rec.setdefault("wavelengths_nm", [float(v) for v in reading.wavelengths])
            print(f"    raw XYZ = {reading.raw}")
        rec["raw"] = [[float(v) for v in x] for x in raws]
        med = median_xyz(raws)
        rec["median_raw"] = med
        if med is not None:
            rec["xyz_d65_pct"] = med
        else:
            rec["error"] = "no_readings"
        if specs:
            rec["spectrum_pct"] = [[float(v) for v in sp] for sp in specs]
            rec["median_spectrum_pct"] = [float(v) for v in np.median(np.vstack(specs), axis=0)]
        emit(rec)
        n += 1
    return n


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
    # Transient USB failures from this instrument are self-clearing; retrying is
    # seconds, while treating one as fatal costs the rest of the cycle.
    ap.add_argument("--transient-retries", type=int, default=2)
    # A cycle started on a nearly-expired calibration is a wasted cycle: one was
    # lost starting with ~3 minutes left. Refuse rather than discover it later.
    # Minimum PATCH time a cycle must be able to do, on top of anchor/margin
    # overhead. Not the total window — see the guard, which adds the overhead.
    ap.add_argument("--min-usable-min", type=float, default=15.0)
    ap.add_argument("--ignore-calibration-age", action="store_true")
    # This unit discharges even on USB (measured -80 mV/h), so a long campaign
    # walks the battery down. Warn well before it matters, and stop cleanly rather
    # than let it die mid-refresh: a half-written panel plus an unmeasured patch is
    # a worse place to resume from than a deliberate stop.
    ap.add_argument("--battery-warn-mv", type=int, default=3800)
    ap.add_argument("--battery-stop-mv", type=int, default=3600)
    # Reflective calibration expires on a timer (~58 min measured), regardless of
    # how much it is used, and ArgyllCMS offers no way to extend it.
    ap.add_argument("--calibration-warn-min", type=float, default=45.0)
    ap.add_argument("--calibration-expect-min", type=float, default=58.0)
    # Anchors at BOTH ends bracket a single calibration, which is how we find out
    # whether Argyll's 1 h dark-cal timeout protects anything real. Disable once
    # that question is settled to buy back ~6 min per cycle.
    # Deduplicate byte-identical stimuli (see measured_by_raster). Sampling a few
    # anyway keeps the assumption under test instead of merely assumed.
    ap.add_argument("--no-dedup", dest="dedup", action="store_false")
    ap.add_argument("--dedup-sample-rate", type=float, default=0.03)
    ap.add_argument("--no-closing-anchors", dest="closing_anchors", action="store_false")
    ap.add_argument(
        "--no-anchors",
        action="store_true",
        help="skip the per-session primary re-measurement (not recommended)",
    )
    ap.add_argument("--dry-run", action="store_true", help="display only, no readings")
    args = ap.parse_args(argv)

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    display = DISPLAY_REGISTRY[spec["model"]]
    patches = spec["patches"]

    session = next_session(args.out)
    seen_rasters = measured_by_raster(args.out)
    # Seeded per session so a resumed run is reproducible rather than re-rolling.
    rng = random.Random(session)  # noqa: S311 — sampling, not secrecy
    already = done_uids(args.out)
    todo = [p for p in patches if p["uid"] not in already]
    if args.limit:
        todo = todo[: args.limit]
    # Report progress against the SPEC. `already` also holds the per-session
    # anchors, which are extra measurements outside the spec, so printing it
    # produced the nonsense "1436 patches, 1438 already done".
    print(
        f"session {session} | spec {args.spec}: {len(patches)} patches, "
        f"{len(patches) - len(todo)} done, {len(todo)} to go"
    )
    # 47 s/patch is the measured throughput; 56 was the original estimate and
    # overstated every ETA by ~20 %.
    print(f"this run: {len(todo)} patches, ~{len(todo) * 47 / 3600:.2f} h if none inherit")
    if not todo:
        print("nothing to do")
        return 0

    age = calibration_age_s()
    if not args.dry_run and not args.ignore_calibration_age:
        if age is None:
            print(
                "No calibration timestamp found. Run tools/f7_calibrate.py first\n"
                "(or pass --ignore-calibration-age if the calibration is known fresh)."
            )
            return 1
        # Gate on time REMAINING, not on age. Age was the wrong test: it refused a
        # perfectly good 39-minute window just because 21 minutes had already been
        # spent on a short run. What makes a cycle not worth starting is having too
        # little left to measure anything useful, which is what this asks.
        remaining = NOMINAL_LIFETIME_S - age
        # The anchor blocks are overhead, not patches. A 20-minute window sounds
        # workable until you subtract two six-ink blocks (~11 min) and the safety
        # margin, leaving 4 minutes of actual measuring — that really happened and
        # yielded 6 spec patches for a dial rotation. Require enough left to cover
        # the overhead AND do a useful amount of work.
        overhead = SAFETY_MARGIN_S
        if not args.no_anchors:
            overhead += ANCHOR_BLOCK_S * (2 if args.closing_anchors else 1)
        needed = overhead + args.min_usable_min * 60
        if remaining < needed:
            print(
                f"ABORT: only {remaining / 60:.0f} min of dark calibration left "
                f"(need at least {args.min_usable_min:.0f}).\n"
                "  Recalibrate with tools/f7_calibrate.py, then start immediately."
            )
            return 1
        print(
            f"calibration age {age / 60:.1f} min — "
            f"~{(NOMINAL_LIFETIME_S - age) / 60:.0f} min of cycle expected"
        )

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
    n_ok = n_fail = n_dedup = 0
    consecutive_fail = 0
    stop_for_battery: int | None = None
    calibration_expired = False
    cal_signals = 0
    warned_cal = False
    stopped_early = False
    battery_first = battery_last = None
    try:
        with args.out.open("a", encoding="utf-8") as fh:

            def emit(rec: dict) -> None:
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                os.fsync(fh.fileno())  # survive a hard kill, not just a clean exit

            # Session header: everything needed to interpret this session's rows
            # without consulting anything outside the file.
            emit(
                {
                    "record": "session_start",
                    "session": session,
                    "t_unix": time.time(),
                    "spec": str(args.spec),
                    "spec_sha256": hashlib.sha256(
                        json.dumps(spec["patches"], sort_keys=True).encode()
                    ).hexdigest()[:16],
                    "model": spec["model"],
                    "instrument": args.instrument,
                    "instrument_args": list(getattr(instrument, "extra_args", [])),
                    "repeats": args.repeats,
                    "settle_s": args.settle_s,
                    "firmware": read_firmware(s),
                    "battery_mv_start": read_battery_mv(s),
                }
            )

            # Re-measure the primaries FIRST, every session. The instrument was
            # recalibrated since the last one, and a recalibration is not a
            # perfect restoration — without this the difference between two
            # sessions is unattributable.
            if not args.dry_run and not args.no_anchors:
                print(f"\n=== session {session}: re-measuring the six primaries ===", flush=True)
                assert instrument is not None
                measure_session_anchors(
                    s, display, instrument, session, args.repeats, args.settle_s, emit, "open"
                )
            for n, patch in enumerate(todo, 1):
                idx = patch_raster(patch, display)
                data = display.indices_to_panel_bytes(idx)
                inks = ink_histogram(idx)

                elapsed = time.monotonic() - t_start
                # ArgyllCMS invalidates the DARK calibration at exactly DCALTOUT
                # (1 h) on elapsed time alone — see f7_calibrate for the source
                # quote. Because the deadline is known rather than guessed, stop on
                # our own terms just before it instead of letting reads fail into
                # it and lose patches to errors that cannot succeed.
                cal_left = None if age is None else NOMINAL_LIFETIME_S - (age + elapsed)
                # Leave room for the closing anchor block as well as the margin, so
                # the session ends with a measured bracket rather than being cut off.
                closing = ANCHOR_BLOCK_S if (args.closing_anchors and not args.no_anchors) else 0
                if cal_left is not None and cal_left <= SAFETY_MARGIN_S + closing:
                    stopped_early = True
                    if closing and not args.dry_run and instrument is not None:
                        print(
                            f"\n=== session {session}: closing anchors "
                            "(brackets this calibration for drift) ===",
                            flush=True,
                        )
                        measure_session_anchors(
                            s,
                            display,
                            instrument,
                            session,
                            args.repeats,
                            args.settle_s,
                            emit,
                            "close",
                        )
                    print(
                        "\n*** dark calibration nearly expired — stopped cleanly. ***\n"
                        "  Recalibrate with tools/f7_calibrate.py, then start again.",
                        flush=True,
                    )
                    break
                if not warned_cal and cal_left is not None and cal_left < 12 * 60:
                    warned_cal = True
                    print(
                        f"\n*** HEADS UP: {cal_left / 60:.0f} min of dark calibration "
                        "left; this cycle will end cleanly then. ***",
                        flush=True,
                    )
                eta = (elapsed / max(n - 1, 1)) * (len(todo) - n + 1) if n > 1 else 0.0
                print(
                    f"\n[{n}/{len(todo)}] {patch['uid']} {patch['phase']}/{patch['label']}"
                    f"   ETA {eta / 3600:.2f} h",
                    flush=True,
                )

                rec: dict = {
                    "uid": patch["uid"],
                    "session": session,
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
                    # Hash of the exact bytes sent to the panel: proves later that
                    # a re-derived raster is the same stimulus that was measured.
                    "raster_sha1": hashlib.sha1(data, usedforsecurity=False).hexdigest()[:16],
                    "t_unix": time.time(),
                }

                # Byte-identical stimulus already measured? Then measuring it again
                # can only reproduce it, so inherit the result and move on. A small
                # random fraction IS re-measured, because "identical raster implies
                # identical reading" is an assumption worth continuously testing
                # rather than trusting blindly.
                # NEVER dedup a control. Controls are deliberately the same
                # stimulus over and over — that is the whole point, since drift is
                # only visible by re-measuring something unchanged. Inheriting
                # them looks like a free saving and is actually the measurement
                # being skipped: 77 drift samples were lost this way before this
                # check existed.
                may_dedup = args.dedup and not patch.get("is_control")
                twin = seen_rasters.get(rec["raster_sha1"]) if may_dedup else None
                sample_it = twin is not None and rng.random() < args.dedup_sample_rate
                if twin is not None and not sample_it:
                    rec["duplicate_of"] = twin["uid"]
                    rec["xyz_d65_pct"] = twin["xyz_d65_pct"]
                    if twin.get("median_spectrum_pct"):
                        rec["median_spectrum_pct"] = twin["median_spectrum_pct"]
                        rec["wavelengths_nm"] = twin.get("wavelengths_nm")
                    n_dedup += 1
                    print(f"    = identical raster to {twin['uid']} — inherited", flush=True)
                    emit(rec)
                    continue

                if not upload_with_retry(s, data, patch["label"], attempts=6, gap_s=8.0):
                    rec["error"] = "upload"
                    n_fail += 1
                else:
                    if args.dry_run:
                        rec["dry_run"] = True  # see done_uids: must not mask a real patch
                        n_ok += 1
                    else:
                        time.sleep(args.settle_s)
                        assert instrument is not None
                        raws, specs = [], []
                        for r in range(args.repeats):
                            reading = read_with_retry(
                                instrument, f"{patch['label']} #{r + 1}", args.transient_retries
                            )
                            if reading is None:
                                print(f"    ! no reading #{r + 1}")
                                if getattr(instrument, "last_error", None) == "calibration":
                                    # Require the signal TWICE before stopping the
                                    # run. A single one has already proven to be a
                                    # transient glitch wearing a calibration
                                    # message, and stopping on it cost half a
                                    # cycle every time.
                                    cal_signals += 1
                                    if cal_signals >= 2:
                                        calibration_expired = True
                                        break
                                continue
                            cal_signals = 0
                            raws.append(reading.raw)
                            if reading.spectrum is not None and reading.wavelengths is not None:
                                specs.append(reading.spectrum)
                                rec.setdefault(
                                    "wavelengths_nm", [float(v) for v in reading.wavelengths]
                                )
                            print(f"    raw XYZ = {reading.raw}")
                        if specs:
                            # The reflectance curve is the irreducible measurement.
                            # XYZ is one projection of it; keeping the spectrum is
                            # what allows any illuminant or observer to be applied
                            # later without returning to the hardware.
                            rec["spectrum_pct"] = [[float(v) for v in sp] for sp in specs]
                            rec["median_spectrum_pct"] = [
                                float(v) for v in np.median(np.vstack(specs), axis=0)
                            ]
                        rec["raw"] = [[float(v) for v in x] for x in raws]
                        med = median_xyz(raws)
                        rec["median_raw"] = med
                        if med is not None:
                            # spotread -i D65 already reports under D65; percent
                            # scale (Y=100 for a perfect diffuser) is preserved as
                            # measured and normalised at analysis time, when the
                            # brightest patch in the set is known.
                            rec["xyz_d65_pct"] = med
                            if not patch.get("is_control"):
                                # Controls are re-measured every time, so they
                                # must not seed the index either — otherwise a
                                # later ordinary patch would inherit from one.
                                seen_rasters.setdefault(rec["raster_sha1"], rec)
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
                        note = ""
                        if mv <= args.battery_stop_mv:
                            note = "  *** TOO LOW — STOPPING ***"
                        elif mv <= args.battery_warn_mv:
                            note = "  *** LOW — CHARGE THE SCREEN SOON ***"
                        print(f"    battery {mv} mV{note}", flush=True)
                        if mv <= args.battery_stop_mv:
                            stop_for_battery = mv

                emit(rec)

                if calibration_expired:
                    stopped_early = True
                    print(
                        "\n*** CALIBRATION EXPIRED — the meter needs recalibrating. ***\n"
                        "  Rotate the dial to the CALIBRATION position and say so; I will\n"
                        "  run the calibration, then you rotate back to MEASUREMENT.\n"
                        "  Every measured patch is on disk; the same command resumes.",
                        flush=True,
                    )
                    break

                if stop_for_battery is not None:
                    stopped_early = True
                    print(
                        f"\nSTOPPING: battery {stop_for_battery} mV is at or below the "
                        f"{args.battery_stop_mv} mV floor.\n"
                        "  CHARGE THE SCREEN, then re-run the SAME command — every\n"
                        "  measured patch is on disk and will be skipped.",
                        flush=True,
                    )
                    break

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
        # This ADC is noisy: a 2.4-minute dry run reported -40 mV, which is not a
        # physical discharge. Every sample so far sits inside a ~+/-40 mV band, so
        # anything smaller than that is not a trend, and calling it one sent me
        # chasing a power problem that did not exist.
        trend = (
            "held (within ADC noise)"
            if delta >= -80
            else f"DRAINING over {(time.monotonic() - t_start) / 3600:.1f} h"
        )
        print(f"battery {battery_first} -> {battery_last} mV ({delta:+d}) — {trend}")
        if battery_last <= args.battery_warn_mv:
            print("*** CHARGE THE SCREEN before the next cycle ***")
    if stopped_early:
        print("STOPPED EARLY — re-run the same command after recalibrating to continue.")
    print(f"results: {args.out}  (resume by re-running the same command)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
