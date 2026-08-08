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
``hokku_frame_receive()`` in ``firmware/bigme_f7/main.c``.

Usage:
    # Cycle every ink, then the calibration target (the colour-cycle demo)
    python tools/f7_send_frame.py --port COM9 --cycle

    # Send one specific frame
    python tools/f7_send_frame.py --port COM9 --solid red
    python tools/f7_send_frame.py --port COM9 --target
    python tools/f7_send_frame.py --port COM9 --bin build/colorcal/colorcal_bigme_f7.bin
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
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cycle", action="store_true", help="every ink, then the calibration target")
    g.add_argument("--solid", help=f"one flat ink: {', '.join(INK_NAMES)}")
    g.add_argument("--target", action="store_true", help="the calibration target")
    g.add_argument("--bin", type=Path, help="send a raw panel .bin as-is")
    ap.add_argument("--repeat", type=int, default=1, help="repeat the whole sequence N times")
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
    s = serial.Serial(args.port, BAUD, timeout=0.3)
    # Match the console handshake used elsewhere: do not toggle DTR/RTS, which
    # can reset the board on some USB-serial bridges.
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
