#!/usr/bin/env python3
"""Quick serial monitor for the frame's USB-Serial/JTAG console.

Shows timestamped lines on stdout and (optionally) mirrors them to a
log file. Exits cleanly on Ctrl-C or on loss of the serial port (e.g.
the frame going to deep sleep, or the cable being unplugged).

Usage:
    python serial_quick.py                       # defaults: COM3 @ 115200
    python serial_quick.py --port COM5           # different port
    python serial_quick.py --log session.log     # also write to file
    python serial_quick.py --baud 921600         # different baud

On Linux / macOS the port is typically /dev/ttyACM0 or /dev/cu.usbmodem*.
"""

import argparse
import datetime
import io
import sys

try:
    import serial  # pyserial
except ImportError:
    print("This script needs pyserial. Install with:\n\n    pip install pyserial\n")
    sys.exit(1)


# Force stdout to UTF-8 so unicode chars from the firmware (→, µ, etc.)
# don't crash on Windows' default cp1252 code page.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default="COM3",
                        help="serial port (default: COM3)")
    parser.add_argument("--baud", type=int, default=115200,
                        help="baud rate (default: 115200)")
    parser.add_argument("--log",
                        help="also write timestamped lines to this file")
    args = parser.parse_args()

    try:
        s = serial.Serial(args.port, args.baud, timeout=0.5)
    except serial.SerialException as e:
        print(f"[err] could not open {args.port}: {e}")
        sys.exit(1)

    log_fh = None
    if args.log:
        log_fh = open(args.log, "w", encoding="utf-8", errors="replace")
        print(f"[serial_quick] {args.port} @ {args.baud}, logging to {args.log}")
    else:
        print(f"[serial_quick] {args.port} @ {args.baud}")

    buf = b""
    try:
        while True:
            try:
                chunk = s.read(4096)
            except OSError as e:
                print(f"[serial_quick] serial read error: {e}")
                break
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                text = line.decode("utf-8", errors="replace").rstrip("\r")
                out = f"[{ts}] {text}"
                print(out, flush=True)
                if log_fh:
                    log_fh.write(out + "\n")
                    log_fh.flush()
    except KeyboardInterrupt:
        pass
    finally:
        if log_fh:
            log_fh.close()
        try:
            s.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
