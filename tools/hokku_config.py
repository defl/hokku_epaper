#!/usr/bin/env python3
"""Hokku e-paper frame configuration tool.

Generates an NVS partition binary with WiFi credentials and server URL,
then flashes it to the ESP32's NVS partition via esptool. Works any time
the device is connected over USB — no special timing or boot mode needed.

esptool automatically resets the ESP32-S3 into download mode via the
USB-Serial/JTAG interface, flashes the NVS partition, and resets back.

Usage:
    hokku-config set --ssid MyWifi --password secret --url http://server:8080/hokku/
    hokku-config get
    hokku-config backup [config_backup.json]
    hokku-config restore config_backup.json
    hokku-config erase
"""
import argparse
import csv
import json
import os
import struct
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import serial.tools.list_ports

# ESP32-S3 USB Serial/JTAG VID:PID
ESP32S3_VID = 0x303A
ESP32S3_PID = 0x1001

# NVS partition location (from partitions.csv)
NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000  # 24KB

# NVS namespace used by firmware
NVS_NAMESPACE = "hokku"


def find_esp32_port():
    """Auto-detect ESP32-S3 USB Serial/JTAG port."""
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if port.vid == ESP32S3_VID and port.pid == ESP32S3_PID:
            return port.device
    return None


# ── NVS partition binary generation ────────────────────────────────
# NVS partition format constants
NVS_PAGE_SIZE = 4096
NVS_ENTRY_SIZE = 32

# Entry types (ESP-IDF NVS)
# Note: uint8 (0x01) and namespace share the same type code.
# Namespace entries have ns_idx=0; data entries have ns_idx>0.
U8_TYPE = 0x01       # uint8 (also used for namespace entries)
STR_TYPE = 0x21      # String

# Config version — increment every time NVS config fields change.
# Must match firmware's CONFIG_VERSION. Source of truth is CLAUDE.md.
CONFIG_VERSION = 1

# Page states
PAGE_ACTIVE = 0xFFFFFFFE  # Active page


