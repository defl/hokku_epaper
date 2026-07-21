"""Send 'upgrade' command to running firmware to trigger BROM mode, then sync."""

import time

import serial


def main():
    s = serial.Serial("COM6", 115200, timeout=0.5)
    s.dtr = False
    s.rts = False

    print("Flushing console buffer...")
    s.reset_input_buffer()
    # Send several newlines to get to a clean prompt
    for _ in range(3):
        s.write(b"\r\n")
        s.flush()
        time.sleep(0.2)
    data = s.read(256)
    if data:
        print(f"Console flush: {data!r}")

    print("\nSending 'upgrade' command...")
    s.reset_input_buffer()
    s.write(b"upgrade\n")  # LF only (0x0A), confirmed from phoenixMC disassembly
    s.flush()

    # Read response for up to 3 seconds (firmware will reboot, severing connection)
    deadline = time.monotonic() + 3.0
    response = b""
    while time.monotonic() < deadline:
        chunk = s.read(256)
        if chunk:
            response += chunk
            print(f"  RX: {chunk!r}")

    if not response:
        print("  (no response)")

    s.close()

    if response and (b"200 OK" in response or b"upgrade" in response.lower()):
        print("\nFirmware accepted upgrade command — waiting 2s for reboot into BROM mode...")
        time.sleep(2.0)

        print("Now syncing with BROM...")
        s2 = serial.Serial("COM6", 115200, timeout=0.2)
        s2.reset_input_buffer()
        for i in range(50):
            s2.write(bytes([0x55]))
            s2.flush()
            time.sleep(0.1)
            data = s2.read(4)
            if data and b"OK" in data:
                print(f"BROM sync OK on attempt {i + 1}: {data.hex()}")
                s2.close()
                return
        s2.close()
        print("BROM sync FAILED after upgrade")
    else:
        print("\n'upgrade' command got unexpected response or no response")
        print("Device may need manual BROM entry (long-press power button)")


if __name__ == "__main__":
    main()
