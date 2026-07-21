"""Build the Bigme F7's on-flash config blob for BROM-time provisioning.

The F7 firmware persists its app config (server URL, screen name, IP, power mode)
as a single ``hokku_config_t`` struct inside an FDCM area at flash ``0x340000``
(see ``firmware/bigme_f7/hokku_config.{c,h}``). WiFi credentials live separately
in sysinfo and are *not* touched here.

Writing this blob during the mask-BROM flash session — the same session that
writes slot 0 — provisions the unit deterministically, instead of racing the
booted firmware's console (which is only alive for a few seconds between the
device's ~180 s hibernation cycles). A malformed blob simply fails the firmware's
magic/version check and defaults load, so a bad write is non-bricking.

The FDCM on-flash layout (matching the XR872 ROM ``fdcm_rewrite``) is::

    +0  fdcm_header:  id_code u32 (0xA55A5AA5) | bitmap_size u16 | data_size u16
    +8  bitmap:       0xFE then 0xFF-fill (one generation used, slot 0)
    +8+bitmap_size    data:  the packed hokku_config_t

Keep the constants below in sync with ``firmware/bigme_f7/hokku_config.h``.
"""

from __future__ import annotations

import struct

# ── flash area (must match hokku_config.c) ──────────────────────────────────
HOKKU_CFG_ADDR = 0x340000
HOKKU_CFG_SIZE = 0x1000  # 4 KB FDCM area / erase sector

# ── hokku_config_t (must match hokku_config.h) ──────────────────────────────
HOKKU_CFG_MAGIC = 0x484B4347  # 'HKCG'
HOKKU_CFG_VERSION = 2
_URL_MAX = 128
_NAME_MAX = 64
_IP_MAX = 16
CONFIG_BLOB_SIZE = 256  # sizeof(hokku_config_t) with EABI alignment

# power_mode values (HOKKU_PWR_*)
PWR_AUTO = 0  # awake on USB, hibernate on battery (firmware default)
PWR_SLEEP = 1
PWR_AWAKE = 2

# ── FDCM (must match xr872_sdk rom fdcm.c) ──────────────────────────────────
_FDCM_ID_CODE = 0xA55A5AA5
_FDCM_HEADER_SIZE = 8


def _cstr(value: str, size: int) -> bytes:
    """NUL-terminated, NUL-padded fixed-width C string field."""
    return value.encode("utf-8")[: size - 1].ljust(size, b"\x00")


def build_config_blob(
    server_url: str,
    screen_name: str,
    *,
    use_dhcp: int = 0,
    power_mode: int = PWR_AUTO,
    ip: str = "192.168.6.199",
    gw: str = "192.168.6.254",
    nm: str = "255.255.255.0",
    default_sleep_s: int = 300,
) -> bytes:
    """Pack a ``hokku_config_t`` (256 bytes), matching the firmware struct layout."""
    blob = (
        struct.pack("<II", HOKKU_CFG_MAGIC, HOKKU_CFG_VERSION)
        + _cstr(server_url, _URL_MAX)
        + _cstr(screen_name, _NAME_MAX)
        + struct.pack("<BB", use_dhcp & 0xFF, power_mode & 0xFF)
        + _cstr(ip, _IP_MAX)
        + _cstr(gw, _IP_MAX)
        + _cstr(nm, _IP_MAX)
        + b"\x00\x00"  # 2 padding bytes before the aligned u32
        + struct.pack("<I", default_sleep_s & 0xFFFFFFFF)
    )
    assert len(blob) == CONFIG_BLOB_SIZE, len(blob)
    return blob


def build_fdcm_block(data: bytes, area_size: int = HOKKU_CFG_SIZE) -> bytes:
    """Wrap *data* in a fresh single-generation FDCM block (header + bitmap + data).

    Returns a 512-byte, 0xFF-padded block ready to write at the area's start after
    a sector erase — the firmware's ``fdcm_read`` reads generation 0 from it.
    """
    data_size = len(data)
    bitmap_size = (area_size - _FDCM_HEADER_SIZE - 1) // (data_size * 8 + 1) + 1
    block = bytearray(b"\xff" * 512)
    struct.pack_into("<IHH", block, 0, _FDCM_ID_CODE, bitmap_size, data_size)
    block[_FDCM_HEADER_SIZE] = 0xFE  # bitmap[0]: one generation used, rest stays 0xFF
    off = _FDCM_HEADER_SIZE + bitmap_size
    block[off : off + data_size] = data
    return bytes(block)


def read_fdcm_data(block: bytes) -> bytes | None:
    """Extract the current-generation data from an FDCM block (the firmware's read
    path), or None if the block is not a valid FDCM area. For verify + tests."""
    if len(block) < _FDCM_HEADER_SIZE:
        return None
    id_code, bitmap_size, data_size = struct.unpack_from("<IHH", block, 0)
    if id_code != _FDCM_ID_CODE:
        return None
    bit = 0
    for b in block[_FDCM_HEADER_SIZE : _FDCM_HEADER_SIZE + bitmap_size]:
        if b == 0:
            bit += 8
        else:
            v = (~b) & 0xFF
            while v:
                v >>= 1
                bit += 1
            break
    if bit == 0:
        return None
    off = _FDCM_HEADER_SIZE + bitmap_size + data_size * (bit - 1)
    return block[off : off + data_size]


