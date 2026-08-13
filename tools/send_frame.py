#!/usr/bin/env python3
"""Push ready-made panel frames to a Bigme F7 over the serial console.

The device is a dumb pipe: it holds no patterns and no list. This tool decides
what to display and uploads the exact bytes, so new test images never need a
firmware rebuild. The frames come from the production packing path
(``Display.indices_to_panel_bytes``), so what lands on the glass exercises the
same code the server uses — not a mock.

Why this exists: the render pipeline is the wrong tool for colour measurement
and bring-up. Metering a panel means putting an *exact*, known raster on the
glass on demand, with no dithering, no tonal chain, no WiFi and no server
config in the loop. See ``docs/color_calibration.md``.

Protocol is ``firmware/common/all/frame_proto.h``; the device side is
``hokku_frame_receive()``, implemented per board in ``firmware/*/main.c``.
The device announces its own frame size in the READY line, so this tool
needs no per-model knowledge of geometry — pass ``--model`` only so the
right dither palette and packing are used to BUILD the frame.

Usage:
    # Cycle every ink, then the calibration target (the colour-cycle demo)
    python tools/send_frame.py --port COM9 --cycle

    # Send one specific frame
    python tools/send_frame.py --port COM9 --solid red
    python tools/send_frame.py --port COM9 --target
    python tools/send_frame.py --port COM9 --bin build/colorcal/colorcal_bigme_f7.bin
"""

from __future__ import annotations

import argparse
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import serial

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from color_target import (
    INK_NAMES,
    build_index_raster,
    plan_patches,
)
from hokku.screens.registry import DISPLAY_REGISTRY

# Must match firmware/common/all/frame_proto.h.
CHUNK_BYTES = 4096
ACK = b"K"
READY = "READY"
DONE = "DONE"
REFRESHED = "REFRESHED"

# Only the CH340-bridged Bigme F7 actually honours this: on the ESP32-S3 the
# console is native USB Serial/JTAG (a CDC device), where the baud setting is
# negotiated away and throughput is bounded by USB, not by this number.
# pyserial still requires a value, so one is given.
BAUD = 115200
# The panel update itself is ~30 s; allow generous headroom before giving up.
REFRESH_TIMEOUT_S = 90.0


def solid_frame(display, ink: int) -> bytes:
    idx = np.full((display.panel_h, display.panel_w), ink, dtype=np.uint8)
    return display.indices_to_panel_bytes(idx)


def target_frame(display) -> bytes:
    patches = plan_patches(
        n_ink=int(display.palette_measured_rgb.shape[0]),
        include_ramp=True,
        panel_w=display.panel_w,
        panel_h=display.panel_h,
        cols=5,
        rows=3,
        min_gutter=8,
    )
    idx = build_index_raster(patches, display.panel_w, display.panel_h)
    return display.indices_to_panel_bytes(idx)


def _read_line(s: serial.Serial, timeout_s: float) -> str:
    """Read one CRLF line, ignoring blanks and any console echo."""
    end = time.monotonic() + timeout_s
    buf = b""
    while time.monotonic() < end:
        b = s.read(1)
        if not b:
            continue
        if b in (b"\r", b"\n"):
            if buf.strip():
                return buf.decode("utf-8", "replace").strip()
            buf = b""
            continue
        buf += b
    return ""


def assert_interactive(s: serial.Serial, model: str) -> None:
    """Tell the device a host owns it now, so it stops scheduling refreshes and
    stops hibernating between uploads (firmware/common/all/interactive.h).

    Without this, host-driven work over the console is a race: the device's
    normal job is to wake on a schedule, fetch a picture and go back to sleep,
    and every one of those steps takes the console away — the F7's closes when
    it hibernates, the ESP32 boards reboot for each refresh and drop the USB
    device entirely. Polling harder only makes the race less likely, never
    absent, which is not good enough for a run of hundreds of consecutive
    uploads.

    Best-effort: an older firmware without the command just gets an ERR line
    back, which is silently fine — it behaves exactly as it did before this
    existed, still racy, but not worse off for the attempt.
    """
    s.reset_input_buffer()
    s.write(b"interactive on\r\n")
    s.flush()
    line = _read_line(s, timeout_s=2.0)
    if "engaged" in line.lower() or line.upper().startswith("INTERACTIVE ON"):
        print(f"    {model}: interactive mode engaged")
    elif line:
        print(f"    {model}: interactive request got {line!r} — old firmware? proceeding anyway")


