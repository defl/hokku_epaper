"""ESP32-S3 flash-layer tests, run against BOTH ESP32 screens (huessen + seeed).

Every test here takes the ``esp32_mod`` fixture (defined in conftest) and runs
once per board. The two boards bind the same shared code in
:mod:`hokku.screens.esp32` to their own :class:`Esp32Spec`, so a single suite
proves the pure flash ops (NVS build/parse, device-state parsing, firmware-file
discovery, app-image slicing, config migration) and the model-aware
``/hokku/firmware-config`` endpoint for both. This mirrors the firmware's
``common/esp32`` shared test suite. The Bigme F7 is tested in ``test_flash_f7.py``.

Hardware-free: esptool/serial are never invoked. The NVS round-trip uses the real
esp-idf-nvs-partition-gen subprocess and is skipped if it is not installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hokku.screens import huessen_epf1301, seeedstudio_e1004
from hokku.screens.esp32 import nvs as esp32_nvs
from hokku.webserver.app_config import AppConfig
from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.flask_app import create_app
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.serve_scheduler import ServeScheduler


def _make_app_header(version: bytes = b"20260101000000Z") -> bytes:
    """A 256-byte app header with the version string at bytes 48:80."""
    header = bytearray(256)
    header[0:12] = b"hokku_epaper"
    header[48 : 48 + len(version)] = version
    return bytes(header)


def _bare_state(app_config: AppConfig) -> AppState:
    clf = ImageClassifier(app_config)
    mgr = build_manager(app_config, clf)
    return AppState(app_config, clf, mgr, ServeScheduler(mgr))


def _client(state: AppState, tmp_path: Path):
    app = create_app(state, config_path=tmp_path / "cfg.json", template_folder=None)
    app.config["TESTING"] = True
    return app.test_client()


# ── esp32.nvs: build / read round-trip ────────────────────────────────────────


@pytest.mark.skipif(
    not esp32_nvs.nvs_tool_available(), reason="esp-idf-nvs-partition-gen not installed"
)
def test_nvs_build_read_round_trip(esp32_mod):
    cfg = {
        "wifi_ssid1": "MyNet",
        "wifi_pass1": 'has"quote',
        "wifi_ssid2": "Backup",
        "wifi_pass2": "second",
        "image_url": "http://192.168.1.5:8080/hokku/screen/",
        "screen_name": "Living Room",
        "wifi_order": 1,
    }
    blob = esp32_mod.build_nvs_binary(cfg)
    assert len(blob) == esp32_mod.NVS_SIZE
    back = esp32_mod.read_nvs(blob)
    assert back["cfg_ver"] == esp32_mod.CONFIG_VERSION
    assert back["wifi_order"] == 1
    for key in ("wifi_ssid1", "wifi_pass1", "wifi_ssid2", "wifi_pass2", "image_url", "screen_name"):
        assert back[key] == cfg[key]


def test_nvs_unavailable_raises(esp32_mod, monkeypatch):
    # build_nvs_binary checks the shared module-level nvs_tool_available().
    monkeypatch.setattr(esp32_nvs, "nvs_tool_available", lambda: False)
    with pytest.raises(esp32_mod.NvsToolUnavailable):
        esp32_mod.build_nvs_binary({"wifi_ssid1": "x", "image_url": "y"})


# ── esp32.device.parse_device_state ───────────────────────────────────────────


def test_parse_device_state_detects_firmware_and_version(esp32_mod):
    header = _make_app_header()
    state = esp32_mod.parse_device_state(b"", header, release_header=header)
    assert state["has_hokku_firmware"] is True
    assert state["device_version"] == "20260101000000Z"
    assert state["firmware_current"] is True


def test_parse_device_state_blank_device(esp32_mod):
    state = esp32_mod.parse_device_state(None, b"\xff" * 256, release_header=_make_app_header())
    assert state["has_hokku_firmware"] is False
    assert state["device_version"] is None
    # device header all-0xFF differs from release -> not current
    assert state["firmware_current"] is False


def test_parse_device_state_reads_config(esp32_mod):
    pytest.importorskip("esp_idf_nvs_partition_gen", reason="NVS generator not installed")
    nvs = esp32_mod.build_nvs_binary({"wifi_ssid1": "Net", "image_url": "u", "screen_name": "Den"})
    state = esp32_mod.parse_device_state(nvs, _make_app_header())
    assert state["config_version_ok"] is True
    assert state["config"]["screen_name"] == "Den"


# ── esp32.firmware: merged-file discovery + app-image slice ────────────────────


def test_merged_firmware_file_picks_highest(esp32_mod, tmp_path):
    model = esp32_mod.SPEC.model_id
    for name in (
        f"hokku-{model}-1.2.4.bin",
        f"hokku-{model}-1.2.9.bin",
        f"hokku-{model}-1.2.10.bin",
        "ignore.txt",
    ):
        (tmp_path / name).write_bytes(b"\x00")
    picked = esp32_mod.merged_firmware_file(tmp_path)
    # Numeric compare: 1.2.10 outranks 1.2.9 (a lexicographic sort would wrongly
    # pick 1.2.9 and silently downgrade the firmware).
    assert picked is not None
    assert picked.name == f"hokku-{model}-1.2.10.bin"


def test_merged_firmware_file_none_when_empty(esp32_mod, tmp_path):
    assert esp32_mod.merged_firmware_file(tmp_path) is None


def test_release_app_image_slices_app_section(esp32_mod, tmp_path):
    # Build a minimal valid ESP image (magic=0xE9, 0 segments, no hash).
    # After 24-byte header, pos=24, pad=(15-24%16)%16=7, total=24+7+1=32 bytes.
    header = bytearray(24)
    header[0] = 0xE9  # magic
    header[1] = 0  # segment_count = 0
    header[23] = 0  # hash_appended = 0
    app_bytes = bytes(header) + b"\x00" * 8  # pad(7) + checksum(1) = 8 bytes
    assert len(app_bytes) == 32
    app_offset = esp32_mod.constants.APP_OFFSET
    merged = b"\x00" * app_offset + app_bytes + b"\xff" * 100  # trailing junk
    (tmp_path / f"hokku-{esp32_mod.SPEC.model_id}-1.2.8.bin").write_bytes(merged)
    assert esp32_mod.release_app_image(tmp_path) == app_bytes


def test_release_app_image_none_when_no_firmware(esp32_mod, tmp_path):
    assert esp32_mod.release_app_image(tmp_path) is None


# ── esp32.nvs.migrate_config ──────────────────────────────────────────────────


def test_migrate_config_identity_preserves_known_keys(esp32_mod):
    cfg = {
        "wifi_ssid1": "Net",
        "wifi_pass1": "pw",
        "wifi_ssid2": "Backup",
        "wifi_pass2": "pw2",
        "image_url": "http://x/hokku/screen/",
        "screen_name": "Den",
        "wifi_order": 1,
        "cfg_ver": 2,
        "unknown_field": "dropped",
    }
    out = esp32_mod.migrate_config(cfg)
    assert out is not None
    assert out["wifi_ssid1"] == "Net"
    assert out["image_url"] == "http://x/hokku/screen/"
    assert out["screen_name"] == "Den"
    assert out["wifi_order"] == 1
    assert "unknown_field" not in out
    # cfg_ver is stamped by build_nvs_binary, not by migrate_config.
    assert "cfg_ver" not in out


def test_migrate_config_refuses_incomplete_config(esp32_mod):
    assert esp32_mod.migrate_config({"wifi_ssid1": "Net"}) is None  # no image_url
    assert esp32_mod.migrate_config({"image_url": "u"}) is None  # no ssid
    assert esp32_mod.migrate_config({}) is None
    assert esp32_mod.migrate_config("not a dict") is None  # type: ignore[arg-type]


# ── Esp32Spec: the fields that actually differ between the two boards ──────────


def test_seeed_and_huessen_spec_differences():
    """Pin the ONLY real behavioral differences between the two ESP32 boards, so a
    copy-paste in seeed's constants (e.g. a 16MB flash size) can't slip through
    while the rest of the parametrized suite stays green on the identical fields."""
    # Literal model ids (the parametrized tests build filenames from these, so a
    # wrong binding would otherwise be self-consistent and undetected).
    assert huessen_epf1301.SPEC.model_id == "huessen_epf1301"
    assert seeedstudio_e1004.SPEC.model_id == "seeedstudio_e1004"
    # Flash size is the one field that changes esptool's behavior and is asserted
    # nowhere else (esptool is never invoked in these hardware-free tests).
    assert huessen_epf1301.SPEC.flash_size == "16MB"
    assert seeedstudio_e1004.SPEC.flash_size == "32MB"
    # Everything the shared NVS/OTA layer relies on must stay identical.
    for field in ("nvs_offset", "nvs_size", "app_offset", "bootloader_offset", "config_version"):
        assert getattr(huessen_epf1301.SPEC, field) == getattr(seeedstudio_e1004.SPEC, field), field


def test_firmware_config_unknown_model_404(app_config, tmp_path):
    # A real but non-ESP32 model (F7 keeps config device-local) or an unknown one
    # has no NVS config path -> 404 (mirrors firmware.bin's unknown-model 404).
    client = _client(_bare_state(app_config), tmp_path)
    for model in ("bigme_f7", "nonesuch"):
        r = client.get(
            "/hokku/firmware-config",
            headers={
                "X-Screen-Name": "x",
                "X-Screen-Model": model,
                "X-Config-State": json.dumps({"wifi_ssid1": "Net", "image_url": "u"}),
            },
        )
        assert r.status_code == 404, model


# ── /hokku/firmware-config (model-aware NVS serving) ──────────────────────────


def test_firmware_config_missing_header_400(esp32_mod, app_config, tmp_path):
    client = _client(_bare_state(app_config), tmp_path)
    r = client.get(
        "/hokku/firmware-config",
        headers={"X-Screen-Name": "x", "X-Screen-Model": esp32_mod.SPEC.model_id},
    )
    assert r.status_code == 400


def test_firmware_config_refusal_records_error_422(esp32_mod, app_config, tmp_path):
    state = _bare_state(app_config)
    client = _client(state, tmp_path)
    # Missing image_url -> migrate_config refuses.
    r = client.get(
        "/hokku/firmware-config",
        headers={
            "X-Screen-Name": "frame-x",
            "X-Screen-Model": esp32_mod.SPEC.model_id,
            "X-Config-State": json.dumps({"wifi_ssid1": "Net"}),
        },
    )
    assert r.status_code == 422
    assert state.scheduler.screens()["frame-x"].ota_error is not None
    # And it's surfaced through the status payload.
    status = client.get("/hokku/api/status").get_json()
    assert status["screens"]["frame-x"]["ota_error"] is not None


@pytest.mark.skipif(
    not esp32_nvs.nvs_tool_available(), reason="esp-idf-nvs-partition-gen not installed"
)
def test_firmware_config_returns_migrated_nvs_image(esp32_mod, app_config, tmp_path):
    client = _client(_bare_state(app_config), tmp_path)
    cfg = {
        "wifi_ssid1": "Net",
        "wifi_pass1": "pw",
        "image_url": "http://x/hokku/screen/",
        "screen_name": "Den",
        "wifi_order": 1,
    }
    r = client.get(
        "/hokku/firmware-config",
        headers={
            "X-Screen-Name": "Den",
            "X-Screen-Model": esp32_mod.SPEC.model_id,
            "X-Config-State": json.dumps(cfg),
        },
    )
    assert r.status_code == 200
    assert len(r.data) == esp32_mod.NVS_SIZE
    back = esp32_mod.read_nvs(r.data)
    assert back["cfg_ver"] == esp32_mod.CONFIG_VERSION
    assert back["screen_name"] == "Den"
    assert back["image_url"] == cfg["image_url"]
    assert back["wifi_order"] == 1
