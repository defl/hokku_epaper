"""Flash firmware + NVS config to an ESP32-S3 hokku screen over USB.

The flash order is mandatory: the merged firmware image is written first (it
fills the NVS partition range with 0xFF), then the NVS config is written on top.
esptool runs as a subprocess so its progress output can be streamed line by line
without touching the host process's stdout. Parameterised by an
:class:`Esp32Spec` (flash size, offsets, baud).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from hokku.common.esp32.device import parse_device_state, read_device_flash
from hokku.common.esp32.nvs import build_nvs_binary
from hokku.common.esp32.spec import Esp32Spec

OnLine = Callable[[str], None]


class EsptoolError(RuntimeError):
    """Raised when an esptool subprocess exits non-zero."""


def _noop(_line: str) -> None:
    pass


def _run_esptool(args: list[str], on_line: OnLine) -> None:
    """Run ``python -m esptool <args>``, streaming stdout/stderr line by line.

    Splits on both ``\\n`` and ``\\r`` so esptool's ``\\r``-based progress bar is
    surfaced as it updates. Raises :class:`EsptoolError` on non-zero exit.
    """
    cmd = [sys.executable, "-m", "esptool", *args]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    stdout = proc.stdout
    buf = ""
    for ch in iter(lambda: stdout.read(1), ""):
        if ch in "\r\n":
            line = buf.strip()
            if line:
                on_line(line)
            buf = ""
        else:
            buf += ch
    tail = buf.strip()
    if tail:
        on_line(tail)
    proc.wait()
    if proc.returncode != 0:
        raise EsptoolError(f"esptool exited {proc.returncode} (command: esptool {' '.join(args)})")


def flash_firmware(
    spec: Esp32Spec, port: str, firmware_path: Path, on_line: OnLine = _noop
) -> None:
    """Write the merged firmware image at the bootloader offset (0x0)."""
    on_line(f"Flashing firmware {Path(firmware_path).name} (~30s)...")
    _run_esptool(
        [
            "--chip",
            "esp32s3",
            "--port",
            port,
            "--baud",
            spec.baud,
            "write-flash",
            "--flash-mode",
            "dio",
            "--flash-freq",
            "80m",
            "--flash-size",
            spec.flash_size,
            hex(spec.bootloader_offset),
            str(firmware_path),
        ],
        on_line,
    )


def write_config(spec: Esp32Spec, port: str, config: dict, on_line: OnLine = _noop) -> None:
    """Build and write the NVS config partition at ``spec.nvs_offset``."""
    on_line("Writing configuration...")
    nvs_binary = build_nvs_binary(spec, config)
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(nvs_binary)
        tmp_path = f.name
    try:
        _run_esptool(
            [
                "--chip",
                "esp32s3",
                "--port",
                port,
                "--baud",
                spec.baud,
                "write-flash",
                "--flash-mode",
                "dio",
                hex(spec.nvs_offset),
                tmp_path,
            ],
            on_line,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def flash_device(
    spec: Esp32Spec,
    port: str,
    config: dict,
    firmware_path: Path,
    on_line: OnLine = _noop,
) -> dict | None:
    """Full provisioning: flash firmware, then write NVS config, then verify.

    Returns the verified device state (see :func:`device.parse_device_state`),
    or ``None`` if the post-flash read-back failed.
    """
    flash_firmware(spec, port, firmware_path, on_line)
    write_config(spec, port, config, on_line)

    on_line("Verifying...")
    nvs_data, app_header = read_device_flash(spec, port)
    state = parse_device_state(spec, nvs_data, app_header) if app_header is not None else None
    on_line("Done.")
    return state