def open_device(
    port: str, model: str, timeout_s: float = 600.0, interactive: bool = True
) -> serial.Serial | None:
    """Open the device's console, by whatever means that board needs.

    The two families differ in a way that matters here. The Bigme F7's console
    lives only a few seconds per wake unless the unit is pinned awake, so it has
    to be *caught* by polling — see ``color_measure_f7.catch_console``. The
    ESP32-S3 boards hold a console for as long as USB is attached (it only runs
    in the USB_AWAKE regime), so a plain open is enough.

    For those, `ping` is the handshake: it proves a console is actually listening
    and that the firmware is new enough to have one, before a caller commits to
    pushing a megabyte at a device that may not be able to receive it. Without it
    the failure surfaces as a silent timeout partway through an upload.

    ``interactive`` asserts USB-interactive mode as part of the handshake — the
    point at which the caller has a live console and is about to start relying
    on it staying that way. Pass False only for one-off pokes (a single `ping`,
    bring-up) where letting the device go back to its own schedule afterwards is
    fine or even wanted.
    """
    if model == "bigme_f7":
        from color_measure_f7 import catch_console  # noqa: PLC0415 — avoids a cycle

        s = catch_console(port, timeout_s)
        if s is not None and interactive:
            assert_interactive(s, model)
        return s

    # Poll, and reopen each round. These boards enumerate their console over
    # native USB, so a reboot makes the port itself disappear and come back —
    # holding a handle across one is useless. A scheduled refresh costs ~30 s of
    # panel update plus the reboot, so the wait can legitimately be long.
    deadline = time.monotonic() + timeout_s
    announced = False
    while time.monotonic() < deadline:
        try:
            s = serial.Serial(port, BAUD, timeout=0.3)
        except (serial.SerialException, OSError):
            if not announced:
                print(f"    waiting for {port} (device rebooting?)...")
                announced = True
            time.sleep(1.0)
            continue

        s.reset_input_buffer()
        s.write(b"ping\r\n")
        s.flush()
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            line = _read_line(s, timeout_s=1.0)
            if line.startswith("PONG"):
                print(f"    console: {line}")
                if interactive:
                    assert_interactive(s, model)
                return s
        s.close()
        if not announced:
            print("    no PONG yet — device may be mid-refresh; retrying...")
            announced = True
        time.sleep(1.5)

    print("    ! console never answered. Is it on USB, and is the firmware new")
    print("      enough to have one? (`frame` needs huessen >= 1.2.22)")
    return None