def _find_nvs_partition_gen():
    """Find ESP-IDF's nvs_partition_gen.py tool."""
    # Check common ESP-IDF installation paths
    candidates = [
        Path(os.environ.get("IDF_PATH", "")) / "components" / "nvs_flash" / "nvs_partition_generator" / "nvs_partition_gen.py",
        Path("C:/esp/v5.5.3/esp-idf/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"),
        Path("/opt/esp-idf/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _find_idf_python():
    """Find the ESP-IDF Python venv interpreter."""
    candidates = [
        Path(os.environ.get("IDF_PYTHON_ENV_PATH", "")) / "Scripts" / "python.exe",
        Path(os.environ.get("IDF_PYTHON_ENV_PATH", "")) / "bin" / "python",
        Path("C:/Espressif/tools/python/v5.5.3/venv/Scripts/python.exe"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _build_nvs_binary(config_dict):
    """Build an NVS partition binary using ESP-IDF's nvs_partition_gen.py.

    Creates a CSV with the config values and calls the ESP-IDF tool to
    generate a properly formatted NVS partition binary.
    """
    import subprocess

    nvs_gen = _find_nvs_partition_gen()
    idf_python = _find_idf_python()

    if nvs_gen is None:
        raise RuntimeError(
            "Cannot find ESP-IDF nvs_partition_gen.py. "
            "Set IDF_PATH environment variable or install ESP-IDF."
        )
    if idf_python is None:
        raise RuntimeError(
            "Cannot find ESP-IDF Python environment. "
            "Set IDF_PYTHON_ENV_PATH or install ESP-IDF."
        )

    # Build CSV: key,type,encoding,value
    csv_lines = ["key,type,encoding,value"]
    csv_lines.append(f"{NVS_NAMESPACE},namespace,,")
    csv_lines.append(f"cfg_ver,data,u8,{CONFIG_VERSION}")
    for key, value in config_dict.items():
        # Escape commas in values
        escaped = value.replace('"', '""')
        csv_lines.append(f'{key},data,string,"{escaped}"')

    csv_content = "\n".join(csv_lines) + "\n"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write(csv_content)
        csv_path = f.name

    bin_path = csv_path.replace(".csv", ".bin")

    try:
        result = subprocess.run(
            [str(idf_python), str(nvs_gen), "generate", csv_path, bin_path, hex(NVS_SIZE)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"nvs_partition_gen.py failed: {result.stderr}")

        with open(bin_path, "rb") as f:
            return f.read()
    finally:
        for p in [csv_path, bin_path]:
            try:
                os.unlink(p)
            except OSError:
                pass


def _read_nvs(partition_data):
    """Read entries from an NVS partition binary (ESP-IDF format).

    Returns dict of key-value pairs from the 'hokku' namespace.
    String values are returned as str, uint8 values as int.
    """
    result = {}
    if len(partition_data) < NVS_PAGE_SIZE:
        return result

    page = partition_data[:NVS_PAGE_SIZE]

    # Check page state
    state = struct.unpack_from("<I", page, 0)[0]
    if state not in (PAGE_ACTIVE, 0xFFFFFFFC):  # ACTIVE or FULL
        return result

    # Read entries starting at offset 64 (after 32-byte header + 32-byte bitmap)
    offset = 64
    ns_map = {}  # ns_index -> ns_name

    while offset + NVS_ENTRY_SIZE <= NVS_PAGE_SIZE:
        entry = page[offset:offset + NVS_ENTRY_SIZE]
        ns_idx = entry[0]
        entry_type = entry[1]
        span = entry[2]

        if entry_type == 0xFF or span == 0:  # empty or corrupt
            break

        # Read key (bytes 8-23, null-terminated)
        key_raw = entry[8:24]
        null_pos = key_raw.find(b"\x00")
        if null_pos >= 0:
            key = key_raw[:null_pos].decode("utf-8", errors="replace")
        else:
            key = key_raw.decode("utf-8", errors="replace").rstrip("\xff")

        if entry_type == U8_TYPE and ns_idx == 0:
            # Namespace entry: ns_idx=0, type=0x01, value is the namespace index
            ns_map[entry[24]] = key
        elif entry_type == U8_TYPE and ns_map.get(ns_idx) == NVS_NAMESPACE:
            result[key] = entry[24]  # uint8 value
        elif entry_type == STR_TYPE and ns_map.get(ns_idx) == NVS_NAMESPACE:
            str_len = struct.unpack_from("<H", entry, 24)[0]
            # String data is in subsequent entries
            data_offset = offset + NVS_ENTRY_SIZE
            data_bytes = page[data_offset:data_offset + str_len - 1]  # exclude null
            result[key] = data_bytes.decode("utf-8", errors="replace")

        offset += span * NVS_ENTRY_SIZE

    return result


# ── esptool integration ────────────────────────────────────────────

def _flash_nvs(port, nvs_binary):
    """Flash NVS partition binary to ESP32 via esptool."""
    try:
        import esptool
    except ImportError:
        print("Error: esptool not installed. Run: pip install esptool")
        sys.exit(1)

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(nvs_binary)
        tmp_path = f.name

    try:
        args = [
            "--chip", "esp32s3",
            "--port", port,
            "--baud", "921600",
            "write_flash",
            "--flash_mode", "dio",
            hex(NVS_OFFSET), tmp_path,
        ]
        print(f"Flashing NVS partition ({len(nvs_binary)} bytes) to {port}...")
        esptool.main(args)
        print("Flash complete. Device will reset.")
    finally:
        os.unlink(tmp_path)


def _read_nvs_from_device(port):
    """Read NVS partition from ESP32 via esptool."""
    try:
        import esptool
    except ImportError:
        print("Error: esptool not installed. Run: pip install esptool")
        sys.exit(1)

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        tmp_path = f.name

    try:
        args = [
            "--chip", "esp32s3",
            "--port", port,
            "--baud", "921600",
            "read_flash",
            hex(NVS_OFFSET), hex(NVS_SIZE), tmp_path,
        ]
        print(f"Reading NVS partition from {port}...")
        esptool.main(args)
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
        config = _read_nvs_strings(nvs_data)
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
        existing = _read_nvs_strings(nvs_data)
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
        config["wifi_ssid"] = args.ssid
    if args.password is not None:
        config["wifi_pass"] = args.password
    if args.url is not None:
        config["image_url"] = args.url
    if args.name is not None:
        name_bytes = args.name.encode("utf-8")
        if len(name_bytes) > 64:
            print(f"Error: screen name is {len(name_bytes)} bytes, maximum is 64.")
            sys.exit(1)
        config["screen_name"] = args.name

    if "wifi_ssid" not in config or "image_url" not in config:
        print("Error: wifi_ssid and image_url are required.")
        print("Use --ssid and --url to set them.")
        sys.exit(1)

    # Generate and flash
    nvs_binary = _build_nvs_binary(config)
    _flash_nvs(port, nvs_binary)

    print("\nConfiguration written:")
    for k, v in config.items():
        print(f"  {k}: {'****' if k == 'wifi_pass' else v}")


def cmd_get(args):
    """Read and display current configuration from device."""
    port = _get_port(args.port)
    nvs_data = _read_nvs_from_device(port)
    config = _read_nvs_strings(nvs_data)

    if not config:
        print("No configuration found on device.")
        return

    print("Current configuration:")
    for key, value in config.items():
        print(f"  {key}: {'****' if key == 'wifi_pass' else value}")


def cmd_backup(args):
    """Backup current config to a JSON file."""
    port = _get_port(args.port)
    nvs_data = _read_nvs_from_device(port)
    config = _read_nvs_strings(nvs_data)

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
    set_parser.add_argument("--ssid", help="WiFi network name")
    set_parser.add_argument("--password", help="WiFi password")
    set_parser.add_argument("--url", help="Image server URL (e.g. http://server:8080/hokku/screen/)")
    set_parser.add_argument("--name", help="Screen name for identification (max 64 bytes)")
    set_parser.set_defaults(func=cmd_set)

    get_parser = subparsers.add_parser("get", help="Read current configuration")
    get_parser.set_defaults(func=cmd_get)

    backup_parser = subparsers.add_parser("backup", help="Backup config to JSON file")
    backup_parser.add_argument("file", nargs="?", help="Output file (default: timestamped in ~/.hokku/backups/)")
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
