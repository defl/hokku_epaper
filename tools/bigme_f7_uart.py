"""Bigme F7 UART boot log capture tool.

Connects to the XR872AT UART0 via the on-board CH340 bridge and captures
whatever the chip outputs. Run this, then power-cycle the device (unplug/replug
USB, or press+hold the power button) to catch the full boot log.

Usage:
    python tools/bigme_f7_uart.py [--port COM6] [--baud 115200] [--duration 15]

Output is printed to stdout and saved to .private/screens/bigme_f7/uart_log_<timestamp>.txt
"""

import argparse
import datetime
import pathlib
import sys
import time

import serial


def main() -> None:
    parser = argparse.ArgumentParser(description="Bigme F7 UART boot log capture")
    parser.add_argument("--port", default="COM6")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--duration", type=int, default=15, help="Seconds to capture")
    args = parser.parse_args()

    log_dir = pathlib.Path(__file__).parents[1] / ".private/screens/bigme_f7"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"uart_log_{ts}.txt"

    print(f"Opening {args.port} at {args.baud} baud")
    print(f"Log -> {log_path}")
    print("Now power-cycle the device (unplug+replug USB or hold power button)")
    print(f"Capturing for {args.duration}s — Ctrl+C to stop early\n")
    print("-" * 60)

    lines = []
    ser = None
    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.1)
        deadline = time.monotonic() + args.duration
        buf = b""
        while time.monotonic() < deadline:
            try:
                chunk = ser.read(256)
            except serial.SerialException:
                break
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    decoded = line.replace(b"\r", b"").decode("utf-8", errors="replace")
                    print(decoded)
                    lines.append(decoded)
    except KeyboardInterrupt:
        pass
    except serial.SerialException as e:
        print(f"Error opening port: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if ser:
            try:
                ser.close()
            except Exception as close_err:
                print(f"Warning: port close error: {close_err}", file=sys.stderr)

    print("-" * 60)
    print(f"\nCaptured {len(lines)} lines")

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"# Bigme F7 UART log — {args.port} @ {args.baud} baud — {ts}\n\n")
        f.write("\n".join(lines))
    print(f"Saved to {log_path}")


if __name__ == "__main__":
    main()
