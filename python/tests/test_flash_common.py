"""Flash / OTA tests common to all screens (model-agnostic or cross-model).

This is the "stuff for all screens" tier, mirroring the firmware's ``common/all``
suite. It covers the parts of the flash + OTA feature that are not specific to one
ESP32 board: the background :class:`FlashJobManager` (incl. its model dispatch),
the model-aware ``/hokku/firmware.bin`` serving + per-model version reporting, the
``/flash/devices`` scan classification, the ServeScheduler OTA pending/error
bookkeeping, and the generic ``serve_binary`` OTA-update signalling.

Per-board ESP32 flash internals live in ``test_flash_esp32.py`` (run against both
ESP32 screens); the Bigme F7 bootstrap lives in ``test_flash_f7.py``.

Hardware-free: esptool/serial are never invoked.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hokku.screens import bigme_f7, huessen_epf1301, seeedstudio_e1004
from hokku.screens.bigme_f7 import firmware as bigme_f7_firmware
from hokku.screens.huessen_epf1301.constants import APP_OFFSET
from hokku.webserver import flashing
from hokku.webserver import flask_app as flask_app_mod
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


def _wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


# ── FlashJobManager ─────────────────────────────────────────────────────────


def test_job_manager_no_job_status_none():
    mgr = flashing.FlashJobManager()
    assert mgr.status() is None
    assert mgr.is_busy() is False


def test_job_manager_runs_and_streams(monkeypatch):
    def fake_flash(port, config, firmware_path, on_line):
        on_line("line one")
        on_line("line two")
        return {"config_version_ok": True}

    monkeypatch.setattr(huessen_epf1301, "flash_device", fake_flash)
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


def test_job_manager_dispatches_by_model(monkeypatch):
    """start(screen_model=...) routes to that screen's flash_device (its spec)."""
    calls: list[str] = []

    def make_fake(name):
        def fake_flash(port, config, firmware_path, on_line):
            calls.append(name)
            on_line(f"flashing {name}")
            return {"config_version_ok": True}

        return fake_flash

    monkeypatch.setattr(huessen_epf1301, "flash_device", make_fake("huessen"))
    monkeypatch.setattr(seeedstudio_e1004, "flash_device", make_fake("seeed"))
    mgr = flashing.FlashJobManager()
    mgr.start("COM9", {}, Path("fw.bin"), screen_model="seeedstudio_e1004")
    _wait_until(lambda: (mgr.status() or {}).get("state") != "running")
    st = mgr.status()
    assert st is not None
    assert st["state"] == "done"
    assert st["screen_model"] == "seeedstudio_e1004"
    assert calls == ["seeed"]


def test_job_manager_unknown_model_errors(monkeypatch):
    mgr = flashing.FlashJobManager()
    mgr.start("COM9", {}, Path("fw.bin"), screen_model="nonesuch")
    _wait_until(lambda: (mgr.status() or {}).get("state") != "running")
    st = mgr.status()
    assert st is not None
    assert st["state"] == "error"
    assert "nonesuch" in st["error"]


def test_job_manager_rejects_concurrent(monkeypatch):
    release = threading.Event()

    def slow_flash(port, config, firmware_path, on_line):
        on_line("working")
        release.wait(timeout=5)

    monkeypatch.setattr(huessen_epf1301, "flash_device", slow_flash)
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
        raise huessen_epf1301.EsptoolError("esptool exited 2")

    monkeypatch.setattr(huessen_epf1301, "flash_device", boom)
    mgr = flashing.FlashJobManager()
    mgr.start("COM9", {}, Path("fw.bin"))
    _wait_until(lambda: (mgr.status() or {}).get("state") != "running")
    st = mgr.status()
    assert st is not None
    assert st["state"] == "error"
    assert "esptool exited 2" in st["error"]
    assert any("ERROR" in line for line in st["log"])


def _slow_flash(release):
    def slow(port, config, firmware_path, on_line):
        on_line("working")
        release.wait(timeout=5)

    return slow


def test_scan_holds_the_single_slot():
    mgr = flashing.FlashJobManager()
    assert mgr.begin_scan() is True
    assert mgr.is_busy() is True
    # A flash cannot start while a scan holds the serial slot (the TOCTOU fix).
    assert mgr.start("COM9", {}, Path("fw.bin")) is None
    # Nor can a second scan reserve it.
    assert mgr.begin_scan() is False
    mgr.end_scan()
    assert mgr.is_busy() is False
    # Slot is free again for a flash.
    assert mgr.begin_scan() is True
    mgr.end_scan()


