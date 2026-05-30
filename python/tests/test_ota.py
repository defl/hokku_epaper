"""Tests for the manual OTA firmware-update feature (server side).

Covers:
  epf1301.release_app_image       — app-only slice of the merged firmware
  epf1301.migrate_config          — forward config migration + refusal
  ServeScheduler OTA pending/error — one-shot toggle, persistence, sticky error
  GET  /hokku/firmware.bin        — app image / 404
  GET  /hokku/firmware-config     — migrated NVS image / 400 / 422 + recorded error
  POST /hokku/api/screens/<n>/update — toggle the pending flag
  GET  /hokku/screen/             — X-Firmware-Update signalling (capable + pending)

Hardware-free; the NVS-generator round-trip is skipped if the package is absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hokku.screens import epf1301
from hokku.screens.epf1301.constants import APP_OFFSET
from hokku.screens.epf1301.nvs import migrate_config
from hokku.webserver.app_config import AppConfig
from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.flask_app import create_app
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.image_manager_single import SingleThreadedImageManager
from hokku.webserver.serve_scheduler import ServeScheduler

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_app_header(version: bytes = b"9.9.9") -> bytes:
    """A 256-byte app header with the version string at bytes 48:80."""
    header = bytearray(256)
    header[0:12] = b"hokku_epaper"
    header[48 : 48 + len(version)] = version
    return bytes(header)


def _sched(app_config: AppConfig) -> tuple[SingleThreadedImageManager, ServeScheduler]:
    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    return mgr, ServeScheduler(mgr)


def _bare_state(app_config: AppConfig) -> AppState:
    clf = ImageClassifier(app_config)
    mgr = build_manager(app_config, clf)
    return AppState(app_config, clf, mgr, ServeScheduler(mgr))


def _state_with_image(app_config: AppConfig, make_test_image) -> AppState:
    clf = ImageClassifier(app_config)
    mgr = build_manager(app_config, clf)
    make_test_image(Path(app_config.upload_dir) / "a.png")
    mgr.sync()
    mgr.wait_for_idle()
    return AppState(app_config, clf, mgr, ServeScheduler(mgr))


def _client(state: AppState, tmp_path: Path):
    app = create_app(state, config_path=tmp_path / "cfg.json", template_folder=None)
    app.config["TESTING"] = True
    return app.test_client()


# ── release_app_image ───────────────────────────────────────────────────────


def test_release_app_image_slices_app_section(tmp_path):
    app_bytes = b"APPSTART" + bytes(range(50))
    merged = b"\x00" * APP_OFFSET + app_bytes
    (tmp_path / "hokku-firmware_1.2.8.bin").write_bytes(merged)
    assert epf1301.release_app_image(tmp_path) == app_bytes


def test_release_app_image_none_when_no_firmware(tmp_path):
    assert epf1301.release_app_image(tmp_path) is None


# ── migrate_config ────────────────────────────────────────────────────────────


def test_migrate_config_identity_preserves_known_keys():
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
    out = migrate_config(cfg)
    assert out is not None
    assert out["wifi_ssid1"] == "Net"
    assert out["image_url"] == "http://x/hokku/screen/"
    assert out["screen_name"] == "Den"
    assert out["wifi_order"] == 1
    assert "unknown_field" not in out
    # cfg_ver is stamped by build_nvs_binary, not by migrate_config.
    assert "cfg_ver" not in out


def test_migrate_config_refuses_incomplete_config():
    assert migrate_config({"wifi_ssid1": "Net"}) is None  # no image_url
    assert migrate_config({"image_url": "u"}) is None  # no ssid
    assert migrate_config({}) is None
    assert migrate_config("not a dict") is None  # type: ignore[arg-type]


# ── ServeScheduler: OTA pending flag ──────────────────────────────────────────


def test_ota_pending_is_one_shot(app_config):
    _, sched = _sched(app_config)
    assert sched.is_ota_pending("a") is False
    sched.set_ota_pending("a", True)
    assert sched.is_ota_pending("a") is True
    assert sched.take_ota_pending("a") is True
    # consumed
    assert sched.take_ota_pending("a") is False
    assert sched.is_ota_pending("a") is False


def test_ota_pending_can_be_cleared(app_config):
    _, sched = _sched(app_config)
    sched.set_ota_pending("a", True)
    sched.set_ota_pending("a", False)
    assert sched.is_ota_pending("a") is False


def test_ota_pending_persists_across_reload(app_config):
    mgr, sched = _sched(app_config)
    sched.set_ota_pending("a", True)
    reloaded = ServeScheduler(mgr)
    assert reloaded.is_ota_pending("a") is True


# ── ServeScheduler: OTA error ─────────────────────────────────────────────────


def test_ota_error_recorded_and_sticky_then_cleared(app_config):
    _, sched = _sched(app_config)
    sched.record_ota_error("a", "boom")
    assert sched.screens()["a"].ota_error == "boom"
    assert sched.screens()["a"].ota_error_at is not None
    # A normal poll must NOT wipe the error.
    sched.record_screen_call("a", "1.2.3.4", 60, None, None, {"fw": "1.0.0"})
    assert sched.screens()["a"].ota_error == "boom"
    # Explicit clear.
    sched.clear_ota_error("a")
    assert sched.screens()["a"].ota_error is None
    assert sched.screens()["a"].ota_error_at is None


def test_ota_error_persists_across_reload(app_config):
    mgr, sched = _sched(app_config)
    sched.record_ota_error("a", "boom")
    reloaded = ServeScheduler(mgr)
    assert reloaded.screens()["a"].ota_error == "boom"


# ── /hokku/firmware.bin ───────────────────────────────────────────────────────


def test_firmware_bin_served(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(epf1301, "release_app_image", lambda *a, **k: b"APPDATA")
    client = _client(_bare_state(app_config), tmp_path)
    r = client.get("/hokku/firmware.bin")
    assert r.status_code == 200
    assert r.data == b"APPDATA"
    assert r.headers["Content-Type"] == "application/octet-stream"


def test_firmware_bin_404_when_absent(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(epf1301, "release_app_image", lambda *a, **k: None)
    client = _client(_bare_state(app_config), tmp_path)
    assert client.get("/hokku/firmware.bin").status_code == 404


# ── /hokku/firmware-config ────────────────────────────────────────────────────


def test_firmware_config_missing_header_400(app_config, tmp_path):
    client = _client(_bare_state(app_config), tmp_path)
    r = client.get("/hokku/firmware-config", headers={"X-Screen-Name": "x"})
    assert r.status_code == 400


def test_firmware_config_refusal_records_error_422(app_config, tmp_path):
    state = _bare_state(app_config)
    client = _client(state, tmp_path)
    # Missing image_url -> migrate_config refuses.
    r = client.get(
        "/hokku/firmware-config",
        headers={"X-Screen-Name": "frame-x", "X-Config-State": json.dumps({"wifi_ssid1": "Net"})},
    )
    assert r.status_code == 422
    assert state.scheduler.screens()["frame-x"].ota_error is not None
    # And it's surfaced through the status payload.
    status = client.get("/hokku/api/status").get_json()
    assert status["screens"]["frame-x"]["ota_error"] is not None


@pytest.mark.skipif(
    not epf1301.nvs_tool_available(), reason="esp-idf-nvs-partition-gen not installed"
)
def test_firmware_config_returns_migrated_nvs_image(app_config, tmp_path):
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
        headers={"X-Screen-Name": "Den", "X-Config-State": json.dumps(cfg)},
    )
    assert r.status_code == 200
    assert len(r.data) == epf1301.NVS_SIZE
    back = epf1301.read_nvs(r.data)
    assert back["cfg_ver"] == epf1301.CONFIG_VERSION
    assert back["screen_name"] == "Den"
    assert back["image_url"] == cfg["image_url"]
    assert back["wifi_order"] == 1


# ── POST /hokku/api/screens/<name>/update + serve_binary signalling ───────────


def test_update_toggle_requires_bundled_firmware(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(epf1301, "release_app_header", lambda *a, **k: None)
    client = _client(_bare_state(app_config), tmp_path)
    r = client.post("/hokku/api/screens/frame-1/update", json={"enabled": True})
    assert r.status_code == 409


def test_serve_binary_signals_ota_once_when_capable_and_pending(
    app_config, make_test_image, tmp_path, monkeypatch
):
    monkeypatch.setattr(epf1301, "release_app_header", lambda *a, **k: _make_app_header(b"9.9.9"))
    state = _state_with_image(app_config, make_test_image)
    client = _client(state, tmp_path)

    assert (
        client.post("/hokku/api/screens/frame-1/update", json={"enabled": True}).status_code == 200
    )

    headers = {"X-Screen-Name": "frame-1", "X-Frame-State": json.dumps({"fw": "1.0.0", "ota": 1})}
    r1 = client.get("/hokku/screen/", headers=headers)
    assert r1.status_code == 200
    assert r1.headers.get("X-Firmware-Update") == "9.9.9"

    # One-shot: the next poll must not re-signal.
    r2 = client.get("/hokku/screen/", headers=headers)
    assert "X-Firmware-Update" not in r2.headers


def test_serve_binary_no_signal_when_not_capable(
    app_config, make_test_image, tmp_path, monkeypatch
):
    monkeypatch.setattr(epf1301, "release_app_header", lambda *a, **k: _make_app_header(b"9.9.9"))
    state = _state_with_image(app_config, make_test_image)
    client = _client(state, tmp_path)
    client.post("/hokku/api/screens/frame-1/update", json={"enabled": True})

    # No "ota" flag in frame-state -> pre-OTA firmware -> never signalled.
    headers = {"X-Screen-Name": "frame-1", "X-Frame-State": json.dumps({"fw": "1.0.0"})}
    r = client.get("/hokku/screen/", headers=headers)
    assert "X-Firmware-Update" not in r.headers
    # Pending flag is preserved (not consumed) so the UI still shows it.
    assert state.scheduler.is_ota_pending("frame-1") is True


def test_serve_binary_no_signal_when_not_pending(
    app_config, make_test_image, tmp_path, monkeypatch
):
    monkeypatch.setattr(epf1301, "release_app_header", lambda *a, **k: _make_app_header(b"9.9.9"))
    state = _state_with_image(app_config, make_test_image)
    client = _client(state, tmp_path)
    headers = {"X-Screen-Name": "frame-1", "X-Frame-State": json.dumps({"fw": "1.0.0", "ota": 1})}
    r = client.get("/hokku/screen/", headers=headers)
    assert "X-Firmware-Update" not in r.headers
