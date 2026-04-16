#!/usr/bin/env python3
"""Hokku/Huessen E-Ink Frame Setup

Interactive installer that detects ESP32-S3 devices, reads/writes configuration,
and flashes firmware. Combines device detection, NVS configuration, and firmware
flashing into a single friendly tool.

Usage:
    python hokku_setup.py
"""
import os
import sys
import tempfile
from pathlib import Path

import serial.tools.list_ports

# Import NVS functions from hokku_config
from hokku_config import (
    ESP32S3_VID, ESP32S3_PID,
    NVS_OFFSET, NVS_SIZE, CONFIG_VERSION,
    _build_nvs_binary, _read_nvs,
    find_esp32_port, backup_dir,
)

# Firmware binary locations (relative to this script)
SCRIPT_DIR = Path(__file__).parent
FIRMWARE_DIR = SCRIPT_DIR.parent / "firmware" / "release"

# Flash addresses
BOOTLOADER_OFFSET = 0x0
PARTITION_TABLE_OFFSET = 0x8000
APP_OFFSET = 0x10000


def print_header():
    print()
    print("  Hokku/Huessen E-Ink Frame Setup")
    print("  ================================")
    print()


def scan_devices():
    """Scan for ESP32-S3 devices and read their NVS config.

    Returns list of dicts: {port, description, is_esp32, config}
    """
    all_ports = serial.tools.list_ports.comports()
    devices = []

    for port in all_ports:
        is_esp32 = (port.vid == ESP32S3_VID and port.pid == ESP32S3_PID)
        device = {
            "port": port.device,
            "description": port.description or port.device,
            "is_esp32": is_esp32,
            "config": None,
            "has_hokku_firmware": False,  # True if firmware on device matches a Hokku build
            "config_version_ok": False,
            "firmware_current": None,  # None=unknown, True=matches release, False=differs
        }

        if is_esp32:
            # Check if Hokku firmware is installed by looking for project name
            # in the app binary, and whether it matches the release binary
            try:
                fw_check = check_firmware_current(port.device)
                device["firmware_current"] = fw_check
            except Exception:
                pass

            try:
                device["has_hokku_firmware"] = check_is_hokku_firmware(port.device)
            except Exception:
                pass

            # Read NVS config
            try:
                config = read_nvs_from_device(port.device)
                if config and config.get("cfg_ver") == CONFIG_VERSION:
                    device["config"] = config
                    device["config_version_ok"] = True
                elif config and "cfg_ver" in config:
                    device["config_version_ok"] = False
            except Exception:
                pass

        devices.append(device)

    return devices


def read_nvs_from_device(port):
    """Read NVS config from device. Returns dict or None."""
    try:
        import esptool
    except ImportError:
        return None

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        tmp_path = f.name

    try:
        # Suppress esptool output
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')
        try:
            esptool.main([
                "--chip", "esp32s3",
                "--port", port,
                "--baud", "921600",
                "read_flash",
                hex(NVS_OFFSET), hex(NVS_SIZE), tmp_path,
            ])
        finally:
            sys.stdout.close()
            sys.stdout = old_stdout

        with open(tmp_path, "rb") as f:
            data = f.read()
        return _read_nvs(data)
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def check_firmware_current(port):
    """Compare on-device firmware with release binary. Returns True if matching."""
    app_bin = FIRMWARE_DIR / "hokku_epaper.bin"
    if not app_bin.exists():
        return None  # no release binary to compare against

    try:
        import esptool
    except ImportError:
        return None

    # Read first 256 bytes of app partition from device
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        tmp_path = f.name

    try:
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')
        try:
            esptool.main([
                "--chip", "esp32s3",
                "--port", port,
                "--baud", "921600",
                "read_flash",
                hex(APP_OFFSET), "256", tmp_path,
            ])
        finally:
            sys.stdout.close()
            sys.stdout = old_stdout

        with open(tmp_path, "rb") as f:
            device_header = f.read()

        with open(app_bin, "rb") as f:
            release_header = f.read(256)

        return device_header == release_header
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def check_is_hokku_firmware(port):
    """Check if the device has Hokku firmware by looking for project name in app binary."""
    try:
        import esptool
    except ImportError:
        return False

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        tmp_path = f.name

    try:
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')
        try:
            esptool.main([
                "--chip", "esp32s3",
                "--port", port,
                "--baud", "921600",
                "read_flash",
                hex(APP_OFFSET), "256", tmp_path,
            ])
        finally:
            sys.stdout.close()
            sys.stdout = old_stdout

        with open(tmp_path, "rb") as f:
            header = f.read()

        # ESP-IDF app binary has project name "hokku_epaper" at offset ~80
        return b"hokku_epaper" in header
    except Exception:
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def format_device_line(idx, device):
    """Format a device for display in the selection list."""
    parts = [f"  [{idx}] {device['port']}"]

    if device["is_esp32"]:
        if device["has_hokku_firmware"] and device["config_version_ok"]:
            cfg = device["config"]
            name = cfg.get("screen_name", "")
            ssid = cfg.get("wifi_ssid", "")
            detail = "Hokku firmware"
            if name:
                detail += f", name={name}"
            if ssid:
                detail += f", ssid={ssid}"
            if device["firmware_current"] is False:
                detail += ", firmware update available"
            parts.append(f"ESP32-S3 ({detail})")
        elif device["has_hokku_firmware"] and not device["config_version_ok"]:
            parts.append("ESP32-S3 (Hokku firmware, needs configuration)")
        elif device["has_hokku_firmware"]:
            parts.append("ESP32-S3 (Hokku firmware)")
        else:
            parts.append("ESP32-S3 (no Hokku firmware)")
    else:
        parts.append(device["description"])

    return " - ".join(parts)


