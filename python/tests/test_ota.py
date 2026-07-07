"""Tests for the manual OTA firmware-update feature (server side).

Covers:
  huessen_epf1301.release_app_image       — app-only slice of the merged firmware
  huessen_epf1301.migrate_config          — forward config migration + refusal
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

from hokku.screens import bigme_f7, huessen_epf1301
from hokku.screens.huessen_epf1301.constants import APP_OFFSET
from hokku.screens.huessen_epf1301.nvs import migrate_config
from hokku.webserver.app_config import AppConfig
from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.flask_app import OTA_MAX_ATTEMPTS, create_app
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
    # Build a minimal valid ESP image (magic=0xE9, 0 segments, no hash).
    # Structure: 24-byte header, 0 segments, pad to 16-byte checksum alignment, 1 checksum byte.
    # After 24-byte header, pos=24, pad=(15-24%16)%16=(15-8)%16=7, total=24+7+1=32 bytes.
    header = bytearray(24)
    header[0] = 0xE9  # magic
    header[1] = 0  # segment_count = 0
    header[23] = 0  # hash_appended = 0
    app_bytes = bytes(header) + b"\x00" * 8  # pad(7) + checksum(1) = 8 bytes
    assert len(app_bytes) == 32
    merged = b"\x00" * APP_OFFSET + app_bytes + b"\xff" * 100  # trailing junk
    (tmp_path / "hokku-firmware_1.2.8.bin").write_bytes(merged)
    assert huessen_epf1301.release_app_image(tmp_path) == app_bytes


def test_release_app_image_none_when_no_firmware(tmp_path):
    assert huessen_epf1301.release_app_image(tmp_path) is None


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
    monkeypatch.setattr(huessen_epf1301, "release_app_image", lambda *a, **k: b"APPDATA")
    client = _client(_bare_state(app_config), tmp_path)
    r = client.get("/hokku/firmware.bin")
    assert r.status_code == 200
    assert r.data == b"APPDATA"
    assert r.headers["Content-Type"] == "application/octet-stream"


def test_firmware_bin_404_when_absent(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(huessen_epf1301, "release_app_image", lambda *a, **k: None)
    client = _client(_bare_state(app_config), tmp_path)
    assert client.get("/hokku/firmware.bin").status_code == 404


# ── model-aware firmware serving (bigme_f7) ───────────────────────────────────


def test_firmware_bin_model_aware_dispatch(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(huessen_epf1301, "release_app_image", lambda *a, **k: b"HUESSEN")
    monkeypatch.setattr(bigme_f7, "release_app_image", lambda *a, **k: b"BIGME_IMG")
    client = _client(_bare_state(app_config), tmp_path)
    # No model -> huessen (back-compat); explicit model -> that model's bytes.
    assert client.get("/hokku/firmware.bin").data == b"HUESSEN"
    assert client.get("/hokku/firmware.bin?model=bigme_f7").data == b"BIGME_IMG"
    assert (
        client.get("/hokku/firmware.bin", headers={"X-Screen-Model": "bigme_f7"}).data
        == b"BIGME_IMG"
    )


def test_firmware_bin_unknown_model_404(app_config, tmp_path):
    client = _client(_bare_state(app_config), tmp_path)
    assert client.get("/hokku/firmware.bin?model=nonesuch").status_code == 404


def test_bigme_firmware_module_reads_repo_tree():
    # The repo ships the built image + FIRMWARE_VERSION in main.c; the module
    # serves the image verbatim (no slicing) and reads the version from source.
    img = bigme_f7.release_app_image()
    assert img is not None and len(img) > 900_000  # ~1 MB AWIH app-chain
    assert img == bigme_f7.firmware_image_file().read_bytes()
    ver = bigme_f7.bundled_firmware_version()
    assert ver and all(part.isdigit() for part in ver.split("."))


def test_status_reports_per_model_firmware_versions(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(huessen_epf1301, "bundled_firmware_version", lambda *a, **k: "1.2.9")
    monkeypatch.setattr(bigme_f7, "bundled_firmware_version", lambda *a, **k: "1.0.0")
    client = _client(_bare_state(app_config), tmp_path)
    body = client.get("/hokku/api/status").get_json()
    assert body["bundled_firmware_versions"] == {
        "huessen_epf1301": "1.2.9",
        "bigme_f7": "1.0.0",
    }


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
    not huessen_epf1301.nvs_tool_available(), reason="esp-idf-nvs-partition-gen not installed"
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
    assert len(r.data) == huessen_epf1301.NVS_SIZE
    back = huessen_epf1301.read_nvs(r.data)
    assert back["cfg_ver"] == huessen_epf1301.CONFIG_VERSION
    assert back["screen_name"] == "Den"
    assert back["image_url"] == cfg["image_url"]
    assert back["wifi_order"] == 1


# ── POST /hokku/api/screens/<name>/update + serve_binary signalling ───────────


def test_update_toggle_requires_bundled_firmware(app_config, tmp_path, monkeypatch):
    # No firmware for ANY model (frame-1 hasn't reported a model yet) -> blocked.
    monkeypatch.setattr(huessen_epf1301, "bundled_firmware_version", lambda *a, **k: None)
    monkeypatch.setattr(bigme_f7, "bundled_firmware_version", lambda *a, **k: None)
    client = _client(_bare_state(app_config), tmp_path)
    r = client.post("/hokku/api/screens/frame-1/update", json={"enabled": True})
    assert r.status_code == 409


def test_serve_binary_retries_upgrade_until_confirmed(
    app_config, make_test_image, tmp_path, monkeypatch
):
    monkeypatch.setattr(huessen_epf1301, "bundled_firmware_version", lambda *a, **k: "9.9.9")
    state = _state_with_image(app_config, make_test_image)
    client = _client(state, tmp_path)
    assert (
        client.post("/hokku/api/screens/frame-1/update", json={"enabled": True}).status_code == 200
    )

    old = {
        "X-Screen-Name": "frame-1",
        "X-Screen-Model": "huessen_epf1301",
        "X-Firmware-Version": "1.0.0",
        "X-Frame-State": json.dumps({"fw": "1.0.0", "ota": 1}),
    }
    # An upgrade re-signals every poll while the device is still on the old version
    # (so a dropped download self-heals) — not one-shot.
    assert client.get("/hokku/screen/", headers=old).headers.get("X-Firmware-Update") == "9.9.9"
    assert client.get("/hokku/screen/", headers=old).headers.get("X-Firmware-Update") == "9.9.9"
    assert state.scheduler.is_ota_pending("frame-1") is True

    # Once the device reports the target version the update is confirmed: no more
    # signal, and the pending flag clears.
    new = {
        **old,
        "X-Firmware-Version": "9.9.9",
        "X-Frame-State": json.dumps({"fw": "9.9.9", "ota": 1}),
    }
    r = client.get("/hokku/screen/", headers=new)
    assert "X-Firmware-Update" not in r.headers
    assert state.scheduler.is_ota_pending("frame-1") is False


def test_serve_binary_upgrade_gives_up_after_cap(
    app_config, make_test_image, tmp_path, monkeypatch
):
    monkeypatch.setattr(huessen_epf1301, "bundled_firmware_version", lambda *a, **k: "9.9.9")
    state = _state_with_image(app_config, make_test_image)
    client = _client(state, tmp_path)
    client.post("/hokku/api/screens/frame-1/update", json={"enabled": True})
    headers = {
        "X-Screen-Name": "frame-1",
        "X-Screen-Model": "huessen_epf1301",
        "X-Firmware-Version": "1.0.0",
        "X-Frame-State": json.dumps({"fw": "1.0.0", "ota": 1}),
    }
    # Device never reaches the target: signals up to the cap, then gives up + errors.
    for _ in range(OTA_MAX_ATTEMPTS):
        assert client.get("/hokku/screen/", headers=headers).headers.get("X-Firmware-Update") == (
            "9.9.9"
        )
    r = client.get("/hokku/screen/", headers=headers)
    assert "X-Firmware-Update" not in r.headers
    assert state.scheduler.is_ota_pending("frame-1") is False
    assert state.scheduler.screens()["frame-1"].ota_error is not None


def test_serve_binary_reflash_same_version_is_one_shot(
    app_config, make_test_image, tmp_path, monkeypatch
):
    monkeypatch.setattr(huessen_epf1301, "bundled_firmware_version", lambda *a, **k: "9.9.9")
    state = _state_with_image(app_config, make_test_image)
    client = _client(state, tmp_path)
    headers = {
        "X-Screen-Name": "frame-1",
        "X-Screen-Model": "huessen_epf1301",
        "X-Firmware-Version": "9.9.9",
        "X-Frame-State": json.dumps({"fw": "9.9.9", "ota": 1}),
    }
    # Device polls first so the server knows it is already on the target version;
    # toggling then is a re-flash (no version change to confirm) -> one-shot.
    client.get("/hokku/screen/", headers=headers)
    client.post("/hokku/api/screens/frame-1/update", json={"enabled": True})
    assert client.get("/hokku/screen/", headers=headers).headers.get("X-Firmware-Update") == "9.9.9"
    r = client.get("/hokku/screen/", headers=headers)
    assert "X-Firmware-Update" not in r.headers
    assert state.scheduler.is_ota_pending("frame-1") is False


def test_serve_binary_no_signal_when_not_capable(
    app_config, make_test_image, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        huessen_epf1301, "release_app_header", lambda *a, **k: _make_app_header(b"9.9.9")
    )
    state = _state_with_image(app_config, make_test_image)
    client = _client(state, tmp_path)
    client.post("/hokku/api/screens/frame-1/update", json={"enabled": True})

    # No "ota" flag in frame-state -> pre-OTA firmware -> never signalled.
    headers = {
        "X-Screen-Name": "frame-1",
        "X-Screen-Model": "huessen_epf1301",
        "X-Frame-State": json.dumps({"fw": "1.0.0"}),
    }
    r = client.get("/hokku/screen/", headers=headers)
    assert "X-Firmware-Update" not in r.headers
    # Pending flag is preserved (not consumed) so the UI still shows it.
    assert state.scheduler.is_ota_pending("frame-1") is True


def test_serve_binary_no_signal_when_not_pending(
    app_config, make_test_image, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        huessen_epf1301, "release_app_header", lambda *a, **k: _make_app_header(b"9.9.9")
    )
    state = _state_with_image(app_config, make_test_image)
    client = _client(state, tmp_path)
    headers = {
        "X-Screen-Name": "frame-1",
        "X-Screen-Model": "huessen_epf1301",
        "X-Frame-State": json.dumps({"fw": "1.0.0", "ota": 1}),
    }
    r = client.get("/hokku/screen/", headers=headers)
    assert "X-Firmware-Update" not in r.headers
