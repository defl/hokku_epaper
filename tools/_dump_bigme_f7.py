"""Full-flash backup of a (working, OEM) Bigme F7 over UART.

LONG-press cold-boots the device: OEM firmware prints a boot log, then runs a
console that accepts `upgrade` → sets SYS_UPDATE flag → watchdog-resets into
BROM (the CH340 stays powered so COM port does NOT bounce on that second reboot).

Flow:
  1. Auto-detect the CH340 by VID/PID (port number changes after re-enumeration).
  2. While the boot log is arriving, hammer `upgrade\n` every 0.3s.
  3. When the log goes quiet for 1.2s, try BROM sync (device should be in BROM now).
  4. Read 4 MB with 512-byte chunks (only confirmed-working ReadSector size).

Usage: python tools/_dump_bigme_f7.py  -> then LONG-press the power button.
"""

import pathlib
import sys
import time

import serial
from serial.tools import list_ports

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "python"))
from _private import private_root
from hokku.common.xr872.flasher import XR872Flasher

CH340_VID = 0x1A86
CH340_PID = 0x7523
FLASH_SIZE = 0x400000  # 4 MB
CHUNK = 0x40000  # 256 KB (512 sectors) per ReadSector frame
# Output dir: pass a unit folder as argv[1], else default under the private root
# (gitignored; overridable via HOKKU_PRIVATE_DIR — see tools/_private.py).
OUT_DIR = private_root() / "screens/bigme_f7"


def find_port() -> str | None:
    for p in list_ports.comports():
        if p.vid == CH340_VID and p.pid == CH340_PID:
            return p.device
    return None


def _open(port: str) -> serial.Serial | None:
    """Try to open the port; retry briefly to survive re-enumeration races."""
    for _ in range(20):  # up to 1s
        try:
            s = serial.Serial(port, 115200, timeout=0.1)
            s.dtr = False
            s.rts = False
            s.reset_input_buffer()
            return s
        except serial.SerialException:
            time.sleep(0.05)
    return None


def _close(s):
    try:
        s.close()
    except Exception:
        pass


def _try_brom_sync(port: str, attempts: int = 4) -> "XR872Flasher | None":
    """Try BROM sync on `port`, return flasher on success or None."""
    from hokku.common.xr872.flasher import XR872Flasher

    for _ in range(attempts):
        time.sleep(0.3)
        cur_port = find_port() or port
        try:
            f = XR872Flasher(cur_port, verbose=False)
        except serial.SerialException:
            # Port not released by OS yet (PermissionError) — retry next loop
            continue
        # NB: do NOT call set_buffer_size() — on Windows it routes through
        # SetupComm, which the CH340 driver mishandles for the bulk stream and
        # the next ReadSector returns an empty ACK. The device resets itself
        # after ~72 KB anyway, so we rely on automatic resume instead.
        if f.sync(attempts=12, timeout_per=0.2):
            return f
        f.close()
    return None


def enter_brom(overall_timeout: float = 300.0) -> "tuple[XR872Flasher, str] | None":
    """Get the device into BROM and return (flasher, port).

    Recipe (works whether the firmware is awake or already in BROM): send
    `upgrade\\n` -> wait ~0.4 s for the watchdog reset into BROM -> BROM sync.
    Repeat until it syncs. If the device is asleep, a button press wakes it and
    a later iteration catches it. No boot-log window timing required.
    """
    from hokku.common.xr872.flasher import XR872Flasher

    deadline = time.monotonic() + overall_timeout

    # Find the CH340 port (waits up to overall_timeout)
    port = None
    print(f"Looking for CH340 (up to {int(overall_timeout)}s)...")
    while time.monotonic() < deadline:
        port = find_port()
        if port:
            break
        time.sleep(0.1)
    if port is None:
        return None
    print(f"  Found CH340 on {port}. Entering BROM (sending `upgrade`)...")

    while time.monotonic() < deadline:
        cur = find_port() or port
        # 1) Send `upgrade` on a plain console handle to trigger the reset.
        try:
            s = serial.Serial(cur, 115200, timeout=0.2)
            s.dtr = False
            s.rts = False
            s.reset_input_buffer()
            s.write(b"\r\n")
            s.flush()
            time.sleep(0.05)
            s.reset_input_buffer()
            s.write(b"upgrade\n")
            s.flush()
            time.sleep(0.4)  # let the watchdog reset the chip into BROM
            s.close()
        except serial.SerialException:
            time.sleep(0.15)
            continue

        # 2) Re-find the port (it may re-enumerate) and try a BROM sync.
        time.sleep(0.15)
        cur = find_port() or cur
        try:
            f = XR872Flasher(cur, verbose=False)
        except serial.SerialException:
            time.sleep(0.15)
            continue
        if f.sync(attempts=8, timeout_per=0.15):
            print(f"[{time.strftime('%H:%M:%S')}] BROM sync OK on {cur}")
            return f, cur
        f.close()

    return None


