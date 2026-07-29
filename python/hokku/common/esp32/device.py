"""Detect connected ESP32-S3 hokku screens and read their on-flash state.

All esptool access is via ``subprocess`` (never ``esptool.main()`` in-process,
which mutates global ``sys.stdout`` and is unsafe in a threaded web server).
Parameterised by an :class:`Esp32Spec` (VID/PID, partition offsets, config version,
release header for the "is current" comparison).
"""

from __future__ import annotations

import logging
import os
import struct
import subprocess
import sys
import tempfile
import zlib

import serial.tools.list_ports

from hokku.common.esp32.firmware import release_app_header
from hokku.common.esp32.nvs import read_nvs
from hokku.common.esp32.spec import Esp32Spec

logger = logging.getLogger(__name__)


def list_serial_ports(spec: Esp32Spec) -> list[dict]:
    """Return ``{port, description, vid, pid, is_esp32}`` for every serial port.

    ``vid``/``pid`` are raw USB ids (or None) so callers can classify the port's
    hardware model (e.g. a CH340 bridge → Bigme F7)."""
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append(
            {
                "port": p.device,
                "description": p.description or p.device,
                "vid": p.vid,
                "pid": p.pid,
                "is_esp32": p.vid == spec.vid and p.pid == spec.pid,
            }
        )
    return ports


# otadata holds two ``esp_ota_select_entry_t`` records, each at the start of its
# own 4 KB sector; the app slots they choose between are ota_0 and ota_1.
_OTA_SELECT_SECTOR = 0x1000
_OTA_SELECT_ENTRY_SIZE = 32
_OTA_APP_COUNT = 2


def _ota_select_seq(entry: bytes) -> int | None:
    """``ota_seq`` from one otadata entry, or None if blank or CRC-invalid.

    Layout is ``uint32 ota_seq; uint8 label[20]; uint32 state; uint32 crc``, with
    ``crc = crc32_le(UINT32_MAX, &ota_seq, 4)`` — the bootloader ignores entries
    whose CRC does not match, so this must too.
    """
    if len(entry) < _OTA_SELECT_ENTRY_SIZE:
        return None
    (seq,) = struct.unpack_from("<I", entry, 0)
    (crc,) = struct.unpack_from("<I", entry, 28)
    if seq == 0xFFFFFFFF:
        return None
    if zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF != crc:
        return None
    return seq


def active_ota_slot(otadata: bytes | None) -> int:
    """Which app slot the bootloader will run, from the raw otadata partition.

    Mirrors the IDF bootloader: the higher of the two valid ``ota_seq`` values
    wins and selects slot ``(seq - 1) % 2``. When neither entry is valid — blank
    otadata, which is what a USB flash leaves behind — it falls back to ota_0.
    """
    if not otadata:
        return 0
    seqs = [
        seq
        for seq in (
            _ota_select_seq(otadata[:_OTA_SELECT_ENTRY_SIZE]),
            _ota_select_seq(
                otadata[_OTA_SELECT_SECTOR : _OTA_SELECT_SECTOR + _OTA_SELECT_ENTRY_SIZE]
            ),
        )
        if seq is not None
    ]
    if not seqs:
        return 0
    return (max(seqs) - 1) % _OTA_APP_COUNT


def boot_app(spec: Esp32Spec, port: str, timeout: int = 60) -> bool:
    """Leave the device running its application firmware.

    Every flash/read step runs with ``--after no-reset``, because an esptool
    reset boots the app and a boot immediately downloads and repaints the panel
    (~28 s). Resetting between steps therefore *starts* a paint that the next
    step interrupts mid-refresh, which leaves the panel controller wedged
    holding BUSY — every subsequent BUSY wait then burns its full 60 s timeout.
    So the app is booted exactly once, here, when all flash traffic is done.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port, "run"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _read_region(spec: Esp32Spec, port: str, offset: int, size: int, timeout: int) -> bytes | None:
    """One esptool ``read-flash`` of *size* bytes at *offset*; None on failure.

    Never resets the chip afterwards — see :func:`boot_app`.
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
                spec.baud,
                "--after",
                "no-reset",
                "read-flash",
                hex(offset),
                hex(size),
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None
        with open(tmp_path, "rb") as f:
            return f.read()
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def read_device_flash(
    spec: Esp32Spec, port: str, timeout: int = 60, boot_after: bool = True
) -> tuple[bytes | None, bytes | None]:
    """Read the NVS partition + the app header of the slot the device *boots*.

    otadata is consulted rather than assuming ota_0: a device that has taken an
    OTA boots ota_1, and reading ota_0's header would report a version the
    bootloader is ignoring — which is exactly how a stale device passes for
    up to date. Costs one extra esptool read, and a third only when ota_1 wins.

    None of the reads reset the chip; unless *boot_after* is False the app is
    booted once at the end (see :func:`boot_app`). Callers that will keep
    flashing pass ``boot_after=False`` and boot when they are done.

    Returns ``(nvs_bytes, app_header_bytes)`` or ``(None, None)`` on failure.
    """
    try:
        read_size = (spec.app_offset + 256) - spec.nvs_offset
        data = _read_region(spec, port, spec.nvs_offset, read_size, timeout)
        if data is None:
            return None, None
        nvs_data = data[: spec.nvs_size]
        app_header = data[spec.app_offset - spec.nvs_offset :][:256]

        # A failed otadata read falls back to the ota_0 header read above.
        otadata = _read_region(spec, port, spec.otadata_offset, spec.otadata_size, timeout)
        if active_ota_slot(otadata) == 1:
            ota1_header = _read_region(spec, port, spec.ota1_offset, 256, timeout)
            if ota1_header is not None:
                app_header = ota1_header
        return nvs_data, app_header
    finally:
        if boot_after:
            boot_app(spec, port, timeout)


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
    spec: Esp32Spec,
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
        release_header = release_app_header(spec)
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
        config = read_nvs(spec, nvs_data)
        if config and config.get("cfg_ver") == spec.config_version:
            result["config"] = config
            result["config_version_ok"] = True
        elif config and "cfg_ver" in config:
            result["config_version_ok"] = False

    return result


def scan_devices(spec: Esp32Spec, boot_after: bool = True) -> list[dict]:
    """Enumerate all serial ports; for ESP32-S3 ports, read on-flash state.

    Each ESP32-S3 device is driven over the same serial port a flash uses, so
    only call this when no flash is in progress.

    *boot_after* controls whether a scanned device is left running its firmware.
    A caller that is about to flash passes False: booting starts a full panel
    repaint (~28 s) at exactly the moment the operator is about to flash, and the
    flash then interrupts that paint and wedges the panel controller. The panel
    keeps showing its last image while held in the bootloader (e-paper is
    persistent), but such a caller MUST guarantee an eventual boot.

    Returns a list of dicts (one per serial port).
    """
    all_ports = list_serial_ports(spec)
    logger.info(
        "Scanning for screens: found %d serial port(s): %s",
        len(all_ports),
        [p["port"] for p in all_ports] or "none",
    )

    release_header = release_app_header(spec)
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
            nvs_data, app_header = read_device_flash(spec, dev["port"], boot_after=boot_after)
            dev.update(parse_device_state(spec, nvs_data, app_header, release_header))
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
