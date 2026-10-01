"""Tests for the model-aware ESP32 provisioning CLIs (esp32_setup + hokku_setup).

The model is an explicit choice. These cover that the CLI honours it: set_model
switches the delegated screen, the scan only recognises that model's USB id, the
release-asset matcher + tag parser are model-scoped, and --model is parsed.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import esp32_setup
import hokku_setup


@pytest.fixture(autouse=True)
def _reset_model():
    # esp32_setup keeps the active screen as module state; restore the default
    # after each test so a set_model() doesn't leak into the next.
    yield
    esp32_setup.set_model("huessen_epf1301")


def test_set_model_switches_delegated_screen():
    esp32_setup.set_model("seeedstudio_e1004")
    assert esp32_setup.MODEL_ID == "seeedstudio_e1004"
    assert esp32_setup.SCREEN.SPEC.model_id == "seeedstudio_e1004"
    assert esp32_setup.SCREEN.SPEC.flash_size == "32MB"

    esp32_setup.set_model("huessen_epf1301")
    assert esp32_setup.SCREEN.SPEC.model_id == "huessen_epf1301"
    assert esp32_setup.SCREEN.SPEC.flash_size == "16MB"


def test_set_model_rejects_unknown_and_non_esp32():
    for bad in ("bigme_f7", "nonesuch"):
        with pytest.raises(ValueError):
            esp32_setup.set_model(bad)


def test_is_merged_firmware_asset_is_model_scoped():
    esp32_setup.set_model("huessen_epf1301")
    assert esp32_setup._is_merged_firmware_asset("hokku-huessen_epf1301-1.2.9.bin")
    assert not esp32_setup._is_merged_firmware_asset("hokku-seeedstudio_e1004-1.2.0.bin")
    assert not esp32_setup._is_merged_firmware_asset("random.bin")

    esp32_setup.set_model("seeedstudio_e1004")
    assert esp32_setup._is_merged_firmware_asset("hokku-seeedstudio_e1004-1.2.0.bin")
    assert not esp32_setup._is_merged_firmware_asset("hokku-huessen_epf1301-1.2.9.bin")


def _port(device, vid, pid):
    return SimpleNamespace(device=device, description=device, vid=vid, pid=pid)


def test_scan_uses_active_model_vid_pid(monkeypatch):
    # huessen is native USB Serial/JTAG; the E1004 enumerates as its CH340K (#45).
    ports = [_port("COM3", 0x303A, 0x1001), _port("COM12", 0x1A86, 0x7522)]
    monkeypatch.setattr(esp32_setup.serial.tools.list_ports, "comports", lambda: ports)
    monkeypatch.setattr(esp32_setup, "read_device_flash", lambda port: (None, None))

    esp32_setup.set_model("seeedstudio_e1004")
    assert (esp32_setup.SCREEN.SPEC.vid, esp32_setup.SCREEN.SPEC.pid) == (0x1A86, 0x7522)
    found = {d["port"]: d["is_esp32"] for d in esp32_setup.scan_devices()}
    assert found == {"COM3": False, "COM12": True}

    esp32_setup.set_model("huessen_epf1301")
    found = {d["port"]: d["is_esp32"] for d in esp32_setup.scan_devices()}
    assert found == {"COM3": True, "COM12": False}


@pytest.mark.parametrize(
    "log",
    [
        b"I (312) app_init: Project name:     hokku_epaper\r\n",
        b"I (298) app_init: Project name:     hokku_seeedstudio_e1004\r\n",
    ],
)
def test_boot_ok_marker_matches_both_esp32_apps(log):
    assert esp32_setup.BOOT_OK_RE.search(log)


def test_boot_ok_marker_ignores_foreign_app():
    assert not esp32_setup.BOOT_OK_RE.search(b"I (312) app_init: Project name:     E_Frame\r\n")


def test_parse_firmware_tag_is_model_aware():
    esp32_setup.set_model("seeedstudio_e1004")
    assert hokku_setup._parse_firmware_tag("hokku-seeedstudio_e1004-1.2.0.bin") == "1.2.0"
    # A different model's asset does not match the active model -> 'local'.
    assert hokku_setup._parse_firmware_tag("hokku-huessen_epf1301-1.2.9.bin") == "local"


def test_parse_model_arg():
    assert (
        hokku_setup._parse_model_arg(["prog", "--model", "seeedstudio_e1004"])
        == "seeedstudio_e1004"
    )
    assert (
        hokku_setup._parse_model_arg(["prog", "--model=seeedstudio_e1004"]) == "seeedstudio_e1004"
    )
    assert hokku_setup._parse_model_arg(["prog"]) is None


def test_failed_flash_read_is_not_reported_as_no_firmware(capsys):
    # A failed read leaves the state unknown: it must not print "will be
    # overwritten" or default the menu to a full reflash (#45 follow-up).
    dev = {
        "port": "COM12",
        "is_esp32": True,
        "flash_read_ok": False,
        "has_hokku_firmware": False,
        "config_version_ok": False,
        "config": None,
    }
    status = {"device": dev}
    assert hokku_setup._menu_default(status) == "8"
    hokku_setup._print_device_status(status)
    out = capsys.readouterr().out
    assert "could not read its flash" in out
    assert "overwritten" not in out
    assert "could not read its flash" in esp32_setup.format_device_line(1, dev)


def test_readable_blank_device_still_defaults_to_flash():
    dev = {"port": "COM12", "is_esp32": True, "flash_read_ok": True, "has_hokku_firmware": False}
    assert hokku_setup._menu_default({"device": dev}) == "4"
