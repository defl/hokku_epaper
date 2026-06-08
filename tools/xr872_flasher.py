#!/usr/bin/env python3
"""XR872AT BROM flasher for Bigme F7 e-paper display.

Protocol reverse-engineered from the phoenixMC Linux ELF binary (x86-64).

Full command set:
  identify  — sync and print flash ID
  read      — read arbitrary flash range to file
  dump      — dump entire 8 MB flash
  erase     — erase one flash sector
  reboot    — trigger firmware SysReboot (must already be in BROM mode)

Typical flow (device in firmware mode):
  python xr872_flasher.py --upgrade identify
  python xr872_flasher.py --upgrade dump --output flash.bin

If the device is already in BROM mode (e.g. manual reset):
  python xr872_flasher.py identify
"""

import argparse
import struct
import sys
import time
from typing import Optional

import serial

# ── Frame constants ──────────────────────────────────────────────────────────
BROM_MAGIC = b"BROM"
SYNC_BYTE = 0x55
SYNC_OK = b"OK"  # expected 2-byte response to SYNC_BYTE

CMD_CHANGE_BAUD = 0x10
CMD_SYS_REBOOT = 0x12
CMD_GET_FLASH_ID = 0x18
CMD_ERASE_FLASH = 0x19
CMD_READ_SECTOR = 0x1A
CMD_WRITE_SECTOR = 0x1B

BAUD_FLAGS = 0x03000000  # upper-byte flags OR'd into baud in ChangeBaud
DEFAULT_PORT = "COM6"
DEFAULT_BAUD = 115200
FAST_BAUD = 921600
RESP_HDR_LEN = 12  # every BROM response is 12 bytes


# ── CRC helpers ──────────────────────────────────────────────────────────────
#
# Formula reversed from CFlashHost methods in phoenixMC ELF:
#   1. Compute an intermediate 16-bit sum ("ax") from the partial frame bytes
#      that are already set when the CRC code runs (frame is zero-filled first,
#      only byte[4]=4 is always pre-set; other constants are embedded in the
#      subtracted magic constant per command).
#   2. ax = NOT(ax) & 0xFFFF
#   3. ax = byte-swap (rol ax, 8)
#   4. Store ax LE at frame[6..7]


def _crc_finish(ax: int) -> int:
    ax = (~ax) & 0xFFFF
    return ((ax << 8) | (ax >> 8)) & 0xFFFF


def _crc_getflashid() -> int:
    # only word[4..5]=0x0004 is set; everything else zeroed
    return _crc_finish((0x0004 - 0x6056) & 0xFFFF)


def _crc_sysreboot() -> int:
    return _crc_finish((0x0004 - 0x605C) & 0xFFFF)


def _crc_changebaud(baud: int) -> int:
    # baud_arg upper byte (= 0x03 when BAUD_FLAGS is applied) contributes
    msb = ((baud | BAUD_FLAGS) >> 24) & 0xFF
    ax = (msb + 0x0004 + 0x0010 - 0x606A) & 0xFFFF
    return _crc_finish(ax)


def _crc_readsector(length: int) -> int:
    # length MSB (byte[17] on wire, i.e. length >> 24) contributes
    len_msb = (length >> 24) & 0xFF
    ax = (len_msb + 0x0004 + 0x001A - 0x6066) & 0xFFFF
    return _crc_finish(ax)


def _crc_eraseflash(addr: int) -> int:
    # Both halves of addr contribute; additionally byte[13]=0x19, byte[14]=0x03
    ax = (0x0319 + 0x0004 + (addr & 0xFFFF) - 0x6069) & 0xFFFF
    ax = (ax + (addr >> 16)) & 0xFFFF
    return _crc_finish(ax)


# ── Frame builders ────────────────────────────────────────────────────────────


def _frame(cmd: int, count: bytes, payload: bytes, crc: int) -> bytes:
    """Assemble: BROM(4) + type(1) + pad(1) + CRC(2) + count(4) + cmd(1) + payload"""
    buf = bytearray(13 + len(payload))
    buf[0:4] = BROM_MAGIC
    buf[4] = 0x04
    buf[5] = 0x00
    buf[6] = crc & 0xFF
    buf[7] = (crc >> 8) & 0xFF
    buf[8:12] = count
    buf[12] = cmd
    buf[13:] = payload
    return bytes(buf)


