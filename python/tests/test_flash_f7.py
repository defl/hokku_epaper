"""Tests for the Bigme F7 (XR872) web bootstrap: FlashJobManager.start_f7 / cancel,
the bootstrap streaming/cancel contract, and the /flash/start_f7 + /flash/cancel
routes. Hardware-free — the BROM catch + slot-0 write are stubbed."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from hokku.screens import huessen_epf1301
from hokku.screens.bigme_f7 import bootstrap as bigme_bootstrap
from hokku.screens.bigme_f7 import config as f7cfg
from hokku.webserver import flashing
from hokku.webserver.app_config import AppConfig
from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.flask_app import create_app
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.serve_scheduler import ServeScheduler


def _bare_state(app_config: AppConfig) -> AppState:
    clf = ImageClassifier(app_config)
    mgr = build_manager(app_config, clf)
    return AppState(app_config, clf, mgr, ServeScheduler(mgr))


def _client(state: AppState, tmp_path: Path):
    app = create_app(state, config_path=tmp_path / "cfg.json", template_folder=None)
    app.config["TESTING"] = True
    return app.test_client()


def _wait_state(mgr: flashing.FlashJobManager, target: str, timeout: float = 3.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = mgr.status() or {}
        if st.get("state") == target:
            return st
        time.sleep(0.02)
    return mgr.status() or {}


# ── _LineWriter ───────────────────────────────────────────────────────────────


def test_line_writer_splits_and_flushes():
    lines: list[str] = []
    w = bigme_bootstrap._LineWriter(lines.append)
    w.write("hello\nwor")
    assert lines == ["hello"]  # only the completed line
    w.write("ld\n")
    assert lines == ["hello", "world"]
    w.write("trailing")  # no newline yet
    assert lines == ["hello", "world"]
    w.flush()  # flush emits the partial line
    assert lines == ["hello", "world", "trailing"]


def test_line_writer_strips_carriage_return():
    lines: list[str] = []
    w = bigme_bootstrap._LineWriter(lines.append)
    w.write("a\r\nb\r\n")
    assert lines == ["a", "b"]


# ── bootstrap_device contract ─────────────────────────────────────────────────


def test_bootstrap_requires_tooling(monkeypatch):
    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: False)
    try:
        bigme_bootstrap.bootstrap_device("COM7", "x.img", lambda _l: None, lambda: False)
    except RuntimeError as e:
        assert "tooling" in str(e).lower()
    else:
        raise AssertionError("expected RuntimeError when tooling is absent")


# ── FlashJobManager.start_f7 / cancel ─────────────────────────────────────────


def test_start_f7_runs_to_done(monkeypatch):
    def fake_bootstrap(port, image_path, on_line, should_cancel, **kw):
        on_line(f"catching on {port}")
        on_line("*** BROM caught ***")
        return {"ok": True}

    monkeypatch.setattr(flashing.f7_bootstrap, "bootstrap_device", fake_bootstrap)
    mgr = flashing.FlashJobManager()
    job_id = mgr.start_f7("COM7", Path("xr_system.img"))
    assert job_id is not None
    st = _wait_state(mgr, "done")
    assert st["state"] == "done"
    assert st["kind"] == "bigme_f7"
    assert st["result"] == {"ok": True}
    assert any("BROM caught" in ln for ln in st["log"])


def test_start_f7_is_single_slot(monkeypatch):
    started = {"go": False}

    def blocking(port, image_path, on_line, should_cancel, **kw):
        while not should_cancel():
            time.sleep(0.01)
        raise RuntimeError("cancelled by the operator")

    monkeypatch.setattr(flashing.f7_bootstrap, "bootstrap_device", blocking)
    mgr = flashing.FlashJobManager()
    assert mgr.start_f7("COM7", Path("a.img")) is not None
    # a second start is refused while the first runs
    assert mgr.start_f7("COM7", Path("a.img")) is None
    assert mgr.is_busy() is True
    mgr.cancel()
    _wait_state(mgr, "error")
    _ = started


def test_cancel_stops_the_catch_loop(monkeypatch):
    def blocking(port, image_path, on_line, should_cancel, **kw):
        on_line("waiting for BROM")
        while not should_cancel():
            time.sleep(0.01)
        raise RuntimeError("cancelled by the operator")

    monkeypatch.setattr(flashing.f7_bootstrap, "bootstrap_device", blocking)
    mgr = flashing.FlashJobManager()
    mgr.start_f7("COM7", Path("a.img"))
    assert _wait_state(mgr, "running", 1.0)["state"] == "running"
    assert mgr.cancel() is True
    st = _wait_state(mgr, "error")
    assert st["state"] == "error"
    assert "cancelled" in (st["error"] or "").lower()


def test_cancel_with_no_job_is_false():
    assert flashing.FlashJobManager().cancel() is False


# ── routes ────────────────────────────────────────────────────────────────────


def test_route_start_f7_requires_port(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: True)
    monkeypatch.setattr(
        "hokku.webserver.flask_app.bigme_firmware.firmware_image_file",
        lambda: tmp_path / "xr_system.img",
    )
    (tmp_path / "xr_system.img").write_bytes(b"AWIH" + b"\x00" * 32)
    client = _client(_bare_state(app_config), tmp_path)
    r = client.post("/hokku/api/flash/start_f7", json={})
    assert r.status_code == 400


def test_route_start_f7_503_without_tooling(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: False)
    client = _client(_bare_state(app_config), tmp_path)
    r = client.post("/hokku/api/flash/start_f7", json={"port": "COM7"})
    assert r.status_code == 503


def test_route_start_f7_503_without_image(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: True)
    monkeypatch.setattr(
        "hokku.webserver.flask_app.bigme_firmware.firmware_image_file", lambda: None
    )
    client = _client(_bare_state(app_config), tmp_path)
    r = client.post("/hokku/api/flash/start_f7", json={"port": "COM7"})
    assert r.status_code == 503


def test_route_start_f7_starts_and_is_single_slot(app_config, tmp_path, monkeypatch):
    img = tmp_path / "xr_system.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: True)
    monkeypatch.setattr("hokku.webserver.flask_app.bigme_firmware.firmware_image_file", lambda: img)

    def slow(port, image_path, on_line, should_cancel, **kw):
        on_line("catching")
        while not should_cancel():
            time.sleep(0.01)
        raise RuntimeError("cancelled by the operator")

    monkeypatch.setattr(flashing.f7_bootstrap, "bootstrap_device", slow)
    # /flash/devices needs a bundled ESP32 firmware present (else it 503s before the
    # busy check); provide one so we exercise the busy path, not the environment.
    monkeypatch.setattr(huessen_epf1301, "merged_firmware_file", lambda *a, **k: img)
    state = _bare_state(app_config)
    client = _client(state, tmp_path)

    r = client.post("/hokku/api/flash/start_f7", json={"port": "COM7"})
    assert r.status_code == 200
    assert "job_id" in r.get_json()

    # a device scan is refused while flashing
    assert client.get("/hokku/api/flash/devices").status_code == 409
    # a second F7 start is refused
    assert client.post("/hokku/api/flash/start_f7", json={"port": "COM7"}).status_code == 409

    # cancel via the route, then the job ends in error("cancelled")
    assert client.post("/hokku/api/flash/cancel").get_json()["cancelled"] is True
    st = _wait_state(state.flash_jobs, "error")
    assert st["state"] == "error" and "cancelled" in (st["error"] or "").lower()


def test_route_cancel_no_job(app_config, tmp_path):
    client = _client(_bare_state(app_config), tmp_path)
    assert client.post("/hokku/api/flash/cancel").get_json()["cancelled"] is False


# ── two-phase BROM entry (software `upgrade` first, catch fallback) ────────────


def _null_writer():
    return bigme_bootstrap._LineWriter(lambda _l: None)


class _FakeFlasher:
    """Stand-in for XR872Flasher; sync() result controls whether entry lands."""

    sync_result = False

    def __init__(self, *a, **k):
        self.closed = False
        # In-memory model of the 0x340000 config sector (default 0xFF = fresh unit).
        self.cfg_sector = bytearray(b"\xff" * 0x1000)

    def sync(self, **k):
        return type(self).sync_result

    def read_sector(self, addr, length):
        if addr == 0x340000:
            return bytes(self.cfg_sector[:length])
        return b"\xff" * length

    def erase_flash(self, addr, etype):
        if addr == 0x340000:
            self.cfg_sector = bytearray(b"\xff" * 0x1000)
        return True

    def write_sector(self, addr, data):
        if addr == 0x340000:
            self.cfg_sector[: len(data)] = data
        return True

    def close(self):
        self.closed = True


def test_software_entry_returns_flasher_when_sync_lands():
    class FF(_FakeFlasher):
        sync_result = True

    sent: list[str] = []
    f = bigme_bootstrap._software_entry(
        "COM7", _null_writer(), lambda: False, FF, lambda p: sent.append(p), attempts=3
    )
    assert isinstance(f, FF)
    assert sent == ["COM7"]  # landed on the first `upgrade`


def test_software_entry_gives_up_on_a_stock_unit():
    # sync never lands -> None after all attempts (each still sends `upgrade`).
    sent: list[str] = []
    f = bigme_bootstrap._software_entry(
        "COM7", _null_writer(), lambda: False, _FakeFlasher, lambda p: sent.append(p), attempts=4
    )
    assert f is None
    assert len(sent) == 4


def test_software_entry_honors_cancel():
    try:
        bigme_bootstrap._software_entry(
            "COM7", _null_writer(), lambda: True, _FakeFlasher, lambda p: None, attempts=5
        )
    except RuntimeError as e:
        assert "cancel" in str(e).lower()
    else:
        raise AssertionError("expected RuntimeError on cancel")


def _stub_import_tools(monkeypatch, flash_records):
    def fake_flash_slot0(f, img, reboot=False):
        flash_records.append(reboot)

    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: True)
    monkeypatch.setattr(
        bigme_bootstrap,
        "_import_tools",
        lambda: (fake_flash_slot0, None, None, object(), lambda p: None, None),
    )


def test_bootstrap_prefers_software_entry(tmp_path, monkeypatch):
    img = tmp_path / "x.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    records: list[bool] = []
    _stub_import_tools(monkeypatch, records)
    fake_f = _FakeFlasher()
    monkeypatch.setattr(bigme_bootstrap, "_software_entry", lambda *a, **k: fake_f)
    catch_calls = {"n": 0}

    def _catch(*a, **k):
        catch_calls["n"] += 1
        return fake_f

    monkeypatch.setattr(bigme_bootstrap, "_catch_entry", _catch)

    lines: list[str] = []
    result = bigme_bootstrap.bootstrap_device("COM7", img, lines.append, lambda: False)
    assert result == {"ok": True}
    assert records == [False]  # flash_slot0 called once, reboot=False
    assert catch_calls["n"] == 0  # never fell back to the manual catch
    assert fake_f.closed is True
    assert any("upgrade" in ln for ln in lines)


def test_bootstrap_falls_back_to_catch_for_stock(tmp_path, monkeypatch):
    img = tmp_path / "x.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    records: list[bool] = []
    _stub_import_tools(monkeypatch, records)
    fake_f = _FakeFlasher()
    monkeypatch.setattr(bigme_bootstrap, "_software_entry", lambda *a, **k: None)
    catch_calls = {"n": 0}

    def _catch(*a, **k):
        catch_calls["n"] += 1
        return fake_f

    monkeypatch.setattr(bigme_bootstrap, "_catch_entry", _catch)

    lines: list[str] = []
    result = bigme_bootstrap.bootstrap_device("COM7", img, lines.append, lambda: False)
    assert result == {"ok": True}
    assert records == [False]
    assert catch_calls["n"] == 1  # fell back to the manual catch
    assert any("stock unit" in ln.lower() for ln in lines)


# ── console provisioning ──────────────────────────────────────────────────────


def test_console_safe_token():
    assert bigme_bootstrap.console_safe_token("living-room")
    assert bigme_bootstrap.console_safe_token("McMansion")
    assert not bigme_bootstrap.console_safe_token("living room")
    assert not bigme_bootstrap.console_safe_token("a\tb")
    assert not bigme_bootstrap.console_safe_token("bad\nline")
    assert not bigme_bootstrap.console_safe_token("")


def test_bootstrap_provisions_config_via_brom_and_wifi_for_fresh_unit(tmp_path, monkeypatch):

    img = tmp_path / "x.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    records: list[bool] = []
    _stub_import_tools(monkeypatch, records)
    ff = _FakeFlasher()  # fresh/stock unit (config sector all 0xFF)
    # A stock unit ignores `upgrade` and is caught by hand -> _catch_entry, so the
    # console Wi-Fi step is expected (it isn't already running Hokku firmware).
    monkeypatch.setattr(bigme_bootstrap, "_software_entry", lambda *a, **k: None)
    monkeypatch.setattr(bigme_bootstrap, "_catch_entry", lambda *a, **k: ff)
    calls = {"n": 0, "prov": None}

    def fake_prov(port, prov, on_line, should_cancel, serial):
        calls["n"] += 1
        calls["prov"] = prov
        on_line("provisioned")

    monkeypatch.setattr(bigme_bootstrap, "_provision_over_console", fake_prov)
    prov = {"ssid": "Net", "psk": "pw", "name": "kitchen", "server_url": "http://x/hokku/screen/"}
    lines: list[str] = []
    bigme_bootstrap.bootstrap_device("COM7", img, lines.append, lambda: False, provision=prov)

    # Config (name + URL) was written to the 0x340000 FDCM sector over BROM...
    on_flash = f7cfg.config_from_blob(f7cfg.read_fdcm_data(bytes(ff.cfg_sector)) or b"")
    assert on_flash is not None
    assert on_flash["screen_name"] == "kitchen"
    assert on_flash["server_url"] == "http://x/hokku/screen/"
    # ...and a FRESH unit still gets Wi-Fi over the console.
    assert calls["n"] == 1
    assert calls["prov"] == prov


def test_bootstrap_skips_console_wifi_when_already_provisioned(tmp_path, monkeypatch):

    img = tmp_path / "x.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    records: list[bool] = []
    _stub_import_tools(monkeypatch, records)
    ff = _FakeFlasher()
    # Pre-provisioned unit: a valid config blob already on the sector.
    ff.cfg_sector[:0x1000] = f7cfg.build_fdcm_block(
        f7cfg.build_config_blob("http://old/hokku/screen/", "OldName")
    ).ljust(0x1000, b"\xff")
    monkeypatch.setattr(bigme_bootstrap, "_software_entry", lambda *a, **k: ff)
    calls = {"n": 0}
    monkeypatch.setattr(
        bigme_bootstrap,
        "_provision_over_console",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    prov = {"ssid": "Net", "psk": "pw", "name": "Renamed", "server_url": "http://new/hokku/screen/"}
    lines: list[str] = []
    bigme_bootstrap.bootstrap_device("COM7", img, lines.append, lambda: False, provision=prov)

    # Rename applied over BROM; server URL updated; console Wi-Fi step SKIPPED.
    on_flash = f7cfg.config_from_blob(f7cfg.read_fdcm_data(bytes(ff.cfg_sector)) or b"")
    assert on_flash is not None and on_flash["screen_name"] == "Renamed"
    assert on_flash["server_url"] == "http://new/hokku/screen/"
    assert calls["n"] == 0  # already had Wi-Fi -> no console step
    assert any("existing wi-fi" in ln.lower() for ln in lines)


def test_bootstrap_via_upgrade_skips_console_even_without_v2_config(tmp_path, monkeypatch):
    # The SmallOne case: the unit runs Hokku firmware (so `upgrade` catches it), but
    # its on-flash config blob is old/absent (no valid v2). It must STILL skip the
    # console Wi-Fi step — running Hokku firmware means its Wi-Fi is in sysinfo.
    img = tmp_path / "x.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    records: list[bool] = []
    _stub_import_tools(monkeypatch, records)
    ff = _FakeFlasher()  # config sector all 0xFF (no valid v2 blob)
    monkeypatch.setattr(bigme_bootstrap, "_software_entry", lambda *a, **k: ff)  # upgrade catches
    calls = {"n": 0}
    monkeypatch.setattr(
        bigme_bootstrap,
        "_provision_over_console",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    prov = {"ssid": "Net", "psk": "pw", "name": "Renamed", "server_url": "http://x/hokku/screen/"}
    lines: list[str] = []
    bigme_bootstrap.bootstrap_device("COM7", img, lines.append, lambda: False, provision=prov)

    on_flash = f7cfg.config_from_blob(f7cfg.read_fdcm_data(bytes(ff.cfg_sector)) or b"")
    assert on_flash is not None and on_flash["screen_name"] == "Renamed"  # config via BROM
    assert calls["n"] == 0  # entered via `upgrade` -> console skipped


def test_bootstrap_skips_provision_when_none(tmp_path, monkeypatch):
    img = tmp_path / "x.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    records: list[bool] = []
    _stub_import_tools(monkeypatch, records)
    monkeypatch.setattr(bigme_bootstrap, "_software_entry", lambda *a, **k: _FakeFlasher())
    calls = {"n": 0}
    monkeypatch.setattr(
        bigme_bootstrap,
        "_provision_over_console",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    lines: list[str] = []
    bigme_bootstrap.bootstrap_device("COM7", img, lines.append, lambda: False, provision=None)
    assert calls["n"] == 0
    assert any("console" in ln.lower() for ln in lines)


def test_route_start_f7_builds_provision(app_config, tmp_path, monkeypatch):
    img = tmp_path / "xr_system.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: True)
    monkeypatch.setattr("hokku.webserver.flask_app.bigme_firmware.firmware_image_file", lambda: img)
    captured = {}

    def fake_bootstrap(port, image_path, on_line, should_cancel, provision=None, **kw):
        captured["provision"] = provision
        return {"ok": True}

    monkeypatch.setattr(flashing.f7_bootstrap, "bootstrap_device", fake_bootstrap)
    state = _bare_state(app_config)
    client = _client(state, tmp_path)
    r = client.post(
        "/hokku/api/flash/start_f7",
        json={
            "port": "COM7",
            "wifi_ssid1": "Net",
            "wifi_pass1": "pw",
            "screen_name": "kitchen",
            "image_url": "http://x/hokku/screen/",
        },
    )
    assert r.status_code == 200
    _wait_state(state.flash_jobs, "done")
    assert captured["provision"] == {
        "ssid": "Net",
        "psk": "pw",
        "name": "kitchen",
        "server_url": "http://x/hokku/screen/",
    }


def test_route_start_f7_no_provision_when_blank(app_config, tmp_path, monkeypatch):
    img = tmp_path / "xr_system.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: True)
    monkeypatch.setattr("hokku.webserver.flask_app.bigme_firmware.firmware_image_file", lambda: img)
    captured: dict = {"provision": "unset"}

    def fake_bootstrap(port, image_path, on_line, should_cancel, provision=None, **kw):
        captured["provision"] = provision
        return {"ok": True}

    monkeypatch.setattr(flashing.f7_bootstrap, "bootstrap_device", fake_bootstrap)
    state = _bare_state(app_config)
    client = _client(state, tmp_path)
    r = client.post("/hokku/api/flash/start_f7", json={"port": "COM7"})
    assert r.status_code == 200
    _wait_state(state.flash_jobs, "done")
    assert captured["provision"] is None


def _f7_ready(tmp_path, monkeypatch):
    img = tmp_path / "xr_system.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: True)
    monkeypatch.setattr("hokku.webserver.flask_app.bigme_firmware.firmware_image_file", lambda: img)
    monkeypatch.setattr(
        flashing.f7_bootstrap,
        "bootstrap_device",
        lambda port, image_path, on_line, should_cancel, provision=None, **kw: {"ok": True},
    )


def test_route_start_f7_remembers_wifi_credentials(app_config, tmp_path, monkeypatch):
    """A bootstrap persists its Wi-Fi credentials like the ESP32 flash does, so
    the form pre-fills next time. The F7 has no fallback network, so a previously
    remembered second network must survive untouched."""
    _f7_ready(tmp_path, monkeypatch)
    cfg = replace(app_config, flash_wifi_ssid2="Old2", flash_wifi_pass2="oldpw2")
    state = _bare_state(cfg)
    client = _client(state, tmp_path)

    r = client.post(
        "/hokku/api/flash/start_f7",
        json={"port": "COM7", "wifi_ssid1": "Net", "wifi_pass1": "secret"},
    )
    assert r.status_code == 200
    _wait_state(state.flash_jobs, "done")
    assert state.config.flash_wifi_ssid == "Net"
    assert state.config.flash_wifi_pass == "secret"  # noqa: S105
    assert state.config.flash_wifi_ssid2 == "Old2"  # not clobbered by a model without one
    assert state.config.flash_wifi_pass2 == "oldpw2"


def test_route_start_f7_without_wifi_keeps_remembered_credentials(
    app_config, tmp_path, monkeypatch
):
    """Bootstrapping with the Wi-Fi fields blank (provision skipped) must not wipe
    the remembered credentials."""
    _f7_ready(tmp_path, monkeypatch)
    cfg = replace(app_config, flash_wifi_ssid="Net", flash_wifi_pass="secret")
    state = _bare_state(cfg)
    client = _client(state, tmp_path)

    r = client.post("/hokku/api/flash/start_f7", json={"port": "COM7"})
    assert r.status_code == 200
    _wait_state(state.flash_jobs, "done")
    assert state.config.flash_wifi_ssid == "Net"
    assert state.config.flash_wifi_pass == "secret"  # noqa: S105


def test_route_start_f7_rejects_spaces(app_config, tmp_path, monkeypatch):
    img = tmp_path / "xr_system.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: True)
    monkeypatch.setattr("hokku.webserver.flask_app.bigme_firmware.firmware_image_file", lambda: img)
    client = _client(_bare_state(app_config), tmp_path)
    for field in ("wifi_ssid1", "wifi_pass1", "screen_name"):
        r = client.post("/hokku/api/flash/start_f7", json={"port": "COM7", field: "has space"})
        assert r.status_code == 400, field


def test_route_start_f7_passes_local_url_through(app_config, tmp_path, monkeypatch):
    # F7 fw >= 1.2.2 resolves mDNS .local, so the server no longer pins it to an IP.
    img = tmp_path / "xr_system.img"
    img.write_bytes(b"AWIH" + b"\x00" * 32)
    monkeypatch.setattr(bigme_bootstrap, "tooling_available", lambda: True)
    monkeypatch.setattr("hokku.webserver.flask_app.bigme_firmware.firmware_image_file", lambda: img)
    captured = {}

    def fake_bootstrap(port, image_path, on_line, should_cancel, provision=None, **kw):
        captured["provision"] = provision
        return {"ok": True}

    monkeypatch.setattr(flashing.f7_bootstrap, "bootstrap_device", fake_bootstrap)
    state = _bare_state(app_config)
    client = _client(state, tmp_path)
    r = client.post(
        "/hokku/api/flash/start_f7",
        json={"port": "COM7", "image_url": "http://hokku-test.local:8080/hokku/screen/"},
    )
    assert r.status_code == 200
    _wait_state(state.flash_jobs, "done")
    assert captured["provision"]["server_url"] == "http://hokku-test.local:8080/hokku/screen/"
