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

import release_cache
from hokku_config import (
    ESP32S3_VID, ESP32S3_PID,
    NVS_OFFSET, NVS_SIZE, CONFIG_VERSION,
    _build_nvs_binary, _read_nvs,
)

SCRIPT_DIR = Path(__file__).parent
LOCAL_FIRMWARE_DIR = SCRIPT_DIR.parent / "firmware" / "release"
FIRMWARE_CACHE_DIR = release_cache.CACHE_DIR / "firmware"

# Mutable — resolved at run() time via resolve_firmware_dir().
FIRMWARE_DIR = LOCAL_FIRMWARE_DIR

# Flash offsets inside the merged image. Only BOOTLOADER_OFFSET is used for
# flashing (write the whole merged image at 0x0); APP_OFFSET is where we seek
# inside the merged image to read the app descriptor / version string.
BOOTLOADER_OFFSET = 0x0
APP_OFFSET = 0x10000


# -------- firmware location resolver --------

def _merged_firmware_file(directory):
    """Return the merged hokku-firmware_<version>.bin in `directory`, or None."""
    if directory is None or not directory.exists():
        return None
    matches = sorted(directory.glob("hokku-firmware_*.bin"))
    return matches[-1] if matches else None


def _is_merged_firmware_asset(name):
    return name.startswith("hokku-firmware_") and name.endswith(".bin")


def resolve_firmware_dir():
    """Return a directory containing a merged hokku-firmware_<version>.bin.
    Prefers the local firmware/release/ dir; falls back to downloading the
    merged release asset from GitHub into .cache/firmware/<tag>/. Returns None
    if nothing is available (no local file and no network)."""
    global FIRMWARE_DIR

    if _merged_firmware_file(LOCAL_FIRMWARE_DIR):
        FIRMWARE_DIR = LOCAL_FIRMWARE_DIR
        return FIRMWARE_DIR

    print(f"  No hokku-firmware_*.bin in {LOCAL_FIRMWARE_DIR}. Fetching latest GitHub release...")
    try:
        release = release_cache.get_latest_release()
    except Exception as e:
        print(f"  ERROR: could not reach GitHub: {e}")
        return None

    tag = release.get("tag_name", "latest")
    asset = release_cache.find_asset(release, _is_merged_firmware_asset)
    if asset is None:
        print(f"  ERROR: release {tag} has no hokku-firmware_*.bin asset.")
        print("  (See 'Releasing firmware' in CLAUDE.md — the release must ship")
        print("   a single merged hokku-firmware_<version>.bin file.)")
        return None

    target_dir = FIRMWARE_CACHE_DIR / tag
    if release_cache.ensure_cached_asset(asset, target_dir, label=f"(release {tag})") is None:
        return None

    FIRMWARE_DIR = target_dir
    print(f"  Firmware ready: {FIRMWARE_DIR}")
    return FIRMWARE_DIR


def _release_app_header(directory=None):
    """Return the first 256 bytes of the app section inside the merged firmware
    image (located at offset 0x10000). Used to read the release version string
    and compare against what's on the device."""
    directory = directory or FIRMWARE_DIR
    merged = _merged_firmware_file(directory)
    if not merged:
        return None
    with open(merged, "rb") as f:
        f.seek(APP_OFFSET)
        return f.read(256)


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

    release_header = _release_app_header()
    if release_header:
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
    host, port = _parse_server_url(config.get("image_url", ""))
    print("  Current configuration:")
    print(f"    WiFi SSID:     {config.get('wifi_ssid', '(not set)')}")
    print(f"    WiFi Password: {'****' if config.get('wifi_pass') else '(not set)'}")
    print(f"    Server:        {host or '(not set)'}:{port or 8080}")
    print(f"    Screen Name:   {config.get('screen_name', '(not set)')}")
    print()


