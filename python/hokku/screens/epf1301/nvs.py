"""NVS partition binary generation and parsing for the EPF1301 frame.

``build_nvs_binary`` produces an ESP-IDF NVS partition image from a config dict
using the standalone ``esp-idf-nvs-partition-gen`` PyPI package (invoked as a
subprocess) — no full ESP-IDF install required. ``read_nvs`` parses an NVS
partition image back into a dict (pure stdlib).
"""

from __future__ import annotations

import importlib.util
import os
import struct
import subprocess
import sys
import tempfile

from .constants import (
    CONFIG_VERSION,
    NVS_ENTRY_SIZE,
    NVS_NAMESPACE,
    NVS_PAGE_SIZE,
    NVS_SIZE,
    PAGE_ACTIVE,
    STR_TYPE,
    U8_TYPE,
)

# Module name of the standalone generator (`pip install esp-idf-nvs-partition-gen`).
_NVS_GEN_MODULE = "esp_idf_nvs_partition_gen"


class NvsToolUnavailable(RuntimeError):
    """Raised when the NVS partition generator package is not installed."""


def nvs_tool_available() -> bool:
    """True if the standalone NVS generator package is importable."""
    return importlib.util.find_spec(_NVS_GEN_MODULE) is not None


def build_nvs_binary(config_dict: dict) -> bytes:
    """Build an NVS partition binary from a config dict.

    String values become NVS strings; ``cfg_ver`` and ``wifi_order`` are written
    as uint8. The CSV layout mirrors the firmware's expected ``hokku`` namespace.
    """
    if not nvs_tool_available():
        raise NvsToolUnavailable(
            f"Cannot find the NVS partition generator ({_NVS_GEN_MODULE}). "
            "Install it with: pip install esp-idf-nvs-partition-gen"
        )

    # Build CSV: key,type,encoding,value
    csv_lines = ["key,type,encoding,value"]
    csv_lines.append(f"{NVS_NAMESPACE},namespace,,")
    csv_lines.append(f"cfg_ver,data,u8,{CONFIG_VERSION}")
    wifi_order = int(config_dict.get("wifi_order", 0))
    csv_lines.append(f"wifi_order,data,u8,{wifi_order}")
    for key, value in config_dict.items():
        if not isinstance(value, str):
            continue  # u8 fields (cfg_ver, wifi_order) handled above
        escaped = value.replace('"', '""')
        csv_lines.append(f'{key},data,string,"{escaped}"')
    csv_content = "\n".join(csv_lines) + "\n"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write(csv_content)
        csv_path = f.name
    bin_path = csv_path.replace(".csv", ".bin")

    try:
        result = subprocess.run(
            [sys.executable, "-m", _NVS_GEN_MODULE, "generate", csv_path, bin_path, hex(NVS_SIZE)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{_NVS_GEN_MODULE} failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        with open(bin_path, "rb") as f:
            return f.read()
    finally:
        for p in (csv_path, bin_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def read_nvs(partition_data: bytes) -> dict:
    """Read entries from an NVS partition binary (ESP-IDF format).

    Returns a dict of key-value pairs from the ``hokku`` namespace. String values
    are returned as ``str``, uint8 values as ``int``.
    """
    result: dict = {}
    if len(partition_data) < NVS_PAGE_SIZE:
        return result

    page = partition_data[:NVS_PAGE_SIZE]

    # Check page state
    state = struct.unpack_from("<I", page, 0)[0]
    if state not in (PAGE_ACTIVE, 0xFFFFFFFC):  # ACTIVE or FULL
        return result

    # Read entries starting at offset 64 (after 32-byte header + 32-byte bitmap)
    offset = 64
    ns_map: dict = {}  # ns_index -> ns_name

    while offset + NVS_ENTRY_SIZE <= NVS_PAGE_SIZE:
        entry = page[offset : offset + NVS_ENTRY_SIZE]
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
            data_bytes = page[data_offset : data_offset + str_len - 1]  # exclude null
            result[key] = data_bytes.decode("utf-8", errors="replace")

        offset += span * NVS_ENTRY_SIZE

    return result
