"""Detect connected EPF1301 frames and read their on-flash state.

All esptool access is via ``subprocess`` (never ``esptool.main()`` in-process,
which mutates global ``sys.stdout`` and is unsafe in a threaded web server).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile

import serial.tools.list_ports

from .constants import (
    APP_OFFSET,
    CONFIG_VERSION,
    ESP32S3_PID,
    ESP32S3_VID,
    ESPTOOL_BAUD,
    NVS_OFFSET,
    NVS_SIZE,
)
from .firmware import release_app_header
from .nvs import read_nvs

logger = logging.getLogger(__name__)

# esptool read covering the NVS partition through the app header in one shot.
_READ_SIZE = (APP_OFFSET + 256) - NVS_OFFSET


def list_serial_ports() -> list[dict]:
    """Return ``{port, description, is_esp32}`` for every serial port present."""
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append(
            {
                "port": p.device,
                "description": p.description or p.device,
                "is_esp32": p.vid == ESP32S3_VID and p.pid == ESP32S3_PID,
            }
        )
    return ports


def read_device_flash(port: str, timeout: int = 60) -> tuple[bytes | None, bytes | None]:
    """One esptool read covering the NVS partition + app header.

    Returns ``(nvs_bytes, app_header_bytes)`` or ``(None, None)`` on failure.
    """
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        tmp_path = f.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "esptool",
                "--chip",
                "esp32s3",
                "--port",
                port,
                "--baud",
                ESPTOOL_BAUD,
                "read-flash",
                hex(NVS_OFFSET),
                hex(_READ_SIZE),
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None, None
        with open(tmp_path, "rb") as f:
            data = f.read()
        nvs_data = data[:NVS_SIZE]
        app_header = data[APP_OFFSET - NVS_OFFSET :][:256]
        return nvs_data, app_header
    except (OSError, subprocess.SubprocessError):
        return None, None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _version_from_header(header: bytes | None) -> str | None:
    if not header or len(header) < 80:
        return None
    raw = header[48:80].split(b"\x00")[0].rstrip(b"\xff")
    if not raw:
        return None  # blank (0xFF) flash region — no version present
    ver = raw.decode("ascii", errors="replace")
    if not ver or "�" in ver:
        return None  # non-ASCII garbage — treat as absent
    return ver


def parse_device_state(
    nvs_data: bytes | None,
    app_header: bytes | None,
    release_header: bytes | None = None,
) -> dict:
    """Parse firmware presence, version, and config from raw flash bytes.

    *release_header* is the bundled firmware's app header (256 bytes); if omitted
    it is read from the bundled image. Used to compute ``firmware_current``.
    """
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
    result["device_version"] = _version_from_header(app_header)

    if release_header is None:
        release_header = release_app_header()
    if release_header:
        result["release_version"] = _version_from_header(release_header)
        if app_header and len(app_header) >= 256 and len(release_header) >= 256:
            # Skip first 24 bytes (esp_image_header_t is rewritten by esptool at flash).
            if app_header[24:] == release_header[24:]:
                result["firmware_current"] = True
            else:
                # Version strings are YYYYMMDDHHMMSSZ timestamps — they sort
                # lexicographically, so device > release means device is ahead.
                dv = result.get("device_version") or ""
                rv = result.get("release_version") or ""
                result["firmware_current"] = "newer" if dv > rv else False

    if nvs_data:
        config = read_nvs(nvs_data)
        if config and config.get("cfg_ver") == CONFIG_VERSION:
            result["config"] = config
            result["config_version_ok"] = True
        elif config and "cfg_ver" in config:
            result["config_version_ok"] = False

    return result


def scan_devices() -> list[dict]:
    """Enumerate all serial ports; for ESP32-S3 ports, read on-flash state.

    Each ESP32-S3 device is read (which resets it), so only call this when no
    flash is in progress. Returns a list of dicts (one per serial port).
    """
    all_ports = list_serial_ports()
    logger.info(
        "Scanning for screens: found %d serial port(s): %s",
        len(all_ports),
        [p["port"] for p in all_ports] or "none",
    )

    release_header = release_app_header()
    devices = []
    for dev in all_ports:
        dev.update(
            {
                "config": None,
                "has_hokku_firmware": False,
                "config_version_ok": False,
                "firmware_current": None,
                "device_version": None,
                "release_version": None,
            }
        )
        if dev["is_esp32"]:
            nvs_data, app_header = read_device_flash(dev["port"])
            dev.update(parse_device_state(nvs_data, app_header, release_header))
            state = dev
            logger.info(
                "  %s: hokku_firmware=%s version=%s firmware_current=%s screen_name=%r",
                dev["port"],
                state["has_hokku_firmware"],
                state["device_version"] or "unknown",
                state["firmware_current"],
                (state["config"] or {}).get("screen_name", ""),
            )
        else:
            logger.info("  %s: %s (not an ESP32-S3)", dev["port"], dev["description"])
        devices.append(dev)

    esp32s = [d for d in devices if d["is_esp32"]]
    if not esp32s:
        logger.info("Scan complete: no ESP32-S3 screens found")
    else:
        logger.info("Scan complete: %d ESP32-S3 screen(s) found", len(esp32s))
    return devices