def test_flash_blocks_scan(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(huessen_epf1301, "flash_device", _slow_flash(release))
    mgr = flashing.FlashJobManager()
    mgr.start("COM9", {}, Path("fw.bin"))
    _wait_until(lambda: mgr.is_busy())
    assert mgr.begin_scan() is False  # scan refused while a flash runs
    release.set()
    _wait_until(lambda: not mgr.is_busy())


def test_cancel_refuses_esp32_flash(monkeypatch):
    # esptool is not interruptible, so cancel() must NOT falsely report success.
    release = threading.Event()
    monkeypatch.setattr(huessen_epf1301, "flash_device", _slow_flash(release))
    mgr = flashing.FlashJobManager()
    mgr.start("COM9", {}, Path("fw.bin"))
    _wait_until(lambda: mgr.is_busy())
    assert mgr.cancel() is False
    release.set()
    _wait_until(lambda: not mgr.is_busy())


def test_thread_start_failure_frees_slot(monkeypatch):
    # If the OS refuses a new thread, the job must not wedge the slot in "running".
    def boom(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", boom)
    mgr = flashing.FlashJobManager()
    job_id = mgr.start("COM9", {}, Path("fw.bin"))
    assert job_id is not None
    st = mgr.status()
    assert st is not None
    assert st["state"] == "error"
    assert "could not start" in st["error"]
    assert mgr.is_busy() is False  # slot freed, next flash can proceed


# ── Flask flash routes ────────────────────────────────────────────────────────


@pytest.fixture
def flash_client(app_config):
    clf = ImageClassifier(app_config)
    mgr = build_manager(app_config, clf)
    state = AppState(app_config, clf, mgr, ServeScheduler(mgr))
    app = create_app(state)
    app.config["TESTING"] = True
    return app.test_client()


def test_server_url_uses_local_ip(app_config, monkeypatch):
    # Force IP path: no mDNS configured, so endpoint falls back to local IP.
    cfg = replace(app_config, mdns_hostname="")
    clf = ImageClassifier(cfg)
    mgr = build_manager(cfg, clf)
    state = AppState(cfg, clf, mgr, ServeScheduler(mgr))
    app = create_app(state)
    app.config["TESTING"] = True
    client = app.test_client()

    monkeypatch.setattr("hokku.webserver.flask_app._get_local_ip", lambda: "10.1.2.3")
    r = client.get("/hokku/api/flash/server_url")
    assert r.status_code == 200
    j = r.get_json()
    assert j["address"] == "10.1.2.3"
    assert j["url"] == f"http://10.1.2.3:{j['port']}/hokku/screen/"


def test_flash_start_requires_fields(flash_client):
    r = flash_client.post("/hokku/api/flash/start", json={})
    # 400 when firmware + NVS tool are available; 503 if either is missing.
    # Either way the flash is not started.
    assert r.status_code in (400, 503)


def test_flash_start_unknown_model_400(flash_client):
    # An unrecognised ESP32 model is rejected up front, regardless of firmware.
    r = flash_client.post(
        "/hokku/api/flash/start",
        json={"screen_model": "nonesuch", "port": "COM9", "wifi_ssid1": "x", "image_url": "y"},
    )
    assert r.status_code == 400
    assert "nonesuch" in r.get_json()["error"]


def test_flash_status_starts_idle(flash_client):
    r = flash_client.get("/hokku/api/flash/status")
    assert r.status_code == 200
    assert r.get_json()["state"] == "idle"


def test_flash_start_dispatches_selected_model_end_to_end(app_config, tmp_path, monkeypatch):
    """A POST with screen_model=seeedstudio_e1004 must flash with SEEED's firmware
    file and model — the operator's selector actually drives a seeed flash, not
    huessen's spec. This is the headline model-aware-dispatch path end to end."""
    seeed_fw = tmp_path / "hokku-seeedstudio_e1004-1.2.0.bin"
    seeed_fw.write_bytes(b"\x00" * (APP_OFFSET + 256))
    monkeypatch.setattr(seeedstudio_e1004, "merged_firmware_file", lambda *a, **k: seeed_fw)
    monkeypatch.setattr(seeedstudio_e1004, "nvs_tool_available", lambda: True)
    state = _bare_state(app_config)

    captured: dict = {}

    def fake_start(port, config, firmware_path, screen_model="huessen_epf1301"):
        captured.update(
            port=port, firmware_path=firmware_path, screen_model=screen_model, config=config
        )
        return 42

    monkeypatch.setattr(state.flash_jobs, "start", fake_start)
    client = _client(state, tmp_path)
    r = client.post(
        "/hokku/api/flash/start",
        json={
            "screen_model": "seeedstudio_e1004",
            "port": "COM9",
            "wifi_ssid1": "Net",
            "image_url": "http://x/hokku/screen/",
            "screen_name": "Den",
        },
    )
    assert r.status_code == 200
    assert r.get_json()["job_id"] == 42
    assert captured["screen_model"] == "seeedstudio_e1004"
    assert captured["firmware_path"] == seeed_fw  # seeed's bin, not huessen's
    assert captured["port"] == "COM9"


def _flash_ready_client(app_config, tmp_path, monkeypatch):
    """A client where huessen firmware + NVS tool are present, so /flash/start
    reaches field validation instead of short-circuiting on 503."""
    fw = tmp_path / "hokku-huessen_epf1301-1.2.9.bin"
    fw.write_bytes(b"\x00" * (APP_OFFSET + 256))
    monkeypatch.setattr(huessen_epf1301, "merged_firmware_file", lambda *a, **k: fw)
    monkeypatch.setattr(huessen_epf1301, "nvs_tool_available", lambda: True)
    return _client(_bare_state(app_config), tmp_path)


def test_flash_start_rejects_oversized_field(app_config, tmp_path, monkeypatch):
    client = _flash_ready_client(app_config, tmp_path, monkeypatch)
    r = client.post(
        "/hokku/api/flash/start",
        json={"port": "COM9", "wifi_ssid1": "x" * 33, "image_url": "http://x/hokku/screen/"},
    )
    assert r.status_code == 400
    assert "wifi_ssid1" in r.get_json()["error"]


def test_flash_start_rejects_bad_wifi_order(app_config, tmp_path, monkeypatch):
    client = _flash_ready_client(app_config, tmp_path, monkeypatch)
    r = client.post(
        "/hokku/api/flash/start",
        json={
            "port": "COM9",
            "wifi_ssid1": "Net",
            "image_url": "http://x/hokku/screen/",
            "wifi_order": "not-an-int",
        },
    )
    assert r.status_code == 400  # clean 400, not a 500


def test_flash_devices_503_when_no_esp32_firmware(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(huessen_epf1301, "merged_firmware_file", lambda *a, **k: None)
    monkeypatch.setattr(seeedstudio_e1004, "merged_firmware_file", lambda *a, **k: None)
    client = _client(_bare_state(app_config), tmp_path)
    assert client.get("/hokku/api/flash/devices").status_code == 503


def test_flash_devices_allowed_when_only_seeed_has_firmware(app_config, tmp_path, monkeypatch):
    seeed_fw = tmp_path / "hokku-seeedstudio_e1004-1.2.0.bin"
    seeed_fw.write_bytes(b"\x00")
    monkeypatch.setattr(huessen_epf1301, "merged_firmware_file", lambda *a, **k: None)
    monkeypatch.setattr(seeedstudio_e1004, "merged_firmware_file", lambda *a, **k: seeed_fw)
    monkeypatch.setattr(huessen_epf1301, "scan_devices", lambda: [])
    client = _client(_bare_state(app_config), tmp_path)
    assert client.get("/hokku/api/flash/devices").status_code == 200


def test_firmware_config_refused_when_build_slots_exhausted(app_config, tmp_path):
    # Hold every NVS-build slot so the next /firmware-config gets a 503 instead of
    # spawning yet another generator subprocess (the DoS guard).
    slots = flask_app_mod._nvs_build_slots
    held = 0
    try:
        while slots.acquire(blocking=False):
            held += 1
        assert held == flask_app_mod.OTA_NVS_MAX_CONCURRENT_BUILDS
        client = _client(_bare_state(app_config), tmp_path)
        r = client.get(
            "/hokku/firmware-config",
            headers={
                "X-Screen-Name": "Den",
                "X-Config-State": json.dumps(
                    {"wifi_ssid1": "Net", "image_url": "http://x/hokku/screen/"}
                ),
            },
        )
        assert r.status_code == 503
    finally:
        for _ in range(held):
            slots.release()


# ── /hokku/firmware.bin (model-aware OTA image serving) ───────────────────────


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


def test_firmware_bin_model_aware_dispatch(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(huessen_epf1301, "release_app_image", lambda *a, **k: b"HUESSEN")
    monkeypatch.setattr(seeedstudio_e1004, "release_app_image", lambda *a, **k: b"SEEED_IMG")
    monkeypatch.setattr(bigme_f7, "release_app_image", lambda *a, **k: b"BIGME_IMG")
    client = _client(_bare_state(app_config), tmp_path)
    # No model -> huessen (back-compat); explicit model -> that model's bytes.
    assert client.get("/hokku/firmware.bin").data == b"HUESSEN"
    assert client.get("/hokku/firmware.bin?model=seeedstudio_e1004").data == b"SEEED_IMG"
    assert client.get("/hokku/firmware.bin?model=bigme_f7").data == b"BIGME_IMG"
    assert (
        client.get("/hokku/firmware.bin", headers={"X-Screen-Model": "bigme_f7"}).data
        == b"BIGME_IMG"
    )


def test_firmware_bin_unknown_model_404(app_config, tmp_path):
    client = _client(_bare_state(app_config), tmp_path)
    assert client.get("/hokku/firmware.bin?model=nonesuch").status_code == 404


def test_bigme_firmware_module_discovers_release_image(tmp_path, monkeypatch):
    # The release artifact is hokku-bigme_f7-<version>.img in the shared
    # firmware/release/ dir; the module serves it verbatim (no slicing) and
    # parses the version from the filename (build artifacts aren't committed,
    # so this must not depend on a real build being present on disk).
    (tmp_path / "hokku-bigme_f7-1.2.2.img").write_bytes(b"X" * 1_000_000)
    monkeypatch.setattr(bigme_f7_firmware, "_DEV_FIRMWARE_DIR", tmp_path)
    monkeypatch.setattr(bigme_f7_firmware, "_INSTALLED_FIRMWARE_DIR", tmp_path / "nonexistent")

    img = bigme_f7.release_app_image()
    assert img == b"X" * 1_000_000
    fw_file = bigme_f7.firmware_image_file()
    assert fw_file is not None
    assert img == fw_file.read_bytes()
    assert bigme_f7.bundled_firmware_version() == "1.2.2"


def test_status_reports_per_model_firmware_versions(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(huessen_epf1301, "bundled_firmware_version", lambda *a, **k: "1.2.9")
    monkeypatch.setattr(seeedstudio_e1004, "bundled_firmware_version", lambda *a, **k: "1.2.0")
    monkeypatch.setattr(bigme_f7, "bundled_firmware_version", lambda *a, **k: "1.0.0")
    client = _client(_bare_state(app_config), tmp_path)
    body = client.get("/hokku/api/status").get_json()
    assert body["bundled_firmware_versions"] == {
        "huessen_epf1301": "1.2.9",
        "seeedstudio_e1004": "1.2.0",
        "bigme_f7": "1.0.0",
    }


def test_flash_devices_classifies_bigme_f7(app_config, tmp_path, monkeypatch):
    # firmware must be present so the /flash/devices route doesn't 503.
    fw = tmp_path / "hokku-huessen_epf1301-1.2.9.bin"
    fw.write_bytes(b"\x00" * (APP_OFFSET + 256))
    monkeypatch.setattr(huessen_epf1301, "merged_firmware_file", lambda *a, **k: fw)
    monkeypatch.setattr(
        huessen_epf1301,
        "scan_devices",
        lambda: [
            {
                "port": "COM7",
                "description": "USB-SERIAL CH340",
                "vid": 0x1A86,
                "pid": 0x7523,
                "is_esp32": False,
            },
            {
                "port": "COM3",
                "description": "ESP32-S3",
                "vid": 0x303A,
                "pid": 0x1001,
                "is_esp32": True,
            },
        ],
    )
    client = _client(_bare_state(app_config), tmp_path)
    devs = {d["port"]: d for d in client.get("/hokku/api/flash/devices").get_json()["devices"]}
    assert devs["COM7"]["is_bigme_f7"] is True  # CH340 -> Bigme F7
    assert devs["COM3"]["is_bigme_f7"] is False  # ESP32-S3 -> not F7


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


# ── POST /hokku/api/screens/<name>/update + serve_binary signalling ───────────


def test_update_toggle_requires_bundled_firmware(app_config, tmp_path, monkeypatch):
    # No firmware for ANY model (frame-1 hasn't reported a model yet) -> blocked.
    monkeypatch.setattr(huessen_epf1301, "bundled_firmware_version", lambda *a, **k: None)
    monkeypatch.setattr(seeedstudio_e1004, "bundled_firmware_version", lambda *a, **k: None)
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
    # Needed for the /update POST below: it 409s ("no bundled firmware") unless
    # a version is discoverable, which normally comes from a real release file.
    monkeypatch.setattr(huessen_epf1301, "bundled_firmware_version", lambda *a, **k: "9.9.9")
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
