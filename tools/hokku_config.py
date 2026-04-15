#!/usr/bin/env python3
"""Hokku e-paper frame configuration tool.

Communicates with the ESP32 firmware over USB serial to read/write
WiFi credentials and server URL stored in NVS.

Usage:
    hokku-config set --ssid MyWifi --password secret --url http://server:8080/hokku/
    hokku-config get
    hokku-config backup [config_backup.json]
    hokku-config restore config_backup.json
    hokku-config erase
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import serial
import serial.tools.list_ports


# ESP32-S3 USB Serial/JTAG VID:PID
ESP32S3_VID = 0x303A
ESP32S3_PID = 0x1001

BAUD_RATE = 115200
TIMEOUT = 2.0  # seconds per read
PING_RETRIES = 10
PING_INTERVAL = 0.5


def find_esp32_port():
    """Auto-detect ESP32-S3 USB Serial/JTAG port."""
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if port.vid == ESP32S3_VID and port.pid == ESP32S3_PID:
            return port.device
    return None


def open_serial(port=None):
    """Open serial connection to ESP32. Auto-detects port if not specified."""
    if port is None:
        port = find_esp32_port()
        if port is None:
            print("Error: No ESP32-S3 device found.")
            print("Make sure the device is connected via USB and powered on.")
            print("The device listens for config commands for 5 seconds after boot.")
            sys.exit(1)
    print(f"Connecting to {port}...")
    try:
        ser = serial.Serial(port, BAUD_RATE, timeout=TIMEOUT)
    except serial.SerialException as e:
        print(f"Error: Cannot open {port}: {e}")
        sys.exit(1)
    return ser


def send_command(ser, cmd):
    """Send a HOKKU: command and return list of response lines."""
    full_cmd = f"HOKKU:{cmd}\n"
    ser.write(full_cmd.encode())
    ser.flush()

    responses = []
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        line = ser.readline().decode(errors="replace").strip()
        if not line:
            continue
        if line.startswith("OK:") or line.startswith("ERR:"):
            responses.append(line)
            # Keep reading if more lines expected (GET ALL returns multiple)
            deadline = time.time() + 0.3  # short timeout for additional lines
        # Skip ESP_LOG output (starts with I, W, E, etc.)

    return responses


def wait_for_device(ser):
    """Send PING until device responds or timeout."""
    print("Waiting for device (press reset if needed)...", end="", flush=True)
    for i in range(PING_RETRIES):
        responses = send_command(ser, "PING")
        for r in responses:
            if r == "OK:PONG":
                print(" connected!")
                return True
        print(".", end="", flush=True)
        time.sleep(PING_INTERVAL)
    print(" timeout.")
    print("The device only listens for config commands for 5 seconds after boot.")
    print("Try pressing reset on the device while this tool is running.")
    return False


def parse_response_value(response):
    """Parse 'OK:key=value' into (key, value) tuple."""
    if response.startswith("OK:"):
        kv = response[3:]
        eq = kv.find("=")
        if eq >= 0:
            return kv[:eq], kv[eq + 1:]
    return None, None


def get_all_config(ser):
    """Read all config from device. Returns dict."""
    responses = send_command(ser, "GET ALL")
    config = {}
    for r in responses:
        key, value = parse_response_value(r)
        if key:
            config[key] = value
    return config


def backup_dir():
    """Return path to backup directory, creating it if needed."""
    d = Path.home() / ".hokku" / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def auto_backup(ser):
    """Automatically backup current config before making changes."""
    config = get_all_config(ser)
    if not config:
        return  # nothing to backup

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir() / f"config_{timestamp}.json"
    with open(backup_file, "w") as f:
        json.dump(config, f, indent=2)
    print(f"Auto-backup saved to: {backup_file}")


def cmd_set(args):
    """Set configuration values."""
    ser = open_serial(args.port)
    if not wait_for_device(ser):
        ser.close()
        sys.exit(1)

    # Auto-backup before writing
    auto_backup(ser)

    if args.ssid:
        responses = send_command(ser, f"SET wifi_ssid={args.ssid}")
        for r in responses:
            print(f"  {r}")

    if args.password:
        responses = send_command(ser, f"SET wifi_pass={args.password}")
        for r in responses:
            print(f"  {r}")

    if args.url:
        responses = send_command(ser, f"SET image_url={args.url}")
        for r in responses:
            print(f"  {r}")

    # Send DONE to exit config mode
    send_command(ser, "DONE")
    print("Configuration saved. Device will restart.")
    ser.close()


def cmd_get(args):
    """Read and display current configuration."""
    ser = open_serial(args.port)
    if not wait_for_device(ser):
        ser.close()
        sys.exit(1)

    config = get_all_config(ser)
    send_command(ser, "DONE")
    ser.close()

    if not config:
        print("No configuration found on device.")
        return

    print("Current configuration:")
    for key, value in config.items():
        print(f"  {key}: {value}")


def cmd_backup(args):
    """Backup current config to a JSON file."""
    ser = open_serial(args.port)
    if not wait_for_device(ser):
        ser.close()
        sys.exit(1)

    config = get_all_config(ser)
    send_command(ser, "DONE")
    ser.close()

    if not config:
        print("No configuration found on device.")
        return

    output = args.file or str(backup_dir() / f"config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(output, "w") as f:
        json.dump(config, f, indent=2)
    print(f"Configuration backed up to: {output}")


def cmd_restore(args):
    """Restore config from a JSON file."""
    if not os.path.exists(args.file):
        print(f"Error: File not found: {args.file}")
        sys.exit(1)

    with open(args.file) as f:
        restore_config = json.load(f)

    ser = open_serial(args.port)
    if not wait_for_device(ser):
        ser.close()
        sys.exit(1)

    # Auto-backup before writing
    auto_backup(ser)

    print(f"Restoring from: {args.file}")
    for key, value in restore_config.items():
        if key in ("wifi_ssid", "wifi_pass", "image_url"):
            responses = send_command(ser, f"SET {key}={value}")
            for r in responses:
                print(f"  {r}")

    send_command(ser, "DONE")
    print("Configuration restored. Device will restart.")
    ser.close()


def cmd_erase(args):
    """Erase all configuration from device."""
    ser = open_serial(args.port)
    if not wait_for_device(ser):
        ser.close()
        sys.exit(1)

    # Auto-backup before erasing
    auto_backup(ser)

    responses = send_command(ser, "ERASE")
    for r in responses:
        print(f"  {r}")

    send_command(ser, "DONE")
    print("Configuration erased.")
    ser.close()


def main():
    parser = argparse.ArgumentParser(
        prog="hokku-config",
        description="Configure Hokku e-paper frame over USB",
    )
    parser.add_argument("--port", "-p", help="Serial port (auto-detected if omitted)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # set
    set_parser = subparsers.add_parser("set", help="Set configuration values")
    set_parser.add_argument("--ssid", help="WiFi network name")
    set_parser.add_argument("--password", help="WiFi password")
    set_parser.add_argument("--url", help="Image server URL (e.g. http://server:8080/hokku/)")
    set_parser.set_defaults(func=cmd_set)

    # get
    get_parser = subparsers.add_parser("get", help="Read current configuration")
    get_parser.set_defaults(func=cmd_get)

    # backup
    backup_parser = subparsers.add_parser("backup", help="Backup config to JSON file")
    backup_parser.add_argument("file", nargs="?", help="Output file (default: timestamped in ~/.hokku/backups/)")
    backup_parser.set_defaults(func=cmd_backup)

    # restore
    restore_parser = subparsers.add_parser("restore", help="Restore config from JSON file")
    restore_parser.add_argument("file", help="JSON file to restore from")
    restore_parser.set_defaults(func=cmd_restore)

    # erase
    erase_parser = subparsers.add_parser("erase", help="Erase all configuration")
    erase_parser.set_defaults(func=cmd_erase)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
