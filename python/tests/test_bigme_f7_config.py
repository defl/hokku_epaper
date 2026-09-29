"""Tests for the Bigme F7 BROM-time config provisioner (hokku.screens.bigme_f7.config).

Validates that the packed hokku_config_t + FDCM block match the firmware's on-flash
format (round-trip through the same read path the firmware uses), and that the
read-modify-write BROM provisioner preserves existing settings while overriding the
provisioned fields. No hardware — the flasher is a small in-memory mock.
"""

from __future__ import annotations

import struct

from hokku.screens.bigme_f7 import config as c


class MockFlasher:
    """In-memory stand-in for XR872Flasher over the 4 KB config sector."""

    def __init__(self, initial: bytes = b"") -> None:
        self.mem = bytearray(b"\xff" * 0x1000)
        self.mem[: len(initial)] = initial

    def read_sector(self, addr: int, length: int) -> bytes:
        off = addr - c.HOKKU_CFG_ADDR
        return bytes(self.mem[off : off + length])

    def erase_flash(self, addr: int, etype: int) -> bool:
        off = addr - c.HOKKU_CFG_ADDR
        self.mem[off : off + 0x1000] = b"\xff" * 0x1000
        return True

    def write_sector(self, addr: int, data: bytes) -> bool:
        off = addr - c.HOKKU_CFG_ADDR
        self.mem[off : off + len(data)] = data
        return True


def test_config_blob_size_and_roundtrip():
    blob = c.build_config_blob("http://x:8080/hokku/screen/", "Den")
    assert len(blob) == c.CONFIG_BLOB_SIZE == 256
    d = c.config_from_blob(blob)
    assert d is not None
    assert d["server_url"] == "http://x:8080/hokku/screen/"
    assert d["screen_name"] == "Den"


def test_fdcm_block_matches_rom_format():
    blob = c.build_config_blob("http://host:8080/hokku/screen/", "SmallOne-reflash")
    block = c.build_fdcm_block(blob)
    id_code, bitmap_size, data_size = struct.unpack_from("<IHH", block, 0)
    assert id_code == 0xA55A5AA5
    assert data_size == 256
    assert bitmap_size == 2
    assert block[8] == 0xFE  # one generation used
    # The firmware's fdcm_read path recovers exactly what we packed.
    assert c.read_fdcm_data(block) == blob
    d = c.config_from_blob(c.read_fdcm_data(block) or b"")
    assert d is not None and d["screen_name"] == "SmallOne-reflash"


def test_config_from_blob_rejects_bad_magic_or_version():
    bad_magic = bytearray(c.build_config_blob("u", "n"))
    bad_magic[0] ^= 0xFF
    assert c.config_from_blob(bytes(bad_magic)) is None
    bad_ver = bytearray(c.build_config_blob("u", "n"))
    bad_ver[4] = 0x09  # version byte
    assert c.config_from_blob(bytes(bad_ver)) is None


def test_write_via_brom_fresh_unit_uses_defaults():
    f = MockFlasher()  # all 0xFF — a fresh unit
    logs: list[str] = []
    out, had_existing = c.write_config_via_brom(
        f, logs.append, server_url="http://h:8080/hokku/screen/", screen_name="NewName"
    )
    assert had_existing is False  # fresh unit -> would still need console Wi-Fi
    assert out["screen_name"] == "NewName"
    # Round-trips off the mock flash exactly as the firmware would read it.
    on_flash = c.config_from_blob(c.read_fdcm_data(f.read_sector(c.HOKKU_CFG_ADDR, 512)) or b"")
    assert on_flash is not None
    assert on_flash["screen_name"] == "NewName"
    assert on_flash["server_url"] == "http://h:8080/hokku/screen/"
    # Issue #44: a fresh unit gets DHCP, no hard-coded address.
    assert on_flash["use_dhcp"] == 1
    assert on_flash["ip"] == "" and on_flash["gw"] == ""


def test_defaults_have_no_hardcoded_ip():
    assert c.FIRMWARE_DEFAULTS["use_dhcp"] == 1
    assert c.FIRMWARE_DEFAULTS["ip"] == c.FIRMWARE_DEFAULTS["gw"] == ""
    assert "hokku.local" in c.FIRMWARE_DEFAULTS["server_url"]


def test_write_via_brom_moves_legacy_default_static_to_dhcp():
    # Issue #44: a unit provisioned with the old compiled-in 192.168.6.199/.254
    # (never a user choice) must come out on DHCP, even though settings are
    # otherwise preserved on re-flash.
    existing = c.build_fdcm_block(
        c.build_config_blob(
            "http://old:8080/hokku/screen/",
            "ari-frame",
            use_dhcp=0,
            ip="192.168.6.199",
            gw="192.168.6.254",
        )
    )
    f = MockFlasher(existing)
    logs: list[str] = []
    c.write_config_via_brom(f, logs.append, screen_name="ari-frame")
    on_flash = c.config_from_blob(c.read_fdcm_data(f.read_sector(c.HOKKU_CFG_ADDR, 512)) or b"")
    assert on_flash is not None
    assert on_flash["use_dhcp"] == 1
    assert on_flash["ip"] == "" and on_flash["gw"] == ""
    assert on_flash["server_url"] == "http://old:8080/hokku/screen/"  # rest preserved
    assert any("dhcp" in ln.lower() for ln in logs)


def test_write_via_brom_keeps_user_static_ip():
    existing = c.build_fdcm_block(
        c.build_config_blob("http://h/", "n", use_dhcp=0, ip="10.0.0.50", gw="10.0.0.1")
    )
    f = MockFlasher(existing)
    c.write_config_via_brom(f, lambda _l: None, screen_name="n")
    on_flash = c.config_from_blob(c.read_fdcm_data(f.read_sector(c.HOKKU_CFG_ADDR, 512)) or b"")
    assert on_flash is not None
    assert on_flash["use_dhcp"] == 0 and on_flash["ip"] == "10.0.0.50"


def test_write_via_brom_preserves_existing_on_rename():
    existing = c.build_fdcm_block(
        c.build_config_blob("http://old:8080/hokku/screen/", "OldName", ip="10.0.0.5")
    )
    f = MockFlasher(existing)
    _cfg, had_existing = c.write_config_via_brom(f, (lambda _l: None), screen_name="Renamed")
    assert had_existing is True  # already provisioned -> skip console Wi-Fi
    on_flash = c.config_from_blob(c.read_fdcm_data(f.read_sector(c.HOKKU_CFG_ADDR, 512)) or b"")
    assert on_flash is not None
    assert on_flash["screen_name"] == "Renamed"
    assert on_flash["server_url"] == "http://old:8080/hokku/screen/"  # preserved
    assert on_flash["ip"] == "10.0.0.5"  # preserved
