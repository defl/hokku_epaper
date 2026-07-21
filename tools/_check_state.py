"""Check the device state - is it in firmware console, BROM, or unresponsive?"""

import sys
import time

import serial

s = serial.Serial("COM6", 115200, timeout=0.3)
s.dtr = False
s.rts = False
s.reset_input_buffer()

# Test 1: Firmware console (should echo and respond to newlines)
print("Test 1: firmware console probe (send 3x newline)...")
for _ in range(3):
    s.write(b"\r\n")
    s.flush()
    time.sleep(0.2)
d = s.read(512)
if d:
    text = d.decode("ascii", errors="replace")
    print(f"  console response: {text!r}")
    if "$" in text:
        print("  -> Device is in FIRMWARE MODE (console active)")
        s.close()
        sys.exit(0)
else:
    print("  no response to newlines")

# Test 2: BROM sync — protocol: send 0x55, expect "OK" (0x4F 0x4B)
print("\nTest 2: BROM sync probe (10x send 0x55, expect 'OK')...")
s.reset_input_buffer()
for _ in range(10):
    s.write(bytes([0x55]))
    s.flush()
    time.sleep(0.1)
    d = s.read(4)
    if d:
        print(f"  BROM response: {d.hex()}")
        if b"OK" in d:
            print("  -> Device is in BROM MODE")
        else:
            print("  -> Got data but not 'OK' response")
        s.close()
        sys.exit(0)
print("  no response to BROM sync")

print("\n-> Device is UNRESPONSIVE or COM6 port is dead")
s.close()
