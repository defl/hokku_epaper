"""Tests for the model-aware ESP32 provisioning CLIs (esp32_setup + hokku_setup).

The two ESP32-S3 boards share a USB VID:PID, so the model is an explicit choice.
These cover that the CLI honours it: set_model switches the delegated screen, the
release-asset matcher + tag parser are model-scoped, and --model is parsed.
"""

import os
import sys

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


def test_scan_uses_active_model_vid_pid():
    esp32_setup.set_model("seeedstudio_e1004")
    assert esp32_setup.SCREEN.SPEC.vid == 0x303A
    assert esp32_setup.SCREEN.SPEC.pid == 0x1001


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
