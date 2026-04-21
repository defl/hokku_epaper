"""
Serial log reader with rapid reconnect.

Reads lines from a serial port @ 115200 and prints them with timestamps.
When the port disappears (USB unplug, device reset, etc.) it reconnects
within ~100ms of the port becoming available again.

Usage: python serial_quick.py <PORT>    (e.g. COM3, /dev/ttyUSB0)

Requires: pip install pyserial
"""

import argparse
import sys
import time
from datetime import datetime

import serial
from serial import SerialException
from serial.tools import list_ports

BAUD = 115200
READ_TIMEOUT_S = 0.05       # 50ms — how long each read() blocks
RECONNECT_POLL_S = 0.05     # 50ms — poll for port 20x/sec


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def status(msg: str) -> None:
    """Meta info (connects, disconnects) -> stderr so it doesn't mix with log data on stdout."""
    print(f"[{ts()}] -- {msg}", file=sys.stderr, flush=True)


def port_present(name: str) -> bool:
    return any(p.device.upper() == name.upper() for p in list_ports.comports())


def wait_for_port(name: str) -> None:
    """Block until the port reappears. Polls every RECONNECT_POLL_S."""
    announced = False
    while not port_present(name):
        if not announced:
            status(f"{name} gone - waiting for it to come back")
            announced = True
        time.sleep(RECONNECT_POLL_S)


def open_port(name: str, baud: int) -> serial.Serial:
    return serial.Serial(
        port=name,
        baudrate=baud,
        timeout=READ_TIMEOUT_S,
        write_timeout=0.5,
    )


def read_loop(ser: serial.Serial) -> None:
    """
    Read bytes, split on \\n, print each line with a timestamp.
    Raises SerialException when the port dies -> outer loop reconnects.
    On any exit, flushes any partial line still in the buffer.
    """
    buf = bytearray()
    try:
        while True:
            chunk = ser.read(512)  # returns b"" on timeout, raises on disconnect
            if not chunk:
                continue
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl]).rstrip(b"\r")
                del buf[: nl + 1]
                print(f"[{ts()}] {line.decode('utf-8', errors='replace')}", flush=True)
    finally:
        if buf:
            print(
                f"[{ts()}] {buf.decode('utf-8', errors='replace')}  <partial>",
                flush=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Serial log reader with rapid reconnect.")
    parser.add_argument("port", help="Serial port (e.g. COM3, /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=BAUD, help=f"Baud rate (default {BAUD})")
    args = parser.parse_args()

    status(f"serial_quick started: {args.port} @ {args.baud}")
    while True:
        try:
            if not port_present(args.port):
                wait_for_port(args.port)

            ser = open_port(args.port, args.baud)
            status(f"connected to {args.port}")
            try:
                read_loop(ser)
            finally:
                try:
                    ser.close()
                except Exception:
                    pass

        except SerialException as e:
            status(f"serial error: {e} - reconnecting")
            time.sleep(RECONNECT_POLL_S)
        except KeyboardInterrupt:
            status("stopped by user")
            return
        except Exception as e:
            status(f"unexpected error: {e!r} - reconnecting")
            time.sleep(RECONNECT_POLL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