def _mdns_resolve(hostname, timeout=3.0):
    """Resolve a .local hostname via mDNS multicast. Returns IP string or None.
    Uses only the standard library — no zeroconf dependency needed."""
    import socket, struct, threading

    def _encode_name(name):
        out = b''
        for label in name.rstrip('.').split('.'):
            b = label.encode('ascii')
            out += bytes([len(b)]) + b
        return out + b'\x00'

    # mDNS query packet: QU bit set so the responder sends a unicast reply
    packet = struct.pack('!HHHHHH', 0, 0, 1, 0, 0, 0)
    packet += _encode_name(hostname) + struct.pack('!HH', 1, 0x8001)  # A, QU+IN

    result = [None]

    def _run():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(timeout)
            sock.bind(('', 0))
            sock.sendto(packet, ('224.0.0.251', 5353))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    break
                # Walk the answer section looking for an A record
                try:
                    ancount = struct.unpack('!H', data[6:8])[0]
                    qdcount = struct.unpack('!H', data[4:6])[0]
                    pos = 12
                    # Skip questions
                    for _ in range(qdcount):
                        while pos < len(data):
                            if data[pos] & 0xc0 == 0xc0:
                                pos += 2; break
                            if data[pos] == 0:
                                pos += 1; break
                            pos += data[pos] + 1
                        pos += 4
                    # Parse answers
                    for _ in range(ancount):
                        while pos < len(data):
                            if data[pos] & 0xc0 == 0xc0:
                                pos += 2; break
                            if data[pos] == 0:
                                pos += 1; break
                            pos += data[pos] + 1
                        if pos + 10 > len(data):
                            break
                        rtype, _, _, rdlen = struct.unpack('!HHIH', data[pos:pos + 10])
                        pos += 10
                        if rtype == 1 and rdlen == 4:  # A record
                            result[0] = socket.inet_ntoa(data[pos:pos + 4])
                            return
                        pos += rdlen
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            try:
                sock.close()
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout + 0.5)
    return result[0]


def _resolve_host(hostname):
    """Return an IP for hostname. Tries getaddrinfo first (works on modern
    Windows/macOS/Linux for .local via the OS mDNS stack), then falls back
    to a manual mDNS query for .local names."""
    import socket
    try:
        return socket.getaddrinfo(hostname, None, socket.AF_INET)[0][4][0]
    except socket.gaierror:
        pass
    if hostname.endswith('.local'):
        return _mdns_resolve(hostname)
    return None


