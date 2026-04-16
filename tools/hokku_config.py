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
# The ESP-IDF NVS library uses a specific binary format. Rather than
# depending on the full ESP-IDF nvs_partition_gen.py, we generate the
# binary directly. NVS format:
#   - Page size: 4096 bytes
#   - Page header: 32 bytes (state, seq, version, crc)
#   - 126 entries per page, each 32 bytes
#   - Namespace entry: type=1 (uint8), key=namespace name
#   - String entry: type=0x21, span=1+ceil(len/32), data across entries

NVS_PAGE_SIZE = 4096
NVS_ENTRY_SIZE = 32
NVS_ENTRIES_PER_PAGE = 126
NVS_VERSION = 0xFE  # NVS version 2

# Entry types
NS_TYPE = 0x01       # Namespace (stored as uint8)
STR_TYPE = 0x21      # String type

# Page states
PAGE_ACTIVE = 0xFFFFFFFE  # Active page


def _crc32(data):
    """CRC32 matching ESP-IDF NVS implementation."""
    import binascii
    return binascii.crc32(data) & 0xFFFFFFFF


def _build_nvs_binary(config_dict):
    """Build an NVS partition binary from a dict of key-value string pairs.

    All values are stored as strings in the 'hokku' namespace.
    Returns bytes of the full NVS partition (NVS_SIZE bytes).
    """
    page = bytearray(b"\xff" * NVS_PAGE_SIZE)

    # ── Page header (32 bytes) ──
    # Bytes 0-3: page state (ACTIVE = 0xFFFFFFFE)
    struct.pack_into("<I", page, 0, PAGE_ACTIVE)
    # Bytes 4-7: sequence number
    struct.pack_into("<I", page, 4, 0)
    # Bytes 8: version
    page[8] = NVS_VERSION
    # Bytes 9-31: reserved (zeros)
    # CRC of header bytes 4-27
    hdr_crc = _crc32(page[4:28])
    struct.pack_into("<I", page, 28, hdr_crc)

    # Entry bitmap: 126 entries, 2 bits each = 252 bits = 32 bytes
    # Starts at offset 32. 0b11 = empty, 0b10 = written
    bitmap_offset = 32
    bitmap = bytearray(32)
    for i in range(32):
        bitmap[i] = 0xFF  # all entries empty initially

    # Entries start at offset 64
    entry_offset = 64
    entry_idx = 0

    def set_bitmap(idx, span):
        """Mark entries as written (0b10) in the bitmap."""
        for i in range(idx, idx + span):
            byte_pos = (i * 2) // 8
            bit_pos = (i * 2) % 8
            bitmap[byte_pos] &= ~(0x03 << bit_pos)  # clear both bits
            bitmap[byte_pos] |= (0x02 << bit_pos)    # set to 0b10 (written)

    def write_entry(ns_idx, entry_type, key, data_bytes, span=1):
        """Write a single entry (or multi-span entry) to the page."""
        nonlocal entry_offset, entry_idx

        entry = bytearray(NVS_ENTRY_SIZE)
        entry[0] = ns_idx          # namespace index
        entry[1] = entry_type      # type
        entry[2] = span            # span (number of entries this takes)
        entry[3] = 0xFF            # chunk index (0xFF = no chunk)

        # CRC of data (bytes 8-31 of first entry + subsequent entries)
        # Key: bytes 8-23 (16 bytes, null-terminated)
        key_bytes = key.encode("utf-8")[:15] + b"\x00"
        entry[8:8 + len(key_bytes)] = key_bytes

        if entry_type == NS_TYPE:
            # Namespace: data is uint8 namespace index in bytes 24-31
            entry[24] = ns_idx
            data_crc = _crc32(entry[8:32])
            struct.pack_into("<I", entry, 4, data_crc)
            page[entry_offset:entry_offset + NVS_ENTRY_SIZE] = entry
            set_bitmap(entry_idx, 1)
            entry_offset += NVS_ENTRY_SIZE
            entry_idx += 1

        elif entry_type == STR_TYPE:
            # String: size in bytes 24-25, then data in subsequent entries
            str_len = len(data_bytes) + 1  # include null terminator
            struct.pack_into("<H", entry, 24, str_len)

            # Subsequent entry data
            padded = data_bytes + b"\x00"
            # Pad to multiple of 32 bytes
            while len(padded) % NVS_ENTRY_SIZE != 0:
                padded += b"\xff"

            # Calculate CRC over bytes 8-31 of first entry + all subsequent data
            crc_data = bytes(entry[8:32]) + padded
            data_crc = _crc32(crc_data)
            struct.pack_into("<I", entry, 4, data_crc)

            page[entry_offset:entry_offset + NVS_ENTRY_SIZE] = entry
            set_bitmap(entry_idx, span)
            entry_offset += NVS_ENTRY_SIZE
            entry_idx += 1

            # Write subsequent entries with string data
            for i in range(0, len(padded), NVS_ENTRY_SIZE):
                chunk = padded[i:i + NVS_ENTRY_SIZE]
                page[entry_offset:entry_offset + NVS_ENTRY_SIZE] = chunk
                entry_offset += NVS_ENTRY_SIZE
                entry_idx += 1

    # Write namespace entry (index 1)
    write_entry(0x01, NS_TYPE, NVS_NAMESPACE, b"")

    # Write string entries
    for key, value in config_dict.items():
        data = value.encode("utf-8")
        # Span = 1 (header) + ceil((len+1) / 32) for data entries
        data_entries = (len(data) + 1 + NVS_ENTRY_SIZE - 1) // NVS_ENTRY_SIZE
        span = 1 + data_entries
        write_entry(0x01, STR_TYPE, key, data, span)

    # Write bitmap
    page[bitmap_offset:bitmap_offset + 32] = bitmap

    # Build full partition (pad remaining pages with 0xFF)
    partition = bytes(page) + b"\xff" * (NVS_SIZE - NVS_PAGE_SIZE)
    return partition


def _read_nvs_strings(partition_data):
    """Read string entries from an NVS partition binary.

    Returns dict of key-value pairs from the 'hokku' namespace.
    """
    result = {}
    if len(partition_data) < NVS_PAGE_SIZE:
        return result

    page = partition_data[:NVS_PAGE_SIZE]

    # Check page state
    state = struct.unpack_from("<I", page, 0)[0]
    if state not in (PAGE_ACTIVE, 0xFFFFFFFC):  # ACTIVE or FULL
        return result

    # Read entries
    offset = 64  # skip header + bitmap
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
        key = key_raw.split(b"\x00")[0].decode("utf-8", errors="replace")

        if entry_type == NS_TYPE:
            ns_map[entry[24]] = key
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
    set_parser.add_argument("--url", help="Image server URL (e.g. http://server:8080/hokku/)")
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
