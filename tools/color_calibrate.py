#!/usr/bin/env python3
"""Drive a full colour-calibration run: display the target, meter it, emit a palette.

One loop covering every screen model. For each screen it puts the calibration
target on the glass, walks the patches in order taking a reading per patch, then
turns the readings into a ``palette_measured_rgb`` block and a tone-response
curve.

Getting the target onto the glass differs per model, so that is abstracted:

  serial  Bigme F7 with the `frame` console command — pushes the exact bytes
          down the UART (~17 s). Nothing depends on the server's preset, on
          WiFi, or on which host the screen points at, which is what makes a
          measurement session reproducible. Preferred where available.

  server  Screens without `frame` (huessen_epf1301, seeedstudio_e1004) — upload
          the target PNG to a hokku server and pin it. Requires the server's
          preset to be `calibration_raw`, or the tonal chain will corrupt the
          patches. The tool checks and refuses rather than measuring a target it
          cannot trust.

  manual  You put the target up however you like and confirm. Always available.

Measurement is likewise abstracted (tools/colorimeter.py): a driven ArgyllCMS
`spotread`, or manual paste for any other instrument.

Usage:
    # Full run on the F7 with a ColorMunki Photo over ArgyllCMS
    python tools/color_calibrate.py --model bigme_f7 --via serial --instrument spotread

    # Every model in turn
    python tools/color_calibrate.py --all --instrument spotread

    # Any meter, typed in by hand
    python tools/color_calibrate.py --model bigme_f7 --via serial --instrument manual

    # Re-analyse a finished session without re-metering
    python tools/color_calibrate.py --model bigme_f7 --replay build/colorcal/readings_bigme_f7.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import color_read
import color_target
from colorimeter import D65_XYZ, Reading, finalise, make_instrument
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import xyz_to_lab

# Models whose firmware carries the `frame` console command.
SERIAL_CAPABLE = {"bigme_f7"}


# ── Getting the target onto the glass ────────────────────────────────────────


class TargetDisplay:
    """Puts the calibration target on one screen."""

    def show(self, model: str, panel_bytes: bytes, png_path: Path) -> bool:
        raise NotImplementedError


class SerialTargetDisplay(TargetDisplay):
    def __init__(self, port: str):
        self.port = port

    def show(self, model: str, panel_bytes: bytes, png_path: Path) -> bool:
        if model not in SERIAL_CAPABLE:
            print(f"  ! {model} has no `frame` console command — use --via server or manual")
            return False
        # Deliberately lazy: the server and manual paths must work on a machine
        # with no pyserial and no serial port at all.
        import serial  # noqa: PLC0415

        from send_frame import send_frame  # noqa: PLC0415

        print(f"  opening {self.port} @115200")
        s = serial.Serial(self.port, 115200, timeout=0.3)
        s.dtr = False
        s.rts = False
        try:
            return send_frame(s, panel_bytes, "calibration target")
        finally:
            s.close()


class ServerTargetDisplay(TargetDisplay):
    """Upload + pin through a running hokku server."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def show(self, model: str, panel_bytes: bytes, png_path: Path) -> bool:
        name = png_path.name
        # Refuse to measure a target the server will re-dither: the tonal chain
        # destroys flat patches and shifts the ramp off its known coverage.
        try:
            with urllib.request.urlopen(f"{self.base_url}/hokku/api/config", timeout=10) as r:
                cfg = json.loads(r.read().decode())
            preset = json.dumps(cfg).count("calibration_raw")
            if not preset:
                print("  ! server is not using the `calibration_raw` preset.")
                print("    Set it first, or the target will be dithered and the")
                print("    measurements will describe the pipeline, not the panel.")
                return False
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"  ! could not read server config ({e}) — cannot verify the preset")
            return False

        body = png_path.read_bytes()
        req = urllib.request.Request(
            f"{self.base_url}/hokku/api/upload",
            data=body,
            headers={"Content-Type": "application/octet-stream", "X-Filename": name},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except urllib.error.HTTPError as e:
            if e.code != 409:  # already present is fine
                print(f"  ! upload failed: {e}")
                return False
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.base_url}/hokku/api/show_next/{name}", method="POST"
                ),
                timeout=30,
            ).read()
        except urllib.error.HTTPError as e:
            print(f"  ! could not pin the target: {e}")
            return False
        input("  target queued — wait for the screen to refresh, then press Enter: ")
        return True