def config_from_blob(blob: bytes) -> dict | None:
    """Parse a packed ``hokku_config_t`` back to a dict (verify + tests), or None
    if the magic/version don't match (mirrors the firmware's validity check)."""
    if len(blob) < CONFIG_BLOB_SIZE:
        return None
    magic, version = struct.unpack_from("<II", blob, 0)
    if magic != HOKKU_CFG_MAGIC or version != HOKKU_CFG_VERSION:
        return None
    use_dhcp, power_mode = struct.unpack_from("<BB", blob, 8 + _URL_MAX + _NAME_MAX)
    (default_sleep_s,) = struct.unpack_from("<I", blob, CONFIG_BLOB_SIZE - 4)

    def field(off: int, size: int) -> str:
        return blob[off : off + size].split(b"\x00")[0].decode("utf-8", "replace")

    ip_base = 8 + _URL_MAX + _NAME_MAX + 2
    return {
        "server_url": field(8, _URL_MAX),
        "screen_name": field(8 + _URL_MAX, _NAME_MAX),
        "use_dhcp": use_dhcp,
        "power_mode": power_mode,
        "ip": field(ip_base, _IP_MAX),
        "gw": field(ip_base + _IP_MAX, _IP_MAX),
        "nm": field(ip_base + 2 * _IP_MAX, _IP_MAX),
        "default_sleep_s": default_sleep_s,
    }


# Compile-time defaults from firmware/bigme_f7/hokku_config.c (hokku_config_defaults).
# Used when a fresh unit has no valid config blob yet.
FIRMWARE_DEFAULTS = {
    "server_url": "http://192.168.6.111:8080/hokku/screen/",
    "screen_name": "bigme-f7",
    "use_dhcp": 0,
    "power_mode": PWR_AUTO,
    "ip": "192.168.6.199",
    "gw": "192.168.6.254",
    "nm": "255.255.255.0",
    "default_sleep_s": 300,
}

# xr872_flasher.ERASE_TYPE_4K (kept local so this module stays tooling-agnostic
# and unit-testable without the dev-tree tools/ on the path).
_ERASE_TYPE_4K = 0x01


def write_config_via_brom(f, on_line, *, server_url=None, screen_name=None) -> tuple[dict, bool]:
    """Provision the F7's app config over an OPEN mask-BROM handle *f* (an
    ``XR872Flasher``): read the existing blob, override the provisioned fields, and
    write it back to the ``0x340000`` config sector, then read-back verify.

    Deterministic — no booted console needed, so it can't race the device's
    hibernation cycle. Read-modify-write preserves the unit's existing IP / power
    settings; only ``server_url`` / ``screen_name`` change when provided. A fresh
    unit (no valid blob) starts from :data:`FIRMWARE_DEFAULTS`. Only the dedicated
    ``0x340000`` sector is touched — bootloader, both app slots, and the OEM config
    partition are untouched.

    Returns ``(written_config, had_existing_config)``. ``had_existing_config`` is
    True when the unit already had a valid config blob (i.e. it was provisioned
    before, so its Wi-Fi in sysinfo is intact and no console Wi-Fi step is needed).
    """
    base = dict(FIRMWARE_DEFAULTS)
    had_existing = False
    try:
        cur = f.read_sector(HOKKU_CFG_ADDR, 512)
        if cur:
            existing = config_from_blob(read_fdcm_data(cur) or b"")
            if existing:
                base.update(existing)
                had_existing = True
                on_line("  found existing config — preserving IP/power settings.")
    except Exception:  # noqa: S110 — a bad/absent blob just means "use defaults"
        pass

    if server_url:
        base["server_url"] = server_url
    if screen_name:
        base["screen_name"] = screen_name

    blob = build_config_blob(
        base["server_url"],
        base["screen_name"],
        use_dhcp=base["use_dhcp"],
        power_mode=base["power_mode"],
        ip=base["ip"],
        gw=base["gw"],
        nm=base["nm"],
        default_sleep_s=base["default_sleep_s"],
    )
    block = build_fdcm_block(blob)

    on_line(f"  writing config @0x{HOKKU_CFG_ADDR:x}: name={base['screen_name']!r}")
    if not f.erase_flash(HOKKU_CFG_ADDR, _ERASE_TYPE_4K):
        raise RuntimeError(f"config 4K-erase failed @0x{HOKKU_CFG_ADDR:x}")
    if not f.write_sector(HOKKU_CFG_ADDR, block):
        raise RuntimeError(f"config write failed @0x{HOKKU_CFG_ADDR:x}")
    back = f.read_sector(HOKKU_CFG_ADDR, len(block))
    if back is None or read_fdcm_data(back) != blob:
        raise RuntimeError("config readback verify failed")
    on_line("  config written + verified (name/URL persisted to flash).")
    return base, had_existing
