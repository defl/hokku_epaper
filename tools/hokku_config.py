#!/usr/bin/env python3
"""Hokku e-paper frame configuration tool.

Generates an NVS partition binary with WiFi credentials and server URL,
then flashes it to the ESP32's NVS partition via esptool. Works any time
the device is connected over USB — no special timing or boot mode needed.

esptool automatically resets the ESP32-S3 into download mode via the
USB-Serial/JTAG interface, flashes the NVS partition, and resets back.

Usage:
    hokku-config set --ssid MyWifi --password secret --url http://hokku.local:8080/hokku/
    hokku-config get
    hokku-config backup [config_backup.json]
    hokku-config restore config_backup.json
    hokku-config erase
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import serial.tools.list_ports

try:
    import esptool as _esptool
except ImportError:
    _esptool = None  # type: ignore[assignment]

# The shared screen library lives under python/ in the repo. When running from a
# source checkout (not installed), add that dir to sys.path so ``hokku`` imports.
try:
    import hokku.screens.huessen_epf1301  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

# Constants + NVS read/build come from the shared huessen_epf1301 library (single source
# of truth, no ESP-IDF dependency). CONFIG_VERSION is re-exported for esp32_setup.
from hokku.screens.huessen_epf1301 import build_nvs_binary as _build_nvs_binary
from hokku.screens.huessen_epf1301 import read_nvs as _read_nvs
from hokku.screens.huessen_epf1301.constants import (
    CONFIG_VERSION,  # noqa: F401 — re-exported for esp32_setup.py
    ESP32S3_PID,
    ESP32S3_VID,
    NVS_OFFSET,
    NVS_SIZE,
    PAGE_ACTIVE,  # noqa: F401 — re-exported for tools tests
)


def find_esp32_port():
    """Auto-detect ESP32-S3 USB Serial/JTAG port."""
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if port.vid == ESP32S3_VID and port.pid == ESP32S3_PID:
            return port.device
    return None


# ── esptool integration ────────────────────────────────────────────


def _flash_nvs(port, nvs_binary):
    """Flash NVS partition binary to ESP32 via esptool."""
    if _esptool is None:
        print("Error: esptool not installed. Run: pip install esptool")
        sys.exit(1)

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(nvs_binary)
        tmp_path = f.name

    try:
        args = [
            "--chip",
            "esp32s3",
            "--port",
            port,
            "--baud",
            "921600",
            "write-flash",
            "--flash-mode",
            "dio",
            hex(NVS_OFFSET),
            tmp_path,
        ]
        print(f"Flashing NVS partition ({len(nvs_binary)} bytes) to {port}...")
        _esptool.main(args)
        print("Flash complete. Device will reset.")
    finally:
        os.unlink(tmp_path)


def _read_nvs_from_device(port):
    """Read NVS partition from ESP32 via esptool."""
    if _esptool is None:
        print("Error: esptool not installed. Run: pip install esptool")
        sys.exit(1)

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        tmp_path = f.name

    try:
        args = [
            "--chip",
            "esp32s3",
            "--port",
            port,
            "--baud",
            "921600",
            "read-flash",
            hex(NVS_OFFSET),
            hex(NVS_SIZE),
            tmp_path,
        ]
        print(f"Reading NVS partition from {port}...")
        _esptool.main(args)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp_path)


# ── Backup helpers ─────────────────────────────────────────────────


def backup_dir():
    d = Path.home() / ".hokku" / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def auto_backup(port):
    """Read current NVS config and save a timestamped backup."""
    try:
        nvs_data = _read_nvs_from_device(port)
        config = _read_nvs(nvs_data)
        if not config:
            print("  No existing config on device (empty NVS)")
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = backup_dir() / f"config_{timestamp}.json"
        with open(backup_file, "w") as f:
            json.dump(config, f, indent=2)
        print(f"  Auto-backup saved to: {backup_file}")
    except Exception as e:
        print(f"  Warning: auto-backup failed: {e}")


def _get_port(args_port):
    """Resolve serial port from args or auto-detect."""
    port = args_port or find_esp32_port()
    if port is None:
        print("Error: No ESP32-S3 device found.")
        print("Make sure the device is connected via USB.")
        sys.exit(1)
    return port


# ── CLI commands ───────────────────────────────────────────────────


def cmd_set(args):
    """Set configuration values by generating and flashing an NVS partition."""
    port = _get_port(args.port)

    # Read existing config first
    print("Reading current configuration...")
    try:
        nvs_data = _read_nvs_from_device(port)
        existing = _read_nvs(nvs_data)
    except Exception:
        existing = {}

    # Auto-backup before writing
    if existing:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = backup_dir() / f"config_{timestamp}.json"
        with open(backup_file, "w") as f:
            json.dump(existing, f, indent=2)
        print(f"  Auto-backup saved to: {backup_file}")

    # Merge new values
    config = dict(existing)
    if args.ssid is not None:
        config["wifi_ssid1"] = args.ssid
    if args.password is not None:
        config["wifi_pass1"] = args.password
    if args.ssid2 is not None:
        config["wifi_ssid2"] = args.ssid2
    if args.password2 is not None:
        config["wifi_pass2"] = args.password2
    if args.order is not None:
        if args.order not in ("primary", "last"):
            print("Error: --order must be 'primary' or 'last'.")
            sys.exit(1)
        config["wifi_order"] = 1 if args.order == "last" else 0
    if args.url is not None:
        config["image_url"] = args.url
    if args.name is not None:
        name_bytes = args.name.encode("utf-8")
        if len(name_bytes) > 64:
            print(f"Error: screen name is {len(name_bytes)} bytes, maximum is 64.")
            sys.exit(1)
        config["screen_name"] = args.name

    if "wifi_ssid1" not in config or "image_url" not in config:
        print("Error: wifi_ssid1 and image_url are required.")
        print("Use --ssid and --url to set them.")
        sys.exit(1)

    # Generate and flash
    nvs_binary = _build_nvs_binary(config)
    _flash_nvs(port, nvs_binary)

    print("\nConfiguration written:")
    pass_keys = {"wifi_pass1", "wifi_pass2"}
    for k, v in config.items():
        print(f"  {k}: {'****' if k in pass_keys else v}")


def cmd_get(args):
    """Read and display current configuration from device."""
    port = _get_port(args.port)
    nvs_data = _read_nvs_from_device(port)
    config = _read_nvs(nvs_data)

    if not config:
        print("No configuration found on device.")
        return

    pass_keys = {"wifi_pass1", "wifi_pass2"}
    print("Current configuration:")
    for key, value in config.items():
        print(f"  {key}: {'****' if key in pass_keys else value}")


def cmd_backup(args):
    """Backup current config to a JSON file."""
    port = _get_port(args.port)
    nvs_data = _read_nvs_from_device(port)
    config = _read_nvs(nvs_data)

    if not config:
        print("No configuration found on device.")
        return

    output = args.file or str(
        backup_dir() / f"config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
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

    port = _get_port(args.port)

    # Auto-backup before writing
    auto_backup(port)

    # Generate and flash
    nvs_binary = _build_nvs_binary(restore_config)
    _flash_nvs(port, nvs_binary)

    print(f"Configuration restored from: {args.file}")


def cmd_erase(args):
    """Erase all configuration (write empty NVS)."""
    port = _get_port(args.port)

    # Auto-backup before erasing
    auto_backup(port)

    # Flash an empty (all 0xFF) NVS partition
    empty = b"\xff" * NVS_SIZE
    _flash_nvs(port, empty)
    print("Configuration erased.")


def main():
    parser = argparse.ArgumentParser(
        prog="hokku-config",
        description="Configure Hokku e-paper frame via USB (flashes NVS partition)",
    )
    parser.add_argument("--port", "-p", help="Serial port (auto-detected if omitted)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    set_parser = subparsers.add_parser("set", help="Set configuration values")
    set_parser.add_argument("--ssid", help="Primary WiFi network name (required)")
    set_parser.add_argument("--password", help="Primary WiFi password")
    set_parser.add_argument("--ssid2", help="Secondary WiFi network name (optional)")
    set_parser.add_argument("--password2", help="Secondary WiFi password")
    set_parser.add_argument(
        "--order",
        help="WiFi order: 'primary' (always try primary first) or 'last' (try last-used first)",
    )
    set_parser.add_argument(
        "--url", help="Image server URL (e.g. http://server:8080/hokku/screen/)"
    )
    set_parser.add_argument("--name", help="Screen name for identification (max 64 bytes)")
    set_parser.set_defaults(func=cmd_set)

    get_parser = subparsers.add_parser("get", help="Read current configuration")
    get_parser.set_defaults(func=cmd_get)

    backup_parser = subparsers.add_parser("backup", help="Backup config to JSON file")
    backup_parser.add_argument(
        "file", nargs="?", help="Output file (default: timestamped in ~/.hokku/backups/)"
    )
    backup_parser.set_defaults(func=cmd_backup)

    restore_parser = subparsers.add_parser("restore", help="Restore config from JSON file")
    restore_parser.add_argument("file", help="JSON file to restore from")
    restore_parser.set_defaults(func=cmd_restore)

    erase_parser = subparsers.add_parser("erase", help="Erase all configuration")
    erase_parser.set_defaults(func=cmd_erase)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