class ManualTargetDisplay(TargetDisplay):
    def show(self, model: str, panel_bytes: bytes, png_path: Path) -> bool:
        print(f"  put {png_path} on the {model} yourself (pixel-exact, no dithering).")
        return (
            input("  press Enter when it is on the glass, or 's' to skip: ").strip().lower() != "s"
        )


def make_display(via: str, port: str, server: str) -> TargetDisplay:
    return {
        "serial": lambda: SerialTargetDisplay(port),
        "server": lambda: ServerTargetDisplay(server),
        "manual": lambda: ManualTargetDisplay(),
    }[via]()


# ── The measurement loop ─────────────────────────────────────────────────────


def build_target(model: str, out_dir: Path):
    """Generate the target for *model*; returns (manifest, panel_bytes, png_path)."""
    display = DISPLAY_REGISTRY[model]
    n_ink = int(display.palette_measured_rgb.shape[0])
    patches = color_target.plan_patches(
        n_ink=n_ink,
        include_ramp=True,
        panel_w=display.panel_w,
        panel_h=display.panel_h,
        cols=5,
        rows=3,
        min_gutter=8,
    )
    idx = color_target.build_index_raster(patches, display.panel_w, display.panel_h)
    png = color_target.index_raster_to_png(idx, display)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"colorcal_{model}"
    png.save(stem.with_suffix(".png"))
    panel_bytes = display.indices_to_panel_bytes(idx)
    stem.with_suffix(".bin").write_bytes(panel_bytes)

    mm = color_target._panel_mm(model, display.panel_w, display.panel_h)
    px_mm = mm[0] / display.panel_w if mm else None
    manifest = {
        "model": model,
        "panel_w": display.panel_w,
        "panel_h": display.panel_h,
        "ink_names": list(color_target.INK_NAMES[:n_ink]),
        "palette_measured_rgb": display.palette_measured_rgb.tolist(),
        "patches": [
            {
                "order": p.order,
                "name": p.name,
                "kind": p.kind,
                "ink_index": p.ink_index,
                "black_fraction": p.black_fraction,
                "row": p.row,
                "col": p.col,
                "x": p.x,
                "y": p.y,
                "w": p.w,
                "h": p.h,
                "actual_black_fraction": round(color_target.actual_black_fraction(idx, p), 6),
                "center_px": list(p.center),
                "patch_mm": round(p.w * px_mm, 1) if px_mm else None,
            }
            for p in patches
        ],
    }
    stem.with_suffix(".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest, panel_bytes, stem.with_suffix(".png")


def meter_patches(manifest: dict, instrument, illuminant: str) -> dict[str, list[float]]:
    """Walk the patches in order, one reading each. Enter alone skips."""
    patches = manifest["patches"]
    print()
    print(f"  metering {len(patches)} patches. Place the instrument squarely on each patch;")
    print("  patches are ~29 mm and the aperture ~6 mm, so centring is forgiving.")
    print("  Enter alone skips a patch; Ctrl-C stops and keeps what you have.")
    print()

    taken: dict[str, Reading] = {}
    try:
        for p in patches:
            label = (
                f"[{p['order']:>2}/{len(patches)}] {p['name']:<16} (row {p['row']} col {p['col']})"
            )
            r = instrument.read(label)
            if r is None:
                print("    · skipped")
                continue
            taken[str(p["order"])] = r
    except KeyboardInterrupt:
        print("\n  (stopped — analysing what was collected)")

    # Scale + white-point adaptation happen once the whole set is known.
    finalise(list(taken.values()), illuminant)
    return {k: np.asarray(v.xyz_d65).tolist() for k, v in taken.items()}


def analyse_absolute(manifest: dict, readings: dict) -> dict:
    """Analysis for an absolute instrument — no white-reference normalisation.

    A reflective spectrophotometer already reports absolute colorimetry, so the
    von Kries division color_read.normalise() performs for the colorimeter path
    would be actively wrong here: it would re-reference everything to whatever
    patch was chosen as white and throw away the absolute lightness the DRC
    stage depends on.
    """
    patches_by_order = {str(p["order"]): p for p in manifest["patches"]}
    measured = {}
    for order, xyz in readings.items():
        if order == "white_ref":
            continue
        lab = xyz_to_lab(np.asarray(xyz, dtype=float))
        measured[order] = {
            "patch": patches_by_order[order],
            "xyz": list(xyz),
            "lab": lab.tolist(),
            "rgb": np.rint(np.clip(color_read._lab_to_rgb(lab), 0, 255)).astype(int).tolist(),
        }
    return color_read.assemble(manifest, measured)


def run_one(model: str, args) -> bool:
    print("=" * 74)
    print(f"  {model}")
    print("=" * 74)

    out_dir = Path(args.out)
    manifest, panel_bytes, png_path = build_target(model, out_dir)
    side_mm = manifest["patches"][0].get("patch_mm")
    print(
        f"  target: {len(manifest['patches'])} patches"
        + (f", {side_mm:.0f} mm square" if side_mm else "")
    )

    readings_path = out_dir / f"readings_{model}.json"

    if args.replay:
        readings = json.loads(Path(args.replay).read_text(encoding="utf-8"))["readings"]
        print(f"  replaying {len(readings)} readings from {args.replay}")
    else:
        display = make_display(args.via, args.port, args.server)
        if not display.show(model, panel_bytes, png_path):
            print(f"  ! could not display the target on {model} — skipping")
            return False
        instrument = make_instrument(args.instrument, exe=args.spotread, illuminant=args.illuminant)
        instrument.prepare()
        try:
            readings = meter_patches(manifest, instrument, args.illuminant)
        finally:
            instrument.close()
        if not readings:
            print("  ! no readings collected")
            return False
        readings_path.write_text(
            json.dumps(
                {
                    "model": model,
                    "instrument": args.instrument,
                    "illuminant": args.illuminant,
                    "absolute": True,
                    "stamped": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "readings": readings,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n  readings saved to {readings_path}")

    result = analyse_absolute(manifest, readings)
    color_read.report(manifest, result, white_lab=D65_XYZ)  # absolute: no caveat banner
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--model", choices=sorted(DISPLAY_REGISTRY), help="one screen model")
    g.add_argument("--all", action="store_true", help="every registered model, in turn")
    ap.add_argument(
        "--via",
        default="serial",
        choices=("serial", "server", "manual"),
        help="how the target reaches the glass",
    )
    ap.add_argument("--instrument", default="spotread", choices=("spotread", "manual"))
    ap.add_argument("--spotread", default="spotread", help="path to the ArgyllCMS spotread binary")
    ap.add_argument(
        "--illuminant",
        default="d50",
        choices=("d50", "d65"),
        help="white point the instrument reports (reflective meters default to D50)",
    )
    ap.add_argument("--port", default="COM9", help="serial port, for --via serial")
    ap.add_argument("--server", default="http://127.0.0.1:8080", help="for --via server")
    ap.add_argument("--out", default="build/colorcal", help="output directory")
    ap.add_argument("--replay", help="re-analyse a saved readings file instead of metering")
    args = ap.parse_args(argv)

    models = sorted(DISPLAY_REGISTRY) if args.all else [args.model]
    if args.all and args.replay:
        raise SystemExit("--replay takes a single --model")

    ok = 0
    for i, model in enumerate(models):
        via = args.via
        if via == "serial" and model not in SERIAL_CAPABLE:
            print(f"\n(note: {model} has no `frame` command — falling back to manual for it)")
            args.via = "manual"
        if run_one(model, args):
            ok += 1
        args.via = via
        if i + 1 < len(models):
            input("\npress Enter for the next screen: ")

    print(f"\n{ok}/{len(models)} screen(s) measured")
    return 0 if ok == len(models) else 1


if __name__ == "__main__":
    raise SystemExit(main())