def _read_chunk(f: "XR872Flasher", addr: int, nbytes: int) -> tuple[bytes, str | None]:
    """Read exactly `nbytes` (512-aligned) from flash at byte `addr` via one frame.

    frame_readsector converts addr/length to 512-byte SECTOR units (addr>>9,
    len>>9) — the BROM's true wire semantics. A single frame returns exactly
    nbytes; the connection stays in BROM, ready for the next frame.
    """
    from hokku.common.xr872.flasher import frame_readsector

    frame = frame_readsector(addr, nbytes)
    f.ser.reset_input_buffer()
    f.ser.write(frame)
    f.ser.flush()
    time.sleep(0.05)
    ack = f._read_exact(12, timeout=2.0)
    if len(ack) < 12 or ack[0:4] != b"BROM":
        return b"", f"bad ACK @0x{addr:06X}: {ack.hex()}"
    if ack[4] & 0x01:
        return b"", f"error status @0x{addr:06X}: {ack.hex()}"

    buf = bytearray()
    idle = 0
    f.ser.timeout = 0.2
    while len(buf) < nbytes:
        try:
            chunk = f.ser.read(nbytes - len(buf))
        except serial.SerialException as e:
            return bytes(buf), f"serial @0x{addr + len(buf):06X}: {e}"
        if chunk:
            buf.extend(chunk)
            idle = 0
        else:
            idle += 1
            if idle >= 5:  # ~1 s quiet — short read
                return bytes(buf), f"short read @0x{addr:06X}: {len(buf)}/{nbytes}"
    return bytes(buf), None


def main() -> int:
    out_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    result = enter_brom()
    if result is None:
        print("ERROR: timed out waiting for BROM.")
        return 1
    f, port = result
    print(f"BROM sync OK on {port}.")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"flash_dump_{ts}.bin"
    print(
        f"Dumping {FLASH_SIZE // 1024 // 1024} MB to {out} "
        f"(0x{CHUNK:X}-byte frames, chained on one BROM session)..."
    )

    off = 0
    last_pct = -1
    try:
        with open(out, "wb") as fh:
            while off < FLASH_SIZE:
                n = min(CHUNK, FLASH_SIZE - off)
                data, err = _read_chunk(f, off, n)
                if data:
                    fh.seek(off)
                    fh.write(data)
                    fh.flush()
                    off += len(data)
                    pct = off * 100 // FLASH_SIZE
                    if pct != last_pct:
                        print(f"  0x{off:06X} ({pct}%)", flush=True)
                        last_pct = pct
                if off >= FLASH_SIZE:
                    break
                if err is not None:
                    # Connection lost mid-dump — re-enter BROM and retry this
                    # chunk at the (now correct) byte offset.
                    print(f"  stopped: {err}. Re-entering BROM...")
                    f.close()
                    result = enter_brom()
                    if result is None:
                        print("ERROR: could not re-enter BROM to resume.")
                        return 1
                    f, port = result
    finally:
        f.close()

    print(f"Done. Saved: {out}  ({out.stat().st_size:,} bytes)")
    return 0 if off >= FLASH_SIZE else 1


if __name__ == "__main__":
    sys.exit(main())