def select_device(devices):
    """Let user pick a device. Returns selected device dict or None."""
    esp32_devices = [d for d in devices if d["is_esp32"]]

    if len(esp32_devices) == 1:
        dev = esp32_devices[0]
        print(f"  Found device: {dev['port']}", end="")
        if dev["has_hokku_firmware"]:
            cfg = dev.get("config") or {}
            name = cfg.get("screen_name", "")
            if dev["config_version_ok"] and name:
                print(f" (Hokku firmware, name={name})")
            elif dev["config_version_ok"]:
                print(f" (Hokku firmware, configured)")
            else:
                print(f" (Hokku firmware, needs configuration)")
        else:
            print(" (ESP32-S3)")
        print()
        return dev

    if len(esp32_devices) > 1:
        print(f"  Found {len(esp32_devices)} ESP32-S3 devices:")
        for i, dev in enumerate(esp32_devices, 1):
            print(format_device_line(i, dev))
        print()
        while True:
            choice = input(f"  Select device [1-{len(esp32_devices)}]: ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(esp32_devices):
                    return esp32_devices[idx]
            except ValueError:
                pass
            print("  Invalid choice, try again.")

    # No ESP32-S3 found
    if not devices:
        print("  No serial devices found.")
        print("  Make sure the frame is connected via USB.")
        return None

    print("  No ESP32-S3 devices found. Available serial ports:")
    for i, dev in enumerate(devices, 1):
        print(format_device_line(i, dev))
    print()
    print("  WARNING: None of these appear to be an ESP32-S3.")
    while True:
        choice = input(f"  Select port anyway [1-{len(devices)}] or 'q' to quit: ").strip()
        if choice.lower() == 'q':
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(devices):
                return devices[idx]
        except ValueError:
            pass
        print("  Invalid choice, try again.")


def show_current_config(config):
    """Display current device configuration."""
    if not config:
        print("  No configuration found on device.")
        return

    print("  Current configuration:")
    print(f"    WiFi SSID:     {config.get('wifi_ssid', '(not set)')}")
    print(f"    WiFi Password: {'****' if config.get('wifi_pass') else '(not set)'}")
    print(f"    Server URL:    {config.get('image_url', '(not set)')}")
    print(f"    Screen Name:   {config.get('screen_name', '(not set)')}")
    print()


def prompt_config(existing_config=None):
    """Interactively prompt for configuration values. Returns config dict."""
    cfg = dict(existing_config or {})

    print("  Enter new values (press Enter to keep current):")
    print()

    # WiFi SSID
    current = cfg.get("wifi_ssid", "")
    prompt = f"  WiFi SSID [{current}]: " if current else "  WiFi SSID: "
    val = input(prompt).strip()
    if val:
        cfg["wifi_ssid"] = val
    elif not current:
        print("  WiFi SSID is required.")
        val = input("  WiFi SSID: ").strip()
        if not val:
            print("  Aborted.")
            return None
        cfg["wifi_ssid"] = val

    # WiFi Password
    current = cfg.get("wifi_pass", "")
    prompt = f"  WiFi Password [****]: " if current else "  WiFi Password: "
    val = input(prompt).strip()
    if val:
        cfg["wifi_pass"] = val

    # Server URL
    current = cfg.get("image_url", "")
    prompt = f"  Server URL [{current}]: " if current else "  Server URL (e.g. http://192.168.1.10:8080/hokku/screen/): "
    val = input(prompt).strip()
    if val:
        if not val.startswith("http://") and not val.startswith("https://"):
            print("  WARNING: URL should start with http://")
        cfg["image_url"] = val
    elif not current:
        print("  Server URL is required.")
        val = input("  Server URL: ").strip()
        if not val:
            print("  Aborted.")
            return None
        cfg["image_url"] = val

    # Screen Name
    current = cfg.get("screen_name", "")
    prompt = f"  Screen Name [{current}]: " if current else "  Screen Name (optional, e.g. Living Room): "
    val = input(prompt).strip()
    if val:
        if len(val.encode("utf-8")) > 64:
            print(f"  ERROR: Screen name is {len(val.encode('utf-8'))} bytes, maximum is 64.")
            return None
        cfg["screen_name"] = val

    return cfg


def write_config(port, config):
    """Write NVS config to device."""
    print("  Writing configuration...", end=" ", flush=True)
    nvs_binary = _build_nvs_binary(config)

    try:
        import esptool
    except ImportError:
        print("FAILED")
        print("  Error: esptool not installed. Run: pip install esptool")
        return False

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(nvs_binary)
        tmp_path = f.name

    try:
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')
        try:
            esptool.main([
                "--chip", "esp32s3",
                "--port", port,
                "--baud", "921600",
                "write_flash",
                "--flash_mode", "dio",
                hex(NVS_OFFSET), tmp_path,
            ])
        finally:
            sys.stdout.close()
            sys.stdout = old_stdout
        print("done.")
        return True
    except Exception as e:
        print("FAILED")
        print(f"  Error: {e}")
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def flash_firmware(port):
    """Flash firmware binaries to device."""
    bootloader = FIRMWARE_DIR / "bootloader.bin"
    partition_table = FIRMWARE_DIR / "partition-table.bin"
    app = FIRMWARE_DIR / "hokku_epaper.bin"

    # Check firmware files exist
    missing = []
    for name, path in [("bootloader.bin", bootloader),
                        ("partition-table.bin", partition_table),
                        ("hokku_epaper.bin", app)]:
        if not path.exists():
            missing.append(name)

    if missing:
        print(f"  ERROR: Firmware files not found in {FIRMWARE_DIR}/")
        for m in missing:
            print(f"    Missing: {m}")
        print()
        print("  Build the firmware first, or copy pre-built binaries to:")
        print(f"    {FIRMWARE_DIR}/")
        return False

    try:
        import esptool
    except ImportError:
        print("  Error: esptool not installed. Run: pip install esptool")
        return False

    print("  Flashing firmware (this takes about 30 seconds)...")
    print(f"    Bootloader:      {bootloader.name}")
    print(f"    Partition table:  {partition_table.name}")
    print(f"    Application:     {app.name}")
    print()

    try:
        esptool.main([
            "--chip", "esp32s3",
            "--port", port,
            "--baud", "921600",
            "write_flash",
            "--flash_mode", "dio",
            "--flash_freq", "80m",
            "--flash_size", "16MB",
            hex(BOOTLOADER_OFFSET), str(bootloader),
            hex(PARTITION_TABLE_OFFSET), str(partition_table),
            hex(APP_OFFSET), str(app),
        ])
        print()
        print("  Firmware flashed successfully.")
        return True
    except Exception as e:
        print()
        print(f"  ERROR: Flash failed: {e}")
        return False


def _refresh_device_state(port):
    """Re-read NVS config and firmware status from device."""
    config = None
    firmware_current = None

    try:
        nvs = read_nvs_from_device(port)
        if nvs and nvs.get("cfg_ver") == CONFIG_VERSION:
            config = nvs
    except Exception:
        pass

    try:
        firmware_current = check_firmware_current(port)
    except Exception:
        pass

    return config, firmware_current


def main_menu(device):
    """Show the main menu and handle user choice."""
    port = device["port"]
    config = device.get("config") or {}
    firmware_current = device.get("firmware_current")

    while True:
        print()
        show_current_config(config)

        # Show firmware status
        if firmware_current is True:
            print("  Firmware: up to date")
        elif firmware_current is False:
            print("  Firmware: UPDATE AVAILABLE")
        else:
            print("  Firmware: unknown (no release binaries or unreadable)")
        print()

        # Determine default option
        if firmware_current is False:
            default = "2"
        elif not config:
            default = "2"
        else:
            default = "1"

        print("  What would you like to do?")
        for num, label in [("1", "Update configuration"),
                           ("2", "Flash firmware + configure"),
                           ("3", "Flash firmware only" + (" (keep existing config)" if config else "")),
                           ("4", "Exit")]:
            marker = " <-- default" if num == default else ""
            print(f"    [{num}] {label}{marker}")
        print()

        choice = input(f"  [{default}]> ").strip()
        if choice == "":
            choice = default

        if choice == "1":
            new_config = prompt_config(config)
            if new_config:
                if write_config(port, new_config):
                    config = new_config
                    print("  Device will restart with new configuration.")

        elif choice == "2":
            if flash_firmware(port):
                print()
                print("  Now let's configure the device.")
                print()
                new_config = prompt_config(config)
                if new_config:
                    write_config(port, new_config)
                    config = new_config
                    print("  Setup complete! Device will restart.")
            # Re-read device state after flashing
            print("  Re-reading device state...")
            config, firmware_current = _refresh_device_state(port)
            config = config or {}

        elif choice == "3":
            if flash_firmware(port):
                # Re-read device state after flashing
                print("  Re-reading device state...")
                config, firmware_current = _refresh_device_state(port)
                config = config or {}

        elif choice == "4":
            print("  Bye!")
            break

        else:
            print("  Invalid choice.")


def main():
    print_header()

    # Check esptool is available
    try:
        import esptool
    except ImportError:
        print("  ERROR: esptool is not installed.")
        print("  Run: pip install esptool pyserial")
        sys.exit(1)

    print("  Scanning for devices...")
    devices = scan_devices()
    print()

    device = select_device(devices)
    if device is None:
        sys.exit(1)

    main_menu(device)


if __name__ == "__main__":
    main()