def frame_getflashid() -> bytes:
    return _frame(CMD_GET_FLASH_ID, b"\x00\x00\x00\x01", b"", _crc_getflashid())


def frame_sysreboot() -> bytes:
    return _frame(CMD_SYS_REBOOT, b"\x00\x00\x00\x01", b"", _crc_sysreboot())


def frame_changebaud(baud: int) -> bytes:
    payload = struct.pack(">I", baud | BAUD_FLAGS)
    return _frame(CMD_CHANGE_BAUD, b"\x00\x00\x00\x05", payload, _crc_changebaud(baud))


def frame_readsector(addr: int, length: int) -> bytes:
    payload = struct.pack(">II", addr, length)
    return _frame(CMD_READ_SECTOR, b"\x00\x00\x00\x09", payload, _crc_readsector(length))


def frame_eraseflash(addr: int) -> bytes:
    # payload: erase-type byte 0x03 + BE addr
    payload = bytes([0x03]) + struct.pack(">I", addr)
    return _frame(CMD_ERASE_FLASH, b"\x00\x00\x00\x06", payload, _crc_eraseflash(addr))


# ── Protocol driver ───────────────────────────────────────────────────────────


class XR872Flasher:
    """Communicate with the XR872AT BROM over serial."""

    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD, verbose: bool = False):
        self.verbose = verbose
        self.ser = serial.Serial(port, baud, timeout=2.0)
        self.ser.dtr = False
        self.ser.rts = False
        self._log(f"opened {port} @ {baud}")

    def close(self):
        if self.ser.is_open:
            self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _log(self, msg: str):
        if self.verbose:
            print(f"  [dbg] {msg}", file=sys.stderr)

    def _read_exact(self, n: int, timeout: float = 2.0) -> bytes:
        """Read exactly n bytes within timeout seconds."""
        buf = b""
        deadline = time.monotonic() + timeout
        while len(buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            self.ser.timeout = min(0.05, left)
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf += chunk
        return buf

    def sync(self, attempts: int = 10, timeout_per: float = 0.5) -> bool:
        """Send 0x55 sync byte repeatedly until BROM replies with 'OK'."""
        self.ser.reset_input_buffer()
        for i in range(attempts):
            self._log(f"sync {i + 1}/{attempts}: → 0x55")
            self.ser.write(bytes([SYNC_BYTE]))
            self.ser.flush()
            # Read bytes until we see "OK" or the per-attempt timeout expires
            buf = b""
            deadline = time.monotonic() + timeout_per
            while time.monotonic() < deadline:
                self.ser.timeout = 0.05
                chunk = self.ser.read(4)
                if chunk:
                    buf += chunk
                    if SYNC_OK in buf:
                        self._log(f"  got OK (buf={buf.hex()})")
                        return True
            self._log(f"  no OK (buf={buf.hex()})")
            self.ser.reset_input_buffer()
        return False

    def _send_cmd(self, frame: bytes) -> Optional[bytes]:
        """Send a command frame and return the 12-byte BROM response, or None."""
        self._log(f"TX {len(frame)}B: {frame.hex()}")
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        time.sleep(0.05)  # BROM needs ~20 ms to process before replying
        resp = self._read_exact(RESP_HDR_LEN, timeout=2.0)
        self._log(f"RX {len(resp)}B: {resp.hex()}")
        if len(resp) < RESP_HDR_LEN:
            self._log(f"short response ({len(resp)}/{RESP_HDR_LEN})")
            return None
        return resp

    def _ack_ok(self, resp: Optional[bytes]) -> bool:
        """Return True if resp is a valid success response (BROM magic, status 0)."""
        if not resp or len(resp) < 5:
            return False
        if resp[0:4] != BROM_MAGIC:
            self._log(f"bad magic: {resp[0:4].hex()}")
            return False
        if resp[4] & 0x01:
            self._log(f"error status: 0x{resp[4]:02X}")
            return False
        return True

    # ── Public API ─────────────────────────────────────────────────────────

    def get_flash_id(self) -> Optional[int]:
        """Return flash manufacturer ID byte (at response[5]) or None."""
        resp = self._send_cmd(frame_getflashid())
        if not self._ack_ok(resp) or resp is None:
            return None
        return resp[5]

    def sys_reboot(self) -> bool:
        return self._ack_ok(self._send_cmd(frame_sysreboot()))

    def change_baud(self, baud: int) -> bool:
        """Send ChangeBaud command and update host serial baud rate."""
        if not self._ack_ok(self._send_cmd(frame_changebaud(baud))):
            return False
        time.sleep(0.1)
        self.ser.baudrate = baud
        time.sleep(0.05)
        return True

    def read_sector(self, addr: int, length: int) -> Optional[bytes]:
        """Read length bytes from flash at addr; returns bytes or None on error."""
        resp = self._send_cmd(frame_readsector(addr, length))
        if not self._ack_ok(resp):
            self._log(f"ReadSector ACK fail @ 0x{addr:08X}+{length}")
            return None
        # BROM streams data immediately after the 12-byte ACK
        bps = self.ser.baudrate / 10.0  # bytes per second (10-bit frames)
        timeout = (length / bps) * 2.0 + 3.0  # generous: 2× data time + 3 s
        data = self._read_exact(length, timeout=timeout)
        if len(data) != length:
            self._log(f"short data: {len(data)}/{length}")
            return None
        return data

    def erase_flash(self, addr: int) -> bool:
        """Erase flash sector at addr."""
        return self._ack_ok(self._send_cmd(frame_eraseflash(addr)))

    def connect(self, fast: bool = True) -> bool:
        """Full connect: sync → GetFlashId → optional baud switch + re-sync."""
        print(f"Syncing with BROM on {self.ser.port}@{self.ser.baudrate}...")
        if not self.sync():
            print("ERROR: BROM sync failed — device not in BROM mode")
            return False
        print("  Sync OK")

        flash_id = self.get_flash_id()
        if flash_id is None:
            print("ERROR: GetFlashId failed")
            return False
        print(f"  Flash manufacturer ID: 0x{flash_id:02X}")

        if fast and self.ser.baudrate != FAST_BAUD:
            print(f"  Switching to {FAST_BAUD} baud...")
            if self.change_baud(FAST_BAUD):
                if self.sync():
                    print(f"  Re-sync OK @ {FAST_BAUD} baud")
                else:
                    print(f"  Re-sync failed — reverting to {DEFAULT_BAUD}")
                    self.ser.baudrate = DEFAULT_BAUD
                    if not self.sync():
                        print("ERROR: Fallback sync also failed")
                        return False
            else:
                print("  ChangeBaud failed — staying at 115200")

        return True


# ── Firmware upgrade trigger ──────────────────────────────────────────────────


def send_upgrade_command(port: str, baud: int = DEFAULT_BAUD) -> None:
    """Send 'upgrade\\n' to running firmware to trigger BROM mode via watchdog reset.

    The firmware console expects a bare LF (0x0A), not CR+LF.
    """
    print(f"Sending upgrade command on {port}...")
    s = serial.Serial(port, baud, timeout=0.5)
    s.dtr = False
    s.rts = False
    s.reset_input_buffer()

    # Drain any pending console output
    for _ in range(3):
        s.write(b"\r\n")
        s.flush()
        time.sleep(0.15)
    s.read(512)

    # Trigger BROM mode — must be \n (0x0A) only, not \r\n
    s.reset_input_buffer()
    s.write(b"upgrade\n")
    s.flush()

    deadline = time.monotonic() + 2.0
    response = b""
    while time.monotonic() < deadline:
        chunk = s.read(256)
        if chunk:
            response += chunk
    s.close()

    if response:
        print(f"  Firmware response: {response!r}")
    else:
        print("  (no response — device may already be in BROM mode or sleeping)")


# ── CLI commands ──────────────────────────────────────────────────────────────


def _enter_brom(args) -> bool:
    """Send upgrade command if --upgrade was requested, then wait for BROM boot."""
    if args.upgrade:
        send_upgrade_command(args.port)
        print("Waiting 2 s for BROM to start...")
        time.sleep(2.0)
    return True


def cmd_identify(args) -> int:
    _enter_brom(args)
    with XR872Flasher(args.port, verbose=args.verbose) as f:
        if not f.connect(fast=False):
            return 1
        print("Device identified successfully.")
        return 0


def cmd_read(args) -> int:
    _enter_brom(args)
    addr = int(args.addr, 0)
    length = int(args.length, 0)

    with XR872Flasher(args.port, verbose=args.verbose) as f:
        if not f.connect():
            return 1

        CHUNK = 0x10000  # 64 KB per ReadSector call
        total = b""
        offset = 0
        while offset < length:
            n = min(CHUNK, length - offset)
            caddr = addr + offset
            print(f"  reading 0x{caddr:08X} ({n} bytes)...", end=" ", flush=True)
            data = f.read_sector(caddr, n)
            if data is None:
                print("FAILED")
                return 1
            print("OK")
            total += data
            offset += n

        outfile = args.output or f"flash_0x{addr:08X}_0x{length:X}.bin"
        with open(outfile, "wb") as fh:
            fh.write(total)
        print(f"Saved {len(total)} bytes to {outfile}")
        return 0


def cmd_dump(args) -> int:
    _enter_brom(args)
    FLASH_SIZE = 0x800000  # 8 MB
    CHUNK = 0x10000  # 64 KB per call

    with XR872Flasher(args.port, verbose=args.verbose) as f:
        if not f.connect():
            return 1

        outfile = args.output or "flash_full_8mb.bin"
        print(f"Dumping {FLASH_SIZE // 1024 // 1024} MB flash to {outfile}...")

        with open(outfile, "wb") as fh:
            offset = 0
            while offset < FLASH_SIZE:
                n = min(CHUNK, FLASH_SIZE - offset)
                pct = offset * 100 // FLASH_SIZE
                print(f"  [{pct:3d}%] 0x{offset:08X}...", end="\r", flush=True)
                data = f.read_sector(offset, n)
                if data is None:
                    print(f"\nFAILED at 0x{offset:08X}")
                    return 1
                fh.write(data)
                offset += n

        print(f"\nDone — {FLASH_SIZE // 1024 // 1024} MB saved to {outfile}")
        return 0


def cmd_erase(args) -> int:
    _enter_brom(args)
    addr = int(args.addr, 0)

    with XR872Flasher(args.port, verbose=args.verbose) as f:
        if not f.connect():
            return 1

        print(f"  erasing at 0x{addr:08X}...", end=" ", flush=True)
        if not f.erase_flash(addr):
            print("FAILED")
            return 1
        print("OK")
        return 0


def cmd_reboot(args) -> int:
    with XR872Flasher(args.port, verbose=args.verbose) as f:
        if not f.sync():
            print("ERROR: BROM sync failed")
            return 1
        if not f.sys_reboot():
            print("ERROR: SysReboot command failed")
            return 1
        print("Reboot command sent.")
        return 0


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(
        description="XR872AT BROM flasher — Bigme F7 e-paper display",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--port", default=DEFAULT_PORT, help="Serial port (default: COM6)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument(
        "--upgrade",
        "-u",
        action="store_true",
        help="Send 'upgrade' command to running firmware before connecting",
    )

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("identify", help="Connect and print flash manufacturer ID")

    p = sub.add_parser("read", help="Read a range of flash bytes to a file")
    p.add_argument("addr", help="Start address, e.g. 0x000000")
    p.add_argument("length", help="Byte count, e.g. 0x10000")
    p.add_argument("--output", "-o")

    p = sub.add_parser("dump", help="Dump entire 8 MB flash to a file")
    p.add_argument("--output", "-o")

    p = sub.add_parser("erase", help="Erase one flash sector")
    p.add_argument("addr", help="Sector address, e.g. 0x000000")

    sub.add_parser("reboot", help="Send SysReboot (device must already be in BROM mode)")

    args = ap.parse_args()

    dispatch = {
        "identify": cmd_identify,
        "read": cmd_read,
        "dump": cmd_dump,
        "erase": cmd_erase,
        "reboot": cmd_reboot,
    }
    sys.exit(dispatch[args.cmd](args) or 0)


if __name__ == "__main__":
    main()
