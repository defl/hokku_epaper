"""Minimal XR872AT BROM sync test.

Tests two paths:
1. Software: send "upgrade\\n" to running firmware → waits for BROM sync
2. Manual:   user long-presses power button → waits for BROM sync

Protocol (from reversed phoenixMC ELF):
  - Send 0x55 (one byte), expect 2-byte response "OK" (0x4F 0x4B)
  - Repeat until success or timeout

Usage:
    python tools/xr872_brom_test.py [--port COM6] [--baud 115200]
    python tools/xr872_brom_test.py --manual   # skip upgrade cmd, wait for manual BROM entry
"""

import argparse
import sys
import time

import serial

DEFAULT_PORT = "COM6"
DEFAULT_BAUD = 115200
SYNC_BYTE = 0x55  # confirmed from phoenixMC Synchron() disassembly
SYNC_OK = b"OK"  # expected 2-byte response (0x4F 0x4B)
SYNC_TIMEOUT = 10.0  # seconds to wait for BROM sync response


def _open_port(port: str, baud: int) -> serial.Serial:
    s = serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=0.5,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    s.dtr = False
    s.rts = False
    return s


def try_upgrade_cmd(port: str, baud: int) -> bool:
    """Send 'upgrade\\n' to running firmware. Returns True if firmware responded."""
    print(f"[upgrade] Opening {port} at {baud} baud...")
    try:
        s = _open_port(port, baud)
    except serial.SerialException as e:
        print(f"[upgrade] Cannot open port: {e}")
        return False

    # BROM upgrade command: LF only (0x0A), not CR+LF — confirmed from phoenixMC disassembly
    print("[upgrade] Sending 'upgrade\\n'...")
    s.write(b"upgrade\n")
    s.flush()

    deadline = time.monotonic() + 2.0
    response = b""
    while time.monotonic() < deadline:
        chunk = s.read(64)
        if chunk:
            response += chunk
            print(f"[upgrade] RX: {chunk!r}")

    s.close()

    if response:
        print(f"[upgrade] Got response ({len(response)}B) - firmware may have accepted upgrade cmd")
        return True
    else:
        print("[upgrade] No response from running firmware")
        return False


def wait_for_brom_sync(port: str, baud: int, timeout: float = SYNC_TIMEOUT) -> bool:
    """Open port and repeatedly send 0x55 until BROM replies with 'OK' (0x4F 0x4B)."""
    print(f"\n[sync] Opening {port} at {baud} baud...")
    try:
        s = _open_port(port, baud)
    except serial.SerialException as e:
        print(f"[sync] Cannot open port: {e}")
        return False

    s.reset_input_buffer()
    s.reset_output_buffer()

    print(f"[sync] Sending 0x{SYNC_BYTE:02X}, waiting for 'OK', timeout={timeout}s...")
    deadline = time.monotonic() + timeout
    attempt = 0

    while time.monotonic() < deadline:
        s.write(bytes([SYNC_BYTE]))
        s.flush()
        attempt += 1

        # Collect any response bytes for up to 0.3s
        buf = b""
        inner_deadline = time.monotonic() + 0.3
        while time.monotonic() < inner_deadline:
            s.timeout = 0.05
            chunk = s.read(8)
            if chunk:
                buf += chunk
                if SYNC_OK in buf:
                    break

        if buf:
            print(f"[sync] Got response on attempt {attempt}: {buf.hex(' ')}")
            if SYNC_OK in buf:
                print("[sync] BROM sync successful! ('OK' received)")
                s.close()
                return True
            else:
                print(f"[sync] Unexpected data: {buf!r}")
                s.reset_input_buffer()

        if attempt % 20 == 0:
            print(
                f"[sync] {attempt} attempts, {timeout - (deadline - time.monotonic()):.1f}s elapsed..."
            )

    s.close()
    print("[sync] Timeout — no BROM sync response")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="XR872AT BROM sync test")
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Skip upgrade cmd, wait for manual BROM entry (long-press power)",
    )
    parser.add_argument(
        "--sync-timeout",
        type=float,
        default=SYNC_TIMEOUT,
        help=f"Seconds to wait for BROM sync (default {SYNC_TIMEOUT})",
    )
    args = parser.parse_args()

    print(f"XR872AT BROM sync test — port={args.port} baud={args.baud}")
    print()

    if not args.manual:
        print("=== Step 1: Try software upgrade trigger ===")
        upgraded = try_upgrade_cmd(args.port, args.baud)
        if upgraded:
            print("Waiting 2s for device to reboot into BROM mode...")
            time.sleep(2.0)
        else:
            print("Software trigger failed — try manual mode (long-press power button)")
            print("Use --manual flag to skip upgrade cmd and wait for manual BROM entry")
    else:
        print("=== Manual mode ===")
        print("Long-press the F7 power button to enter BROM mode now.")
        print(f"Sync window: {args.sync_timeout}s — starting immediately...")

    print("\n=== Step 2: BROM sync ===")
    synced = wait_for_brom_sync(args.port, args.baud, args.sync_timeout)

    if synced:
        print("\nBROM sync OK — proceed with: python -m hokku.common.xr872.flasher identify")
        sys.exit(0)
    else:
        print("\nBROM sync FAILED")
        print("  - Ensure device is powered and COM6 is correct")
        print("  - Use --upgrade to trigger BROM via firmware, or --manual if using power button")
        print("  - Then run: python tools/xr872_brom_test.py --manual --sync-timeout 30")
        sys.exit(1)


if __name__ == "__main__":
    main()