def send_frame(s: serial.Serial, data: bytes, label: str, verbose: bool = True) -> bool:
    """Run one `frame` upload. Returns True once the panel reports REFRESHED."""
    expect_crc = zlib.crc32(data)
    if verbose:
        print(f"  {label}: {len(data)} B, crc32=0x{expect_crc:08X}")

    s.reset_input_buffer()
    s.write(b"frame\r\n")
    s.flush()

    # Wait for READY <total> <chunk>; the device prints it before taking the UART.
    deadline = time.monotonic() + 10.0
    total = chunk = None
    while time.monotonic() < deadline:
        line = _read_line(s, timeout_s=max(0.1, deadline - time.monotonic()))
        if line.startswith(READY):
            parts = line.split()
            if len(parts) >= 3:
                total, chunk = int(parts[1]), int(parts[2])
            break
        if line and verbose:
            print(f"    (device: {line})")
    if total is None:
        print("    ! device never sent READY — is the firmware new enough for `frame`?")
        return False
    if total != len(data):
        print(f"    ! device expects {total} B, frame is {len(data)} B")
        return False
    if chunk != CHUNK_BYTES:
        print(f"    ! chunk size mismatch: device {chunk}, host {CHUNK_BYTES}")
        return False

    # Drop whatever is still buffered from the READY line before the first chunk.
    # _read_line() returns on the FIRST of \r or \n, so its partner is still queued
    # and would otherwise be read as chunk 0's ACK — which fails as `got b'\n'` on
    # the very first chunk. Safe here: the device sends nothing between READY and
    # the ACK for a chunk we have not written yet.
    s.reset_input_buffer()

    sent = 0
    t0 = time.monotonic()
    while sent < total:
        piece = data[sent : sent + chunk]
        s.write(piece)
        s.flush()
        # Per-chunk ACK is the flow control: the RX FIFO is tiny and the panel is
        # fed a byte at a time, so an unthrottled blast would overrun the device.
        ack = s.read(1)
        if ack != ACK:
            print(f"    ! no ACK after {sent} B (got {ack!r}) — aborted")
            return False
        sent += len(piece)
        if verbose and sent % (chunk * 10) == 0:
            pct = 100.0 * sent / total
            print(f"    {sent}/{total} B ({pct:.0f}%)", end="\r", flush=True)

    elapsed = time.monotonic() - t0
    if verbose:
        print(f"    uploaded {sent} B in {elapsed:.1f}s ({sent / elapsed / 1024:.1f} KiB/s)")

    line = _read_line(s, timeout_s=10.0)
    if not line.startswith(DONE):
        print(f"    ! expected DONE, got {line!r}")
        return False
    dev_crc = int(line.split()[1], 16)
    if dev_crc != expect_crc:
        print(f"    ! CRC mismatch: device 0x{dev_crc:08X}, host 0x{expect_crc:08X}")
        return False
    if verbose:
        print(f"    CRC OK (0x{dev_crc:08X}) — refreshing panel (~30 s)")

    # Keep reading until REFRESHED rather than testing a single line. The device
    # writes an hlog line ("hokku: frame received ... — refreshing") between DONE
    # and REFRESHED, and hlog can interleave at any moment, so a one-shot read
    # reports failure for an upload that actually succeeded — the panel then gets
    # needlessly repainted by a retry.
    deadline = time.monotonic() + REFRESH_TIMEOUT_S
    while time.monotonic() < deadline:
        line = _read_line(s, timeout_s=max(0.1, deadline - time.monotonic()))
        if line.startswith(REFRESHED):
            if verbose:
                print("    REFRESHED")
            break
        if "ABORTED" in line:
            print(f"    ! device aborted the refresh: {line!r}")
            return False
        if line and verbose:
            print(f"    (device: {line})")
    else:
        print(f"    ! no REFRESHED within {REFRESH_TIMEOUT_S:.0f}s")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", default="COM9", help="serial port of the F7 console")
    ap.add_argument("--model", default="bigme_f7", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument(
        "--console-timeout",
        type=float,
        default=180.0,
        help="seconds to wait for the console to answer. The default is generous "
        "because an ESP32 board caught mid-refresh needs ~30 s of panel update "
        "plus a reboot before its console exists again.",
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cycle", action="store_true", help="every ink, then the calibration target")
    g.add_argument("--solid", help=f"one flat ink: {', '.join(INK_NAMES)}")
    g.add_argument("--target", action="store_true", help="the calibration target")
    g.add_argument("--bin", type=Path, help="send a raw panel .bin as-is")
    ap.add_argument("--repeat", type=int, default=1, help="repeat the whole sequence N times")
    ap.add_argument(
        "--no-interactive",
        action="store_true",
        help="do not assert USB-interactive mode — the device stays on its own "
        "refresh schedule, which is racy for anything but a single one-off frame",
    )
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]

    frames: list[tuple[str, bytes]] = []
    if args.cycle:
        frames = [(n, solid_frame(display, i)) for i, n in enumerate(INK_NAMES)]
        frames.append(("calibration target", target_frame(display)))
    elif args.solid:
        if args.solid not in INK_NAMES:
            raise SystemExit(f"unknown ink {args.solid!r}; choose from {', '.join(INK_NAMES)}")
        ink = INK_NAMES.index(args.solid)
        frames = [(args.solid, solid_frame(display, ink))]
    elif args.target:
        frames = [("calibration target", target_frame(display))]
    else:
        data = args.bin.read_bytes()
        if len(data) != display.total_bytes:
            raise SystemExit(
                f"{args.bin} is {len(data)} B, {args.model} needs {display.total_bytes} B"
            )
        frames = [(args.bin.name, data)]

    print(f"opening {args.port} @{BAUD}")
    # Handshake first. The ESP32 boards reboot for every scheduled refresh, and a
    # reboot takes the console with it — so "the console answered five minutes
    # ago" is not evidence it is answering now. Without this the symptom is
    # "device never sent READY", which reads like a firmware-too-old problem and
    # is actually a timing one.
    s = open_device(
        args.port,
        args.model,
        timeout_s=args.console_timeout,
        interactive=not args.no_interactive,
    )
    if s is None:
        return 1
    # Do not toggle DTR/RTS, which can reset the board on some USB-serial bridges.
    s.dtr = False
    s.rts = False

    ok = fail = 0
    try:
        for lap in range(args.repeat):
            if args.repeat > 1:
                print(f"--- lap {lap + 1}/{args.repeat} ---")
            for i, (name, data) in enumerate(frames, 1):
                print(f"[{i}/{len(frames)}] {name}")
                if send_frame(s, data, name):
                    ok += 1
                else:
                    fail += 1
    except KeyboardInterrupt:
        print("\n(interrupted)")
    finally:
        s.close()
        print("serial closed cleanly")

    print(f"\n{ok} frame(s) displayed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