def _check_server_reachable(host, port):
    import urllib.request, urllib.error
    ip = _resolve_host(host)
    if ip is None:
        return False, None
    try:
        urllib.request.urlopen(f"http://{ip}:{port}/hokku/api/time", timeout=5)
        return True, ip
    except urllib.error.HTTPError:
        return True, ip   # got an HTTP response — server is up, endpoint may differ
    except Exception:
        return False, ip


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
    current_host, current_port = _parse_server_url(cfg.get("image_url", ""))
    default_host = pi.get("server_ip") or current_host or "hokku.local"
    default_port = current_port or 8080

    prompt = f"  Server hostname or IP [{default_host}]: "
    val = input(prompt).strip()
    if val:
        current_host = val
    elif default_host:
        current_host = default_host
    else:
        print("  Server hostname or IP is required.")
        return None

    prompt = f"  Server Port [{default_port}]: "
    val = input(prompt).strip()
    current_port = int(val) if val else default_port

    cfg["image_url"] = f"http://{current_host}:{current_port}/hokku/screen/"

    print(f"  Checking server at {current_host}:{current_port}...", end=" ", flush=True)
    reachable, resolved_ip = _check_server_reachable(current_host, current_port)
    if reachable:
        if resolved_ip and resolved_ip != current_host:
            print(f"OK  ({resolved_ip})")
        else:
            print("OK")
    else:
        if resolved_ip is None and current_host.endswith('.local'):
            print("NOT FOUND")
            print(f"  WARNING: Could not resolve {current_host} via mDNS.")
            print("  Make sure the server is running and on the same network.")
        else:
            print("NOT REACHABLE")
            print(f"  WARNING: Resolved {current_host} to {resolved_ip} but could not connect.")
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
        host, _ = _parse_server_url(existing_config.get("image_url", ""))
        if host != pi_credentials["server_ip"]:
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
    """Flash the merged hokku-firmware_<version>.bin image at offset 0x0."""
    try:
        import esptool
    except ImportError:
        print("  Error: esptool not installed. Run: pip install esptool")
        return False

    merged = _merged_firmware_file(FIRMWARE_DIR)
    if not merged:
        print(f"  ERROR: No hokku-firmware_*.bin in {FIRMWARE_DIR}.")
        print("  (See 'Releasing firmware' in CLAUDE.md — builds must produce a")
        print("   single merged firmware file.)")
        return False

    print(f"  Flashing {merged.name} (~30s)...")
    try:
        esptool.main([
            "--chip", "esp32s3",
            "--port", port,
            "--baud", "921600",
            "write-flash",
            "--flash-mode", "dio",
            "--flash-freq", "80m",
            "--flash-size", "16MB",
            hex(BOOTLOADER_OFFSET), str(merged),
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
                # Flash before writing NVS: the merged binary covers the NVS
                # partition range and would erase it if written first.
                if flash_firmware(port):
                    write_config(port, new_config)
                    config = new_config
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


def _prepare(require_firmware):
    """Shared prelude for the run_* helpers: check esptool, resolve firmware
    (if needed), scan, and let the user pick a device. Returns the selected
    device dict, or None on failure."""
    try:
        import esptool  # noqa: F401
    except ImportError:
        print("  ERROR: esptool is not installed.")
        print("  Run: pip install esptool pyserial")
        return None

    if require_firmware and resolve_firmware_dir() is None:
        print("  ERROR: no firmware available locally or from GitHub. Aborting.")
        return None
    if not require_firmware:
        # Still try to resolve so the menu can show version info; tolerate failure.
        resolve_firmware_dir()

    print("  Scanning for ESP32-S3 on USB serial ports...")
    devices = scan_devices()
    print()
    return select_device(devices)


def run(pi_credentials=None, pi_install_ran=False):
    """Full interactive menu (configure / flash / both)."""
    device = _prepare(require_firmware=False)
    if device is None:
        return 1
    main_menu(device, pi_credentials=pi_credentials, pi_install_ran=pi_install_ran)
    return 0


def run_configure_and_flash(pi_credentials=None):
    """Direct: prompt for config, flash firmware, then write NVS config,
    then post-flash boot check. No inner menu.

    Flash must happen before the NVS write: the merged firmware binary
    spans 0x0–0xFCxxx and fills the NVS gap (0x9000–0xEFFF) with 0xFF,
    so flashing after writing config would erase the NVS partition."""
    device = _prepare(require_firmware=True)
    if device is None:
        return 1
    port = device["port"]
    existing = device.get("config") or {}

    print("  Configure + flash firmware")
    print("  --------------------------")
    new_config = prompt_config(existing, pi_credentials)
    if new_config is None:
        print("  Aborted — no changes written.")
        return 1
    if not flash_firmware(port):
        print("  ERROR: firmware flash failed.")
        return 1
    if not write_config(port, new_config):
        print("  ERROR: failed to write configuration.")
        return 1
    check_boot(port)
    print()
    print("  Done — firmware flashed and configuration written.")
    return 0


def run_configure_only(pi_credentials=None):
    """Direct: prompt for config, write NVS, done. Does not flash."""
    device = _prepare(require_firmware=False)
    if device is None:
        return 1
    port = device["port"]
    existing = device.get("config") or {}

    print("  Configure only (keep existing firmware)")
    print("  ---------------------------------------")
    new_config = prompt_config(existing, pi_credentials)
    if new_config is None:
        print("  Aborted — no changes written.")
        return 1
    if not write_config(port, new_config):
        print("  ERROR: failed to write configuration.")
        return 1
    print()
    print("  Configuration written successfully.")
    host, port_num = _parse_server_url(new_config.get("image_url", ""))
    if host:
        print(f"  Server:      {host}:{port_num or 8080}")
    if new_config.get("wifi_ssid"):
        print(f"  WiFi SSID:   {new_config['wifi_ssid']}")
    if new_config.get("screen_name"):
        print(f"  Screen name: {new_config['screen_name']}")
    print()
    print("  The frame will restart and connect on the next scheduled refresh.")
    return 0


def run_flash_only():
    """Direct: flash firmware keeping existing NVS config. Post-flash boot check."""
    device = _prepare(require_firmware=True)
    if device is None:
        return 1
    port = device["port"]

    print("  Flash firmware only (keep existing config)")
    print("  ------------------------------------------")
    if not flash_firmware(port):
        print("  ERROR: firmware flash failed.")
        return 1
    check_boot(port)
    print()
    print("  Done — firmware flashed successfully.")
    return 0
