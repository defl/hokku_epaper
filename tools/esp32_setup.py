"""ESP32-S3 detection, NVS configuration, and firmware flashing.

Extracted from the old hokku_setup.py so the top-level installer can drive
the Pi-install phase and the ESP32 phase as separate stages.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

import serial
import serial.tools.list_ports

from hokku_config import (
    ESP32S3_VID, ESP32S3_PID,
    NVS_OFFSET, NVS_SIZE, CONFIG_VERSION,
    _build_nvs_binary, _read_nvs,
)

SCRIPT_DIR = Path(__file__).parent
FIRMWARE_DIR = SCRIPT_DIR.parent / "firmware" / "release"

BOOTLOADER_OFFSET = 0x0
PARTITION_TABLE_OFFSET = 0x8000
APP_OFFSET = 0x10000


# -------- device scan --------

def scan_devices():
    """Return list of {port, description, is_esp32, config, ...} for all serial ports."""
    all_ports = serial.tools.list_ports.comports()
    devices = []
    for port in all_ports:
        is_esp32 = (port.vid == ESP32S3_VID and port.pid == ESP32S3_PID)
        device = {
            "port": port.device,
            "description": port.description or port.device,
            "is_esp32": is_esp32,
            "config": None,
            "has_hokku_firmware": False,
            "config_version_ok": False,
            "firmware_current": None,
            "device_version": None,
            "release_version": None,
        }
        if is_esp32:
            nvs_data, app_header = read_device_flash(port.device)
            state = parse_device_state(nvs_data, app_header)
            device.update(state)
        devices.append(device)
    return devices


def read_device_flash(port):
    """One esptool read covering NVS partition + app header. Returns (nvs, header) or (None, None)."""
    try:
        import esptool
    except ImportError:
        return None, None

    read_start = NVS_OFFSET
    read_end = APP_OFFSET + 256
    read_size = read_end - read_start

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        tmp_path = f.name

    try:
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            esptool.main([
                "--chip", "esp32s3",
                "--port", port,
                "--baud", "921600",
                "read-flash",
                hex(read_start), hex(read_size), tmp_path,
            ])
        finally:
            sys.stdout.close()
            sys.stdout = old_stdout

        with open(tmp_path, "rb") as f:
            data = f.read()
        nvs_data = data[:NVS_SIZE]
        app_header = data[APP_OFFSET - NVS_OFFSET:][:256]
        return nvs_data, app_header
    except Exception:
        return None, None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def parse_device_state(nvs_data, app_header):
    """Parse firmware presence, version, config from raw flash bytes."""
    result = {
        "config": None,
        "has_hokku_firmware": False,
        "config_version_ok": False,
        "firmware_current": None,
        "device_version": None,
        "release_version": None,
    }

    if app_header and b"hokku_epaper" in app_header:
        result["has_hokku_firmware"] = True

    if app_header and len(app_header) >= 80:
        ver = app_header[48:80].split(b"\x00")[0].decode("ascii", errors="replace")
        if ver:
            result["device_version"] = ver

    app_bin = FIRMWARE_DIR / "hokku_epaper.bin"
    if app_bin.exists():
        with open(app_bin, "rb") as f:
            release_header = f.read(256)
        if len(release_header) >= 80:
            ver = release_header[48:80].split(b"\x00")[0].decode("ascii", errors="replace")
            if ver:
                result["release_version"] = ver
        if app_header and len(app_header) >= 256 and len(release_header) >= 256:
            # Skip first 24 bytes (esp_image_header_t changed by esptool at flash)
            result["firmware_current"] = (app_header[24:] == release_header[24:])

    if nvs_data:
        config = _read_nvs(nvs_data)
        if config and config.get("cfg_ver") == CONFIG_VERSION:
            result["config"] = config
            result["config_version_ok"] = True
        elif config and "cfg_ver" in config:
            result["config_version_ok"] = False

    return result


# -------- device selection UI --------

def format_device_line(idx, device):
    parts = [f"  [{idx}] {device['port']}"]
    if device["is_esp32"]:
        if device["has_hokku_firmware"] and device["config_version_ok"]:
            cfg = device["config"]
            detail = "Hokku firmware"
            if cfg.get("screen_name"):
                detail += f", name={cfg['screen_name']}"
            if cfg.get("wifi_ssid"):
                detail += f", ssid={cfg['wifi_ssid']}"
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
                print(" (Hokku firmware, configured)")
            else:
                print(" (Hokku firmware, needs configuration)")
        else:
            print(" (ESP32-S3)")
        return dev

    if len(esp32_devices) > 1:
        print(f"  Found {len(esp32_devices)} ESP32-S3 devices:")
        for i, dev in enumerate(esp32_devices, 1):
            print(format_device_line(i, dev))
        while True:
            choice = input(f"  Select device [1-{len(esp32_devices)}]: ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(esp32_devices):
                    return esp32_devices[idx]
            except ValueError:
                pass
            print("  Invalid choice, try again.")

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
        if choice.lower() == "q":
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(devices):
                return devices[idx]
        except ValueError:
            pass
        print("  Invalid choice, try again.")


# -------- config display/prompt --------

def _parse_server_url(url):
    if not url:
        return None, None
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return p.hostname, p.port or 8080
    except Exception:
        return None, None


def show_current_config(config):
    if not config:
        print("  No configuration found on device.")
        return
    ip, port = _parse_server_url(config.get("image_url", ""))
    print("  Current configuration:")
    print(f"    WiFi SSID:     {config.get('wifi_ssid', '(not set)')}")
    print(f"    WiFi Password: {'****' if config.get('wifi_pass') else '(not set)'}")
    print(f"    Server:        {ip or '(not set)'}:{port or 8080}")
    print(f"    Screen Name:   {config.get('screen_name', '(not set)')}")
    print()


def _check_server_reachable(ip, port):
    import urllib.request
    try:
        urllib.request.urlopen(f"http://{ip}:{port}/hokku/api/time", timeout=5)
        return True
    except Exception:
        return False


def prompt_config(existing_config=None, pi_credentials=None):
    """Interactive config. `pi_credentials` (optional) pre-fills wifi/server if the
    user just ran the Pi install. Returns config dict or None on abort."""
    cfg = dict(existing_config or {})
    pi = pi_credentials or {}

    print("  Enter new values (press Enter to keep shown default):")
    print()

    # --- WiFi SSID ---
    default = pi.get("wifi_ssid") or cfg.get("wifi_ssid", "")
    prompt = f"  WiFi SSID [{default}]: " if default else "  WiFi SSID: "
    val = input(prompt).strip()
    if val:
        cfg["wifi_ssid"] = val
    elif default:
        cfg["wifi_ssid"] = default
    else:
        print("  WiFi SSID is required.")
        return None

    # --- WiFi password ---
    pi_pass = pi.get("wifi_pass")
    existing_pass = cfg.get("wifi_pass", "")
    if pi_pass:
        prompt = "  WiFi Password [use Pi install value]: "
    elif existing_pass:
        prompt = "  WiFi Password [****]: "
    else:
        prompt = "  WiFi Password: "
    val = input(prompt).strip()
    if val:
        cfg["wifi_pass"] = val
    elif pi_pass:
        cfg["wifi_pass"] = pi_pass
    # else keep existing

    # --- server IP/port ---
    current_ip, current_port = _parse_server_url(cfg.get("image_url", ""))
    default_ip = pi.get("server_ip") or current_ip
    default_port = current_port or 8080

    prompt = f"  Server IP [{default_ip}]: " if default_ip else "  Server IP (e.g. 192.168.1.10): "
    val = input(prompt).strip()
    if val:
        current_ip = val
    elif default_ip:
        current_ip = default_ip
    else:
        print("  Server IP is required.")
        return None

    prompt = f"  Server Port [{default_port}]: "
    val = input(prompt).strip()
    current_port = int(val) if val else default_port

    cfg["image_url"] = f"http://{current_ip}:{current_port}/hokku/screen/"

    print(f"  Checking server at {current_ip}:{current_port}...", end=" ", flush=True)
    if _check_server_reachable(current_ip, current_port):
        print("OK")
    else:
        print("NOT REACHABLE")
        print(f"  WARNING: Could not connect to {current_ip}:{current_port}")
        print("  Make sure the webserver is running before the frame tries to connect.")
        if input("  Continue anyway? [Y/n]: ").strip().lower() == "n":
            return None

    # --- screen name ---
    current = cfg.get("screen_name", "")
    prompt = f"  Screen Name [{current}]: " if current else "  Screen Name (optional, e.g. Living Room): "
    val = input(prompt).strip()
    if val:
        if len(val.encode("utf-8")) > 64:
            print(f"  ERROR: Screen name is {len(val.encode('utf-8'))} bytes, max 64.")
            return None
        cfg["screen_name"] = val

    return cfg


def _pi_config_mismatch(existing_config, pi_credentials):
    """Return list of field names where existing ESP32 config differs from fresh Pi-install values."""
    if not existing_config or not pi_credentials:
        return []
    diffs = []
    if pi_credentials.get("wifi_ssid") and existing_config.get("wifi_ssid") != pi_credentials["wifi_ssid"]:
        diffs.append("wifi_ssid")
    if pi_credentials.get("wifi_pass") and existing_config.get("wifi_pass") != pi_credentials["wifi_pass"]:
        diffs.append("wifi_pass")
    if pi_credentials.get("server_ip"):
        ip, _ = _parse_server_url(existing_config.get("image_url", ""))
        if ip != pi_credentials["server_ip"]:
            diffs.append("server_ip")
    return diffs


# -------- flashing --------

def write_config(port, config):
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
        sys.stdout = open(os.devnull, "w")
        try:
            esptool.main([
                "--chip", "esp32s3",
                "--port", port,
                "--baud", "921600",
                "write-flash",
                "--flash-mode", "dio",
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
    bootloader = FIRMWARE_DIR / "bootloader.bin"
    partition_table = FIRMWARE_DIR / "partition-table.bin"
    app = FIRMWARE_DIR / "hokku_epaper.bin"

    missing = [name for name, path in [
        ("bootloader.bin", bootloader),
        ("partition-table.bin", partition_table),
        ("hokku_epaper.bin", app),
    ] if not path.exists()]

    if missing:
        print(f"  ERROR: Firmware files not found in {FIRMWARE_DIR}/")
        for m in missing:
            print(f"    Missing: {m}")
        return False

    try:
        import esptool
    except ImportError:
        print("  Error: esptool not installed. Run: pip install esptool")
        return False

    print("  Flashing firmware (this takes about 30 seconds)...")
    try:
        esptool.main([
            "--chip", "esp32s3",
            "--port", port,
            "--baud", "921600",
            "write-flash",
            "--flash-mode", "dio",
            "--flash-freq", "80m",
            "--flash-size", "16MB",
            hex(BOOTLOADER_OFFSET), str(bootloader),
            hex(PARTITION_TABLE_OFFSET), str(partition_table),
            hex(APP_OFFSET), str(app),
        ])
        print("  Firmware flashed successfully.")
        return True
    except Exception as e:
        print(f"  ERROR: Flash failed: {e}")
        return False


# -------- post-flash boot check --------

BOOT_CHECK_SECS = 10
BOOT_OK_MARKERS = [b"hokku_epaper", b"Charger enabled", b"SPI bus init", b"Entering "]
BOOT_FAIL_MARKERS = [b"Guru Meditation", b"abort()", b"rst:0x10", b"assert failed"]


def check_boot(port):
    """Read serial for a few seconds after flash; report obvious success/failure markers.
    Returns 'ok', 'fail', or 'unknown'."""
    print(f"  Reading serial on {port} for {BOOT_CHECK_SECS}s to verify boot...")
    try:
        ser = serial.Serial(port=port, baudrate=115200, timeout=0.2)
    except Exception as e:
        print(f"  Could not open {port}: {e}")
        return "unknown"

    deadline = time.time() + BOOT_CHECK_SECS
    buf = bytearray()
    saw_ok = False
    saw_fail = False
    try:
        while time.time() < deadline:
            chunk = ser.read(512)
            if chunk:
                buf.extend(chunk)
                if any(m in buf for m in BOOT_FAIL_MARKERS):
                    saw_fail = True
                    break
                if any(m in buf for m in BOOT_OK_MARKERS):
                    saw_ok = True
    finally:
        try:
            ser.close()
        except Exception:
            pass

    if saw_fail:
        print("  Boot check: FAILED — crash markers in serial output.")
        tail = buf[-400:].decode("utf-8", errors="replace")
        print("  Last output:")
        for line in tail.splitlines()[-8:]:
            print(f"    {line}")
        return "fail"
    if saw_ok:
        print("  Boot check: OK — firmware started.")
        return "ok"
    print("  Boot check: no recognized output (device may be in deep sleep).")
    return "unknown"


def _refresh_device_state(port):
    nvs_data, app_header = read_device_flash(port)
    state = parse_device_state(nvs_data, app_header)
    return state["config"], state["firmware_current"], state.get("device_version"), state.get("release_version")


# -------- main menu --------

def main_menu(device, pi_credentials=None, pi_install_ran=False):
    """Configure + flash loop. If `pi_install_ran`, prefer reconfigure-by-default on mismatch."""
    port = device["port"]
    config = device.get("config") or {}
    firmware_current = device.get("firmware_current")
    device_version = device.get("device_version")
    release_version = device.get("release_version")

    # If Pi install just ran, compare existing NVS config with Pi values
    if pi_install_ran and config:
        diffs = _pi_config_mismatch(config, pi_credentials)
        if diffs:
            print(f"  NOTE: existing ESP32 config differs from values used in Pi install: {', '.join(diffs)}.")
            print("  Reconfiguring is recommended.")

    while True:
        print()
        show_current_config(config)

        if device_version:
            print(f"  Firmware on device:  {device_version}")
        if release_version:
            print(f"  Firmware available:  {release_version}")
        if firmware_current is True:
            print("  Status: up to date")
        elif firmware_current is False:
            print("  Status: UPDATE AVAILABLE")
        elif device_version:
            print("  Status: installed")
        else:
            print("  Status: not detected")
        print()

        # Default choice picking
        if not config:
            default = "2"
        elif pi_install_ran and _pi_config_mismatch(config, pi_credentials):
            default = "1"  # reconfigure to match Pi install values
        elif not pi_install_ran and config:
            default = "4"  # existing config user didn't set — leave it alone
        elif firmware_current is False:
            default = "3"
        else:
            default = "1"

        print("  What would you like to do?")
        for num, label in [("1", "Update configuration"),
                           ("2", "Configure + flash firmware"),
                           ("3", "Flash firmware only" + (" (keep existing config)" if config else "")),
                           ("4", "Exit")]:
            marker = " <-- default" if num == default else ""
            print(f"    [{num}] {label}{marker}")
        print()

        choice = input(f"  [{default}]> ").strip() or default

        if choice == "1":
            new_config = prompt_config(config, pi_credentials)
            if new_config and write_config(port, new_config):
                config = new_config
                print("  Device will restart with new configuration.")

        elif choice == "2":
            print()
            print("  First, let's configure the device.")
            new_config = prompt_config(config, pi_credentials)
            if new_config:
                write_config(port, new_config)
                config = new_config
                if flash_firmware(port):
                    print("  Setup complete.")
                    check_boot(port)
            print("  Re-reading device state...")
            config, firmware_current, device_version, release_version = _refresh_device_state(port)
            config = config or {}
            if firmware_current is None:
                firmware_current = True
                device_version = release_version

        elif choice == "3":
            if flash_firmware(port):
                check_boot(port)
                print("  Re-reading device state...")
                config, firmware_current, device_version, release_version = _refresh_device_state(port)
                config = config or {}
                if firmware_current is None:
                    firmware_current = True
                    device_version = release_version

        elif choice == "4":
            print("  Bye!")
            break
        else:
            print("  Invalid choice.")


def run(pi_credentials=None, pi_install_ran=False):
    """Entry point called by the top-level installer."""
    try:
        import esptool  # noqa: F401
    except ImportError:
        print("  ERROR: esptool is not installed.")
        print("  Run: pip install esptool pyserial")
        return 1

    print("  Scanning for ESP32-S3 on USB serial ports...")
    devices = scan_devices()
    print()

    device = select_device(devices)
    if device is None:
        return 1

    main_menu(device, pi_credentials=pi_credentials, pi_install_ran=pi_install_ran)
    return 0
