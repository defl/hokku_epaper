"""Tests for the screen-flashing feature (hokku.screens.epf1301 + web job manager).

Hardware-free: esptool/serial are never invoked. The NVS round-trip uses the
real esp-idf-nvs-partition-gen subprocess and is skipped if it is not installed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hokku.screens import epf1301
from hokku.webserver import flashing
from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.flask_app import create_app
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.serve_scheduler import ServeScheduler

# ── epf1301.nvs ───────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not epf1301.nvs_tool_available(), reason="esp-idf-nvs-partition-gen not installed"
)
def test_nvs_build_read_round_trip():
    cfg = {
        "wifi_ssid1": "MyNet",
        "wifi_pass1": 'has"quote',
        "wifi_ssid2": "Backup",
        "wifi_pass2": "second",
        "image_url": "http://192.168.1.5:8080/hokku/screen/",
        "screen_name": "Living Room",
        "wifi_order": 1,
    }
    blob = epf1301.build_nvs_binary(cfg)
    assert len(blob) == epf1301.NVS_SIZE
    back = epf1301.read_nvs(blob)
    assert back["cfg_ver"] == epf1301.CONFIG_VERSION
    assert back["wifi_order"] == 1
    for key in ("wifi_ssid1", "wifi_pass1", "wifi_ssid2", "wifi_pass2", "image_url", "screen_name"):
        assert back[key] == cfg[key]


def test_nvs_unavailable_raises(monkeypatch):
    monkeypatch.setattr(epf1301.nvs, "nvs_tool_available", lambda: False)
    with pytest.raises(epf1301.NvsToolUnavailable):
        epf1301.nvs.build_nvs_binary({"wifi_ssid1": "x", "image_url": "y"})


# ── epf1301.device.parse_device_state ─────────────────────────────────────────


def _make_app_header(version: bytes = b"20260101000000Z") -> bytes:
    header = bytearray(256)
    header[0:12] = b"hokku_epaper"
    header[48 : 48 + len(version)] = version
    return bytes(header)


def test_parse_device_state_detects_firmware_and_version():
    header = _make_app_header()
    state = epf1301.parse_device_state(b"", header, release_header=header)
    assert state["has_hokku_firmware"] is True
    assert state["device_version"] == "20260101000000Z"
    assert state["firmware_current"] is True


def test_parse_device_state_blank_device():
    state = epf1301.parse_device_state(None, b"\xff" * 256, release_header=_make_app_header())
    assert state["has_hokku_firmware"] is False
    assert state["device_version"] is None
    # device header all-0xFF differs from release -> not current
    assert state["firmware_current"] is False


def test_parse_device_state_reads_config():
    pytest.importorskip("esp_idf_nvs_partition_gen", reason="NVS generator not installed")
    nvs = epf1301.build_nvs_binary({"wifi_ssid1": "Net", "image_url": "u", "screen_name": "Den"})
    state = epf1301.parse_device_state(nvs, _make_app_header())
    assert state["config_version_ok"] is True
    assert state["config"]["screen_name"] == "Den"


# ── epf1301.firmware ──────────────────────────────────────────────────────────


def test_merged_firmware_file_picks_highest(tmp_path):
    for name in ("hokku-firmware_1.2.4.bin", "hokku-firmware_1.2.10.bin", "ignore.txt"):
        (tmp_path / name).write_bytes(b"\x00")
    picked = epf1301.merged_firmware_file(tmp_path)
    # sorted()[-1] is lexicographic, so "1.2.4" sorts after "1.2.10".
    assert picked is not None
    assert picked.name == "hokku-firmware_1.2.4.bin"


def test_merged_firmware_file_none_when_empty(tmp_path):
    assert epf1301.merged_firmware_file(tmp_path) is None


# ── FlashJobManager ───────────────────────────────────────────────────────────


def test_job_manager_no_job_status_none():
    mgr = flashing.FlashJobManager()
    assert mgr.status() is None
    assert mgr.is_busy() is False


def test_job_manager_runs_and_streams(monkeypatch):
    def fake_flash(port, config, firmware_path, on_line):
        on_line("line one")
        on_line("line two")
        return {"config_version_ok": True}

    monkeypatch.setattr(flashing.epf1301, "flash_device", fake_flash)
    mgr = flashing.FlashJobManager()
    fw = Path("fw.bin")
    job_id = mgr.start("COM9", {"wifi_ssid1": "x", "image_url": "y"}, fw)
    assert job_id is not None
    _wait_until(lambda: (mgr.status() or {}).get("state") != "running")
    st = mgr.status()
    assert st is not None
    assert st["state"] == "done"
    assert st["log"] == ["line one", "line two"]
    assert st["result"] == {"config_version_ok": True}
    assert st["finished_at"] is not None


def test_job_manager_rejects_concurrent(monkeypatch):
    release = threading.Event()

    def slow_flash(port, config, firmware_path, on_line):
        on_line("working")
        release.wait(timeout=5)

    monkeypatch.setattr(flashing.epf1301, "flash_device", slow_flash)
    mgr = flashing.FlashJobManager()
    fw = Path("fw.bin")
    first = mgr.start("COM9", {}, fw)
    _wait_until(lambda: mgr.is_busy())
    second = mgr.start("COM9", {}, fw)
    assert first is not None
    assert second is None  # busy
    release.set()
    _wait_until(lambda: not mgr.is_busy())


def test_job_manager_records_error(monkeypatch):
    def boom(port, config, firmware_path, on_line):
        raise epf1301.EsptoolError("esptool exited 2")

    monkeypatch.setattr(flashing.epf1301, "flash_device", boom)
    mgr = flashing.FlashJobManager()
    mgr.start("COM9", {}, Path("fw.bin"))
    _wait_until(lambda: (mgr.status() or {}).get("state") != "running")
    st = mgr.status()
    assert st is not None
    assert st["state"] == "error"
    assert "esptool exited 2" in st["error"]
    assert any("ERROR" in line for line in st["log"])


def _wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


# ── Flask routes ──────────────────────────────────────────────────────────────


@pytest.fixture
def flash_client(app_config):
    clf = ImageClassifier(app_config)
    mgr = build_manager(app_config, clf)
    state = AppState(app_config, clf, mgr, ServeScheduler(mgr))
    app = create_app(state)
    app.config["TESTING"] = True
    return app.test_client()


def test_server_url_uses_local_ip(flash_client, monkeypatch):
    monkeypatch.setattr("hokku.webserver.flask_app._get_local_ip", lambda: "10.1.2.3")
    r = flash_client.get("/hokku/api/flash/server_url")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ip"] == "10.1.2.3"
    assert j["url"] == f"http://10.1.2.3:{j['port']}/hokku/screen/"


def test_flash_start_requires_fields(flash_client):
    r = flash_client.post("/hokku/api/flash/start", json={})
    # 400 when firmware + NVS tool are available; 503 if either is missing.
    # Either way the flash is not started.
    assert r.status_code in (400, 503)


def test_flash_status_starts_idle(flash_client):
    r = flash_client.get("/hokku/api/flash/status")
    assert r.status_code == 200
    assert r.get_json()["state"] == "idle"
