"""Watch for a cold-boot and print everything received, no upgrade trigger.

Diagnostic: confirms the boot log arrives on the right port after long-press.
Also detects port changes after the USB bounce.
"""

import contextlib
import sys
import time

import serial
from serial.tools import list_ports

CH340_VID = 0x1A86
CH340_PID = 0x7523


def find_port():
    for p in list_ports.comports():
        if p.vid == CH340_VID and p.pid == CH340_PID:
            return p.device
    return None


port = find_port()
if not port:
    print("No CH340 found")
    sys.exit(1)

print(f"Watching {port}. LONG-press the power button now. Running for 60s...")
s = serial.Serial(port, 115200, timeout=0.1)
s.dtr = False
s.rts = False

t0 = time.monotonic()
total_rx = 0
while time.monotonic() - t0 < 60.0:
    # Check for port changes
    new_port = find_port()
    if new_port and new_port != port:
        print(f"\n!!! Port changed: {port} -> {new_port}")
        with contextlib.suppress(Exception):
            s.close()
        port = new_port
        s = serial.Serial(port, 115200, timeout=0.1)
        s.dtr = False
        s.rts = False

    r = b""
    try:
        r = s.read(256)
    except serial.SerialException:
        print(f"\n!!! SerialException on {port} — port bounced")
        with contextlib.suppress(Exception):
            s.close()
        # Wait for reconnect (check every 50ms)
        reconnected = False
        for _ in range(40):
            time.sleep(0.05)
            new_port = find_port()
            if new_port:
                port = new_port
                print(f"!!! Reconnected on {port}")
                s = serial.Serial(port, 115200, timeout=0.1)
                s.dtr = False
                s.rts = False
                reconnected = True
                break
        if not reconnected:
            print("!!! Could not reconnect after 2s")
            continue

    if r:
        total_rx += len(r)
        # Print hex + ASCII
        for b in r:
            c = chr(b)
            sys.stdout.write(c if 32 <= b < 127 or b in (10, 13) else f"[{b:02X}]")
        sys.stdout.flush()

print(f"\nDone. Total RX bytes: {total_rx}")
s.close()
